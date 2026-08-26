"""
Tests for download error classification and the retry pacing it drives.

Not every failure deserves the same treatment. Retrying a members-only video every few
minutes burns the whole retry budget on something that can never succeed, while a
premiere airing in three hours needs to be retried *after* it airs, not five times in
half an hour.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from media.models import MediaItem
from media.service.errors import (
    CATEGORY_BLOCKED,
    CATEGORY_PERMANENT,
    CATEGORY_SCHEDULED,
    CATEGORY_TRANSIENT,
    classify_download_error,
    parse_wait_hint,
)
from media.tasks import schedule_retry_or_fail

# The three messages that prompted this, copied verbatim from production logs.
PREMIERE = 'ERROR: [youtube] 1d4cLDVV-7M: Premieres in 3 hours'
MEMBERS_ONLY = (
    'ERROR: [youtube] fvtgZ57EO3k: Join this channel to get access to members-only '
    'content like this video, and other exclusive perks.'
)
BOT_CHECK = (
    'ERROR: [youtube] TyWOGmQoXqc: Sign in to confirm you’re not a bot. Use '
    '--cookies-from-browser or --cookies for the authentication.'
)


class ClassifyDownloadErrorTest(TestCase):
    """Tests against the real-world messages, not invented ones"""

    def test_premiere_is_scheduled(self):
        self.assertEqual(classify_download_error(PREMIERE), CATEGORY_SCHEDULED)

    def test_members_only_is_permanent(self):
        self.assertEqual(classify_download_error(MEMBERS_ONLY), CATEGORY_PERMANENT)

    def test_bot_check_is_blocked(self):
        self.assertEqual(classify_download_error(BOT_CHECK), CATEGORY_BLOCKED)

    def test_typographic_apostrophe_is_handled(self):
        """YouTube writes "you’re" with U+2019; an ASCII-only pattern would miss it"""
        self.assertIn('’', BOT_CHECK)
        self.assertEqual(classify_download_error(BOT_CHECK), CATEGORY_BLOCKED)

    def test_private_and_removed_videos_are_permanent(self):
        self.assertEqual(
            classify_download_error('ERROR: [youtube] x: Private video'), CATEGORY_PERMANENT
        )
        self.assertEqual(
            classify_download_error('ERROR: [youtube] x: Video unavailable'),
            CATEGORY_PERMANENT,
        )

    def test_forbidden_stays_transient(self):
        """403 on the media URL is a format/CDN problem that a retry can fix"""
        self.assertEqual(
            classify_download_error('unable to download video data: HTTP Error 403: Forbidden'),
            CATEGORY_TRANSIENT,
        )

    def test_rate_limit_is_blocked(self):
        self.assertEqual(
            classify_download_error('HTTP Error 429: Too Many Requests'), CATEGORY_BLOCKED
        )

    def test_unknown_message_is_transient(self):
        self.assertEqual(classify_download_error('something odd happened'), CATEGORY_TRANSIENT)

    def test_empty_message_is_transient(self):
        self.assertEqual(classify_download_error(''), CATEGORY_TRANSIENT)
        self.assertEqual(classify_download_error(None), CATEGORY_TRANSIENT)


class ParseWaitHintTest(TestCase):
    def test_hours(self):
        """3 hours plus the safety margin"""
        self.assertEqual(parse_wait_hint(PREMIERE), timedelta(hours=3, minutes=10))

    def test_minutes(self):
        self.assertEqual(parse_wait_hint('Premieres in 25 minutes'), timedelta(minutes=35))

    def test_days(self):
        self.assertEqual(parse_wait_hint('Premieres in 2 days'), timedelta(days=2, minutes=10))

    def test_singular_unit(self):
        self.assertEqual(parse_wait_hint('Premieres in 1 hour'), timedelta(hours=1, minutes=10))

    def test_no_hint(self):
        self.assertIsNone(parse_wait_hint(MEMBERS_ONLY))
        self.assertIsNone(parse_wait_hint(''))


class RetryPacingTest(TestCase):
    """Tests how the classification changes what the queue actually does"""

    def _item(self, attempts=1, slug='err'):
        return MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            slug=slug,
            status=MediaItem.STATUS_PREFETCHING,
            download_attempts=attempts,
        )

    def _minutes_until_retry(self, item):
        return (item.next_attempt_at - timezone.now()).total_seconds() / 60

    @override_settings(STASHCAST_DOWNLOAD_MAX_ATTEMPTS=3)
    def test_members_only_fails_immediately(self):
        """The whole point: do not spend 3 attempts on something that cannot work"""
        item = self._item()

        requeued = schedule_retry_or_fail(item, MEMBERS_ONLY)

        self.assertFalse(requeued)
        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_ERROR)
        self.assertIsNone(item.next_attempt_at)
        self.assertEqual(item.download_attempts, 1)

    def test_members_only_error_explains_itself(self):
        item = self._item()

        schedule_retry_or_fail(item, MEMBERS_ONLY)

        item.refresh_from_db()
        self.assertIn('Not retrying', item.error_message)

    @override_settings(STASHCAST_DOWNLOAD_SCHEDULED_MAX_ATTEMPTS=8)
    def test_premiere_waits_until_it_airs(self):
        item = self._item()

        requeued = schedule_retry_or_fail(item, PREMIERE)

        self.assertTrue(requeued)
        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_QUEUED)
        # 3 hours + 10 minutes of margin, not the usual 5
        self.assertGreater(self._minutes_until_retry(item), 180)

    @override_settings(
        STASHCAST_DOWNLOAD_SCHEDULED_MAX_ATTEMPTS=8,
        STASHCAST_DOWNLOAD_RETRY_SCHEDULED_MINUTES=60,
    )
    def test_premiere_without_a_time_uses_the_configured_wait(self):
        item = self._item()

        schedule_retry_or_fail(item, 'ERROR: [youtube] x: This video is upcoming')

        item.refresh_from_db()
        self.assertGreater(self._minutes_until_retry(item), 55)

    @override_settings(
        STASHCAST_DOWNLOAD_MAX_ATTEMPTS=3, STASHCAST_DOWNLOAD_SCHEDULED_MAX_ATTEMPTS=8
    )
    def test_premiere_gets_a_larger_attempt_budget(self):
        """Each premiere attempt costs hours, so 3 attempts would be too few"""
        item = self._item(attempts=5)

        requeued = schedule_retry_or_fail(item, PREMIERE)

        self.assertTrue(requeued)

    @override_settings(STASHCAST_DOWNLOAD_RETRY_BLOCKED_MINUTES=120)
    def test_bot_check_rests_for_a_long_time(self):
        item = self._item()

        schedule_retry_or_fail(item, BOT_CHECK)

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_QUEUED)
        self.assertGreater(self._minutes_until_retry(item), 110)

    def test_bot_check_points_at_the_cookies_setting(self):
        item = self._item()

        schedule_retry_or_fail(item, BOT_CHECK)

        item.refresh_from_db()
        self.assertIn('STASHCAST_YTDLP_COOKIES_FILE', item.error_message)

    @override_settings(STASHCAST_DOWNLOAD_QUEUE_MINUTES=5, STASHCAST_DOWNLOAD_MAX_ATTEMPTS=3)
    def test_transient_error_keeps_the_short_backoff(self):
        """Ordinary failures must not inherit the long category waits"""
        item = self._item()

        schedule_retry_or_fail(item, 'unable to download video data: HTTP Error 403: Forbidden')

        item.refresh_from_db()
        self.assertLess(self._minutes_until_retry(item), 10)

    @override_settings(STASHCAST_DOWNLOAD_SCHEDULED_MAX_ATTEMPTS=2)
    def test_scheduled_still_gives_up_eventually(self):
        """A premiere that never airs must not be retried forever"""
        item = self._item(attempts=2)

        requeued = schedule_retry_or_fail(item, PREMIERE)

        self.assertFalse(requeued)
        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_ERROR)
