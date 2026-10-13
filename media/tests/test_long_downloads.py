"""
Tests for the fixes behind failing long YouTube downloads.

- task_heartbeat keeps an item alive through postprocessing, so recover_stuck_items
  no longer releases it a second time while it is still being converted;
- item_lock stops two tasks from working in the same tmp-<guid> directory;
- audio prefers AAC so the m4a conversion is a stream copy, and --audio-quality is
  actually passed on;
- the player-client fallback moves on after client-specific failures and reports the
  first error instead of the last.
"""

import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from media.models import MediaItem
from media.processing import item_lock, task_heartbeat
from media.service.config import parse_ytdlp_extra_args
from media.service.download import default_format_spec, run_ytdlp_with_fallback
from media.tasks import process_media


def make_item(**fields):
    defaults = dict(
        source_url='https://www.youtube.com/watch?v=long',
        requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
        slug='pending',
        status=MediaItem.STATUS_DOWNLOADING,
    )
    defaults.update(fields)
    return MediaItem.objects.create(**defaults)


class ItemLockTest(TestCase):
    def test_second_holder_is_refused_while_the_first_runs(self):
        with item_lock('abc') as first:
            with item_lock('abc') as second:
                self.assertTrue(first)
                self.assertFalse(second)

    def test_lock_is_free_again_afterwards(self):
        with item_lock('abc'):
            pass
        with item_lock('abc') as again:
            self.assertTrue(again)

    def test_other_items_are_independent(self):
        with item_lock('abc'), item_lock('xyz') as other:
            self.assertTrue(other)

    def test_lock_separates_worker_threads(self):
        """Huey runs thread workers, so the lock must hold across threads too"""
        results = []

        def other_worker():
            with item_lock('abc') as acquired:
                results.append(acquired)

        with item_lock('abc'):
            thread = threading.Thread(target=other_worker)
            thread.start()
            thread.join()
        self.assertEqual(results, [False])

    def test_lock_released_when_the_task_raises(self):
        with self.assertRaises(RuntimeError):
            with item_lock('abc'):
                raise RuntimeError('boom')
        with item_lock('abc') as again:
            self.assertTrue(again)


class ProcessMediaLockTest(TestCase):
    def test_duplicate_run_leaves_the_item_alone(self):
        item = make_item()

        with item_lock(item.guid):
            process_media.call_local(item.guid)

        item.refresh_from_db()
        self.assertEqual(item.download_attempts, 0)
        self.assertEqual(item.status, MediaItem.STATUS_DOWNLOADING)


class TaskHeartbeatTest(TransactionTestCase):
    """The heartbeat thread writes through its own connection, hence no TestCase"""

    def _age(self, item, minutes=60):
        old = timezone.now() - timedelta(minutes=minutes)
        MediaItem.objects.filter(pk=item.pk).update(updated_at=old)
        return old

    def test_keeps_an_item_in_progress_alive(self):
        item = make_item()
        old = self._age(item)

        with task_heartbeat([item.guid], interval_seconds=0.05):
            time.sleep(0.3)

        item.refresh_from_db()
        self.assertGreater(item.updated_at, old)

    def test_finished_items_are_not_touched(self):
        item = make_item(status=MediaItem.STATUS_READY)
        old = self._age(item)

        with task_heartbeat([item.guid], interval_seconds=0.05):
            time.sleep(0.3)

        item.refresh_from_db()
        self.assertEqual(item.updated_at, old)

    def test_items_added_while_running_are_kept_alive(self):
        first = make_item()
        later = make_item(source_url='https://www.youtube.com/watch?v=later')
        old = self._age(later)
        guids = {first.guid}

        with task_heartbeat(guids, interval_seconds=0.05):
            guids.add(later.guid)
            time.sleep(0.3)

        later.refresh_from_db()
        self.assertGreater(later.updated_at, old)

    def test_stops_with_the_task(self):
        item = make_item()

        with task_heartbeat([item.guid], interval_seconds=0.05):
            pass
        old = self._age(item)
        time.sleep(0.2)

        item.refresh_from_db()
        self.assertEqual(item.updated_at, old)
        self.assertFalse(any(t.name == 'stashcast-task-heartbeat' for t in threading.enumerate()))


class AudioFormatTest(TestCase):
    def test_audio_prefers_aac(self):
        self.assertEqual(default_format_spec('audio'), 'bestaudio[ext=m4a]/bestaudio/best')

    def test_audio_quality_reaches_the_conversion(self):
        opts = parse_ytdlp_extra_args('--audio-format m4a --audio-quality 128K', {})

        extract = [p for p in opts['postprocessors'] if p['key'] == 'FFmpegExtractAudio']
        self.assertEqual(extract[0]['preferredquality'], '128')

    def test_audio_quality_before_the_format_still_applies(self):
        opts = parse_ytdlp_extra_args('--audio-quality 5 --audio-format m4a', {})

        extract = [p for p in opts['postprocessors'] if p['key'] == 'FFmpegExtractAudio']
        self.assertEqual(extract[0]['preferredquality'], '5')


@override_settings(STASHCAST_YTDLP_PLAYER_CLIENTS='default,tv,web_safari')
class PlayerClientFallbackTest(TestCase):
    def setUp(self):
        # Clearing the player cache between attempts must not touch the real one
        patch('media.service.download.yt_dlp.YoutubeDL', MagicMock()).start()
        self.addCleanup(patch.stopall)

    def _run(self, outcomes):
        """Run the fallback where attempt N raises/returns outcomes[N]."""
        clients = []

        def run(opts):
            clients.append(opts.get('extractor_args', {}).get('youtube', {}).get('player_client'))
            outcome = outcomes[len(clients) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return run_ytdlp_with_fallback(dict, run), clients

    def test_format_not_available_moves_on_to_the_next_client(self):
        result, clients = self._run(
            [Exception('ERROR: [youtube] x: Requested format is not available'), 'ok']
        )

        self.assertEqual(result, 'ok')
        self.assertEqual(clients, [None, ['tv']])

    def test_fallback_failing_differently_keeps_going(self):
        """A client refusing in its own way used to end the chain right there"""
        result, clients = self._run(
            [
                Exception('HTTP Error 403: Forbidden'),
                Exception('ERROR: [youtube] x: The page needs to be reloaded.'),
                'ok',
            ]
        )

        self.assertEqual(result, 'ok')
        self.assertEqual(len(clients), 3)

    def test_first_error_is_reported_when_every_client_fails(self):
        with self.assertRaisesRegex(Exception, '403'):
            self._run(
                [
                    Exception('HTTP Error 403: Forbidden'),
                    Exception('The page needs to be reloaded.'),
                    Exception('Requested format is not available'),
                ]
            )

    def test_unrelated_errors_are_not_retried(self):
        with self.assertRaisesRegex(Exception, 'Conversion failed'):
            self._run([Exception('ERROR: Postprocessing: Conversion failed!'), 'ok'])
