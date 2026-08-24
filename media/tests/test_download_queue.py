"""
Tests for the paced download queue.

Items stashed by the YouTube channel sync wait in STATUS_QUEUED and are released a
batch at a time, so a channel with a large backlog cannot flood the Huey workers.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from media.models import MediaItem
from media.tasks import (
    _backoff_delay_minutes,
    _download_queue_crontab,
    recover_stuck_items,
    release_download_queue,
    retry_failed_items,
    worker_is_alive,
)


def make_item(status=MediaItem.STATUS_QUEUED, url=None, **kwargs):
    """Create a MediaItem with sensible defaults for queue tests."""
    counter = MediaItem.objects.count()
    return MediaItem.objects.create(
        source_url=url or f'https://youtu.be/v{counter}',
        requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
        slug='pending',
        status=status,
        **kwargs,
    )


class ReleaseDownloadQueueTest(TestCase):
    def setUp(self):
        self.process_media = patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

    @override_settings(STASHCAST_DOWNLOAD_QUEUE_BATCH=1)
    def test_releases_one_item_per_run_by_default(self):
        for _ in range(5):
            make_item()

        released = release_download_queue()

        self.assertEqual(len(released), 1)
        self.assertEqual(self.process_media.call_count, 1)
        self.assertEqual(MediaItem.objects.filter(status=MediaItem.STATUS_QUEUED).count(), 4)

    @override_settings(STASHCAST_DOWNLOAD_QUEUE_BATCH=3)
    def test_batch_size_is_configurable(self):
        for _ in range(5):
            make_item()

        released = release_download_queue()

        self.assertEqual(len(released), 3)
        self.assertEqual(MediaItem.objects.filter(status=MediaItem.STATUS_QUEUED).count(), 2)

    def test_explicit_limit_overrides_setting(self):
        for _ in range(5):
            make_item()

        released = release_download_queue(limit=4)

        self.assertEqual(len(released), 4)

    def test_released_items_are_claimed(self):
        """An item is moved out of QUEUED before enqueuing, so it cannot go twice"""
        item = make_item()

        release_download_queue(limit=1)

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_PREFETCHING)

        # A second run finds nothing left to do
        self.assertEqual(release_download_queue(limit=1), [])

    def test_oldest_items_go_first(self):
        """A backlog drains in arrival order"""
        first = make_item(url='https://youtu.be/first')
        second = make_item(url='https://youtu.be/second')
        MediaItem.objects.filter(guid=first.guid).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        MediaItem.objects.filter(guid=second.guid).update(
            created_at=timezone.now() - timedelta(hours=1)
        )

        released = release_download_queue(limit=1)

        self.assertEqual(released[0].guid, first.guid)

    def test_backoff_deadline_is_respected(self):
        """An item waiting out its retry backoff is not released early"""
        make_item(next_attempt_at=timezone.now() + timedelta(minutes=30))

        self.assertEqual(release_download_queue(limit=5), [])
        self.assertEqual(self.process_media.call_count, 0)

    def test_item_past_its_deadline_is_released(self):
        item = make_item(next_attempt_at=timezone.now() - timedelta(minutes=1))

        released = release_download_queue(limit=5)

        self.assertEqual([r.guid for r in released], [item.guid])

    def test_other_statuses_are_ignored(self):
        make_item(status=MediaItem.STATUS_READY)
        make_item(status=MediaItem.STATUS_ERROR)
        make_item(status=MediaItem.STATUS_DOWNLOADING)

        self.assertEqual(release_download_queue(limit=5), [])

    def test_empty_queue_is_harmless(self):
        self.assertEqual(release_download_queue(), [])


class RecoverStuckItemsTest(TestCase):
    def setUp(self):
        patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

    def _make_stale(self, status, minutes):
        item = make_item(status=status)
        MediaItem.objects.filter(guid=item.guid).update(
            updated_at=timezone.now() - timedelta(minutes=minutes)
        )
        item.refresh_from_db()
        return item

    @override_settings(STASHCAST_STUCK_TIMEOUT_MINUTES=30, STASHCAST_DOWNLOAD_MAX_ATTEMPTS=3)
    def test_abandoned_item_is_requeued(self):
        """An item left behind by a dead worker goes back to the queue by itself"""
        item = self._make_stale(MediaItem.STATUS_DOWNLOADING, minutes=60)

        recovered = recover_stuck_items()

        self.assertEqual(len(recovered), 1)
        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_QUEUED)
        self.assertIn('Abandoned', item.error_message)

    @override_settings(STASHCAST_STUCK_TIMEOUT_MINUTES=30)
    def test_recently_updated_item_is_left_alone(self):
        """A download still making progress must not be snatched away"""
        item = self._make_stale(MediaItem.STATUS_DOWNLOADING, minutes=5)

        self.assertEqual(recover_stuck_items(), [])
        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_DOWNLOADING)

    @override_settings(STASHCAST_STUCK_TIMEOUT_MINUTES=30, STASHCAST_DOWNLOAD_MAX_ATTEMPTS=1)
    def test_exhausted_item_is_failed(self):
        item = self._make_stale(MediaItem.STATUS_PROCESSING, minutes=60)
        MediaItem.objects.filter(guid=item.guid).update(download_attempts=1)

        recover_stuck_items()

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_ERROR)

    @override_settings(STASHCAST_STUCK_TIMEOUT_MINUTES=30, STASHCAST_DOWNLOAD_MAX_ATTEMPTS=3)
    def test_repeated_abandonment_eventually_gives_up(self):
        """Recovery must count the attempt, or the item loops between states forever.

        A worker that dies before process_media runs never increments the counter, so
        recover_stuck_items has to do it - otherwise the item is requeued, released,
        abandoned and requeued again without ever reaching the attempt limit.
        """
        item = self._make_stale(MediaItem.STATUS_PREFETCHING, minutes=60)
        self.assertEqual(item.download_attempts, 0)

        for _ in range(10):
            recover_stuck_items()
            item.refresh_from_db()
            if item.status == MediaItem.STATUS_ERROR:
                break
            # Simulate the item being released and abandoned again
            MediaItem.objects.filter(guid=item.guid).update(
                status=MediaItem.STATUS_PREFETCHING,
                updated_at=timezone.now() - timedelta(minutes=60),
            )
            item.refresh_from_db()

        self.assertEqual(item.status, MediaItem.STATUS_ERROR)

    @override_settings(STASHCAST_STUCK_TIMEOUT_MINUTES=30)
    def test_finished_statuses_are_not_touched(self):
        ready = self._make_stale(MediaItem.STATUS_READY, minutes=600)
        errored = self._make_stale(MediaItem.STATUS_ERROR, minutes=600)
        queued = self._make_stale(MediaItem.STATUS_QUEUED, minutes=600)

        self.assertEqual(recover_stuck_items(), [])
        for item in (ready, errored, queued):
            before = item.status
            item.refresh_from_db()
            self.assertEqual(item.status, before)


class RetryFailedItemsTest(TestCase):
    def setUp(self):
        patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

    def test_failed_items_return_to_the_queue_with_fresh_attempts(self):
        item = make_item(
            status=MediaItem.STATUS_ERROR,
            download_attempts=3,
            error_message='HTTP Error 403: Forbidden',
        )

        requeued = retry_failed_items()

        self.assertEqual(len(requeued), 1)
        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_QUEUED)
        self.assertEqual(item.download_attempts, 0)
        self.assertIsNone(item.next_attempt_at)
        self.assertEqual(item.error_message, '')

    def test_healthy_items_are_untouched(self):
        ready = make_item(status=MediaItem.STATUS_READY)

        self.assertEqual(retry_failed_items(), [])
        ready.refresh_from_db()
        self.assertEqual(ready.status, MediaItem.STATUS_READY)


@override_settings(STASHCAST_DOWNLOAD_QUEUE_MINUTES=5)
class BackoffTest(TestCase):
    def test_backoff_grows_with_attempts(self):
        self.assertEqual(_backoff_delay_minutes(1), 5)
        self.assertEqual(_backoff_delay_minutes(2), 10)
        self.assertEqual(_backoff_delay_minutes(3), 20)

    def test_backoff_is_capped(self):
        """A long-broken URL cannot push its retry arbitrarily far out"""
        self.assertEqual(_backoff_delay_minutes(99), 6 * 60)

    def test_zero_attempts_does_not_go_negative(self):
        self.assertEqual(_backoff_delay_minutes(0), 5)


class QueueCrontabTest(TestCase):
    @override_settings(STASHCAST_DOWNLOAD_QUEUE_MINUTES=5)
    def test_crontab_built_from_settings(self):
        self.assertTrue(callable(_download_queue_crontab()))

    @override_settings(STASHCAST_DOWNLOAD_QUEUE_MINUTES=0)
    def test_interval_clamped_to_valid_minute_expression(self):
        """0 would produce an invalid '*/0' crontab expression"""
        self.assertTrue(callable(_download_queue_crontab()))

    @override_settings(STASHCAST_DOWNLOAD_QUEUE_MINUTES=999)
    def test_large_interval_clamped(self):
        self.assertTrue(callable(_download_queue_crontab()))


class WorkerHeartbeatTest(TestCase):
    def test_missing_heartbeat_means_not_alive(self):
        with patch('media.tasks.heartbeat_path') as mock_path:
            mock_path.return_value.stat.side_effect = OSError()
            self.assertFalse(worker_is_alive())

    @override_settings(STASHCAST_WORKER_HEARTBEAT_STALE_SECONDS=180)
    def test_fresh_heartbeat_means_alive(self):
        import time

        with patch('media.tasks.heartbeat_path') as mock_path:
            mock_path.return_value.stat.return_value.st_mtime = time.time()
            self.assertTrue(worker_is_alive())

    @override_settings(STASHCAST_WORKER_HEARTBEAT_STALE_SECONDS=180)
    def test_stale_heartbeat_means_not_alive(self):
        import time

        with patch('media.tasks.heartbeat_path') as mock_path:
            mock_path.return_value.stat.return_value.st_mtime = time.time() - 600
            self.assertFalse(worker_is_alive())


class QueuePositionColumnTest(TestCase):
    """Tests the admin column that shows where an item sits in the queue.

    The changelist's default ordering is by publication/download date, which says
    nothing about release order - hence a dedicated column.
    """

    def setUp(self):
        patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

        from django.contrib import admin as dj_admin

        from media.admin import MediaItemAdmin

        self.admin = MediaItemAdmin(MediaItem, dj_admin.site)

    def _queued(self, title, minutes_ago, **kwargs):
        item = make_item(status=MediaItem.STATUS_QUEUED, url=f'https://youtu.be/{title}', **kwargs)
        item.title = title
        item.save()
        MediaItem.objects.filter(pk=item.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago)
        )
        item.refresh_from_db()
        return item

    def _value(self, item):
        """Rendered column text, with tags stripped and HTML entities decoded."""
        import html
        import re

        rendered = str(self.admin.queue_position_display(item))
        return html.unescape(re.sub(r'<[^>]+>', '', rendered)).strip()

    def test_positions_follow_created_at(self):
        first = self._queued('pierwszy', 30)
        second = self._queued('drugi', 20)
        third = self._queued('trzeci', 10)

        self.assertEqual(self._value(first), '1')
        self.assertEqual(self._value(second), '2')
        self.assertEqual(self._value(third), '3')

    def test_position_matches_what_the_queue_releases(self):
        """The column would be worse than nothing if it disagreed with reality"""
        self._queued('pierwszy', 30)
        self._queued('drugi', 20)

        released = release_download_queue(limit=2)

        self.assertEqual([r.title for r in released], ['pierwszy', 'drugi'])

    def test_item_waiting_on_backoff_shows_its_deadline(self):
        """It is skipped until then, so a position number would be misleading"""
        item = self._queued(
            'zepsuty', 40, next_attempt_at=timezone.now() + timedelta(minutes=12)
        )

        value = self._value(item)

        self.assertNotEqual(value, '1')
        self.assertIn('try', value)

    def test_backoff_item_does_not_take_up_a_position(self):
        self._queued('zepsuty', 40, next_attempt_at=timezone.now() + timedelta(minutes=12))
        due = self._queued('gotowy', 30)

        self.assertEqual(self._value(due), '1')

    def test_non_queued_items_show_a_dash(self):
        ready = make_item(status=MediaItem.STATUS_READY)

        self.assertEqual(self._value(ready), '—')

    def test_column_sorts_by_created_at(self):
        from media.admin import MediaItemAdmin

        self.assertEqual(
            MediaItemAdmin.queue_position_display.admin_order_field, 'created_at'
        )

    def test_changelist_renders_the_column(self):
        from django.contrib.auth import get_user_model
        from django.test import Client

        self._queued('pierwszy', 10)
        User = get_user_model()
        User.objects.create_superuser('qadmin', 'q@test.com', 'password')
        client = Client()
        client.login(username='qadmin', password='password')

        response = client.get('/admin/media/mediaitem/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('queue_position_display', response.content.decode())
