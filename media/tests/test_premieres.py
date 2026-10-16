"""
Premieres and live streams must not be downloaded while they are on air.

The reported case: a premiere listed as "Premieres in 39 minutes" was retried 10 minutes
after it started. yt-dlp then recorded it live from that moment until it ended, and
the file was 855s of a 1352s video. A premiere airs for as long as the video lasts, so
the download has to wait for its start plus its duration.
"""

import shutil
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from media.models import MediaItem
from media.processing import still_airing_reason
from media.service.download import airing_filter
from media.service.errors import CATEGORY_SCHEDULED, classify_download_error, parse_wait_hint
from media.service.resolve import PrefetchResult
from media.tasks import process_media

NOW = 1_800_000_000


def airing(status, started_ago=None, duration=1352):
    return PrefetchResult(
        title='Premiere',
        live_status=status,
        release_timestamp=None if started_ago is None else NOW - started_ago,
        duration_seconds=duration,
        has_audio_streams=True,
    )


class StillAiringReasonTest(TestCase):
    def test_finished_videos_are_fine(self):
        for status in (None, 'not_live', 'was_live'):
            self.assertIsNone(still_airing_reason(airing(status, started_ago=9999), now=NOW))

    def test_premiere_on_air_waits_for_its_end_not_its_start(self):
        # The reported case: started 10 minutes ago, 1352s long - 13 minutes to go
        reason = still_airing_reason(airing('is_live', started_ago=600), now=NOW)

        self.assertIn('ends in 13 minutes', reason)

    def test_upcoming_premiere_waits_for_start_plus_duration(self):
        reason = still_airing_reason(airing('is_upcoming', started_ago=-39 * 60), now=NOW)

        # 39 minutes to the start + 22.5 minutes of video
        self.assertIn('ends in 62 minutes', reason)

    def test_unknown_end_falls_back_to_regular_pacing(self):
        for result in (airing('is_live'), airing('post_live', started_ago=600)):
            reason = still_airing_reason(result, now=NOW)
            self.assertIn('end time unknown', reason)
            self.assertIsNone(parse_wait_hint(reason))

    def test_reason_is_scheduled_with_a_wait_after_the_end(self):
        reason = still_airing_reason(airing('is_live', started_ago=600), now=NOW)

        self.assertEqual(classify_download_error(reason), CATEGORY_SCHEDULED)
        # 13 minutes plus the 10 minute margin
        self.assertEqual(parse_wait_hint(reason), timedelta(minutes=23))


class AiringFilterTest(TestCase):
    def test_refuses_videos_on_air_and_records_why(self):
        skipped = []
        match_filter = airing_filter(skipped)

        self.assertIsNotNone(match_filter({'live_status': 'is_live'}))
        self.assertIsNone(match_filter({'live_status': 'was_live'}))
        self.assertIsNone(match_filter({}))
        self.assertEqual(len(skipped), 1)
        self.assertIn('still airing', skipped[0])


class PipelineTest(TestCase):
    """process_media with yt-dlp mocked out"""

    def setUp(self):
        self.item = MediaItem.objects.create(
            source_url='https://www.youtube.com/watch?v=E0ia5pkLKwg',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
            status=MediaItem.STATUS_PREFETCHING,
            error_message='ERROR: Premieres in 39 minutes (attempt 1/8, retrying in 49 min)',
        )
        self.download = patch('media.tasks.download_ytdlp').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(shutil.rmtree, Path(settings.STASHCAST_MEDIA_DIR) / 'premiere-slug', True)

    def _prefetch_returning(self, result):
        return patch('media.processing.service_prefetch', return_value=result)

    def test_premiere_on_air_is_requeued_for_after_it_ends(self):
        result = airing('is_live', started_ago=600)
        result.release_timestamp = int(time.time()) - 600

        # The task re-raises after scheduling the retry, so Huey logs the failure
        with self._prefetch_returning(result), self.assertRaisesRegex(Exception, 'still airing'):
            process_media.call_local(self.item.guid)

        self.item.refresh_from_db()
        self.download.assert_not_called()
        self.assertEqual(self.item.status, MediaItem.STATUS_QUEUED)
        self.assertIn('still airing', self.item.error_message)
        wait = self.item.next_attempt_at - timezone.now()
        # ~13 minutes left plus the 10 minute margin, not the old fixed retry
        self.assertGreater(wait, timedelta(minutes=21))
        self.assertLess(wait, timedelta(minutes=25))

    def test_successful_download_clears_the_old_error(self):
        def fake_download(item, tmp_dir, log_path):
            (tmp_dir / 'content.m4a').write_bytes(b'audio')
            item.content_path = 'content.m4a'
            item.save()

        self.download.side_effect = fake_download
        result = PrefetchResult(
            title='Premiere slug', live_status='was_live', has_audio_streams=True
        )
        with (
            self._prefetch_returning(result),
            patch('media.processing.generate_slug', return_value='premiere-slug'),
            patch('media.tasks.process_files'),
            patch('media.tasks.record_download_duration'),
        ):
            process_media.call_local(self.item.guid)

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, MediaItem.STATUS_READY)
        self.assertEqual(self.item.error_message, '')
