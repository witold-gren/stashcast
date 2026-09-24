"""
Tests that a long download is not mistaken for an abandoned one.

Nothing wrote to the database between "status = DOWNLOADING" and the end of the
download, so anything slower than STASHCAST_STUCK_TIMEOUT_MINUTES looked abandoned.
recover_stuck_items requeued it, the queue released it again, and the second run shared
the same tmp-<guid> directory as the first - deleting its half-written file and racing
it. The download "succeeded" but the resulting media file was truncated, and only a
manual immediate re-fetch produced a correct one.
"""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from media.models import MediaItem
from media.processing import make_download_heartbeat
from media.tasks import recover_stuck_items, release_download_queue


def downloading_item(slug='dlugie', minutes_since_update=45):
    """An item that has been downloading for a while without touching the database."""
    item = MediaItem.objects.create(
        source_url=f'https://youtu.be/{slug}',
        requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
        media_type=MediaItem.MEDIA_TYPE_AUDIO,
        slug=slug,
        title=slug,
        status=MediaItem.STATUS_DOWNLOADING,
        download_attempts=1,
    )
    MediaItem.objects.filter(pk=item.pk).update(
        updated_at=timezone.now() - timedelta(minutes=minutes_since_update)
    )
    item.refresh_from_db()
    return item


class DownloadHeartbeatTest(TestCase):
    def test_heartbeat_refreshes_updated_at(self):
        item = downloading_item()
        before = item.updated_at

        make_download_heartbeat(item)()

        item.refresh_from_db()
        self.assertGreater(item.updated_at, before)

    def test_heartbeat_is_throttled(self):
        """yt-dlp fires progress hooks constantly; one DB write per tick would hurt"""
        item = downloading_item()
        heartbeat = make_download_heartbeat(item, interval_seconds=60)

        heartbeat()
        item.refresh_from_db()
        after_first = item.updated_at

        heartbeat()
        item.refresh_from_db()

        self.assertEqual(item.updated_at, after_first)

    def test_heartbeat_touches_nothing_else(self):
        """A plain save() could clobber fields the running task is about to write"""
        item = downloading_item()
        MediaItem.objects.filter(pk=item.pk).update(title='Set by the running task')

        make_download_heartbeat(item)()

        item.refresh_from_db()
        self.assertEqual(item.title, 'Set by the running task')
        self.assertEqual(item.status, MediaItem.STATUS_DOWNLOADING)


@override_settings(STASHCAST_STUCK_TIMEOUT_MINUTES=30)
class LongDownloadNotHijackedTest(TestCase):
    def setUp(self):
        patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

    def test_download_reporting_progress_is_left_alone(self):
        """The regression: a live download must survive the recovery pass"""
        item = downloading_item(minutes_since_update=45)

        make_download_heartbeat(item)()
        recover_stuck_items()

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_DOWNLOADING)

    def test_download_with_no_progress_is_still_recovered(self):
        """A worker that really died must still be cleaned up"""
        item = downloading_item(minutes_since_update=45)

        recover_stuck_items()

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_QUEUED)

    def test_hijacked_item_gets_released_again(self):
        """Shows the full race: requeued mid-download, then handed to a second run"""
        item = downloading_item(minutes_since_update=45)

        recover_stuck_items()
        item.refresh_from_db()
        # Skip past the retry backoff, as happens while a long download continues
        MediaItem.objects.filter(pk=item.pk).update(
            next_attempt_at=timezone.now() - timedelta(minutes=1)
        )

        released = release_download_queue(limit=1)

        self.assertEqual([r.pk for r in released], [item.pk])


class ProgressHookWiringTest(TestCase):
    """The heartbeat is useless unless yt-dlp actually calls it"""

    def test_on_progress_is_registered_as_a_progress_hook(self):
        from media.service.download import _download_ytdlp_inner

        calls = []
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured['opts'] = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def download(self, urls):
                for hook in captured['opts'].get('progress_hooks', []):
                    hook({'status': 'downloading'})
                raise RuntimeError('stop before file handling')

        with patch('media.service.download.yt_dlp.YoutubeDL', FakeYDL):
            with self.assertRaises(RuntimeError):
                _download_ytdlp_inner(
                    'https://youtu.be/x',
                    'audio',
                    self._tmp(),
                    on_progress=lambda: calls.append(1),
                )

        self.assertTrue(calls, 'progress hook never reached on_progress')

    def _tmp(self):
        import shutil
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_no_hook_when_no_callback(self):
        from media.service.download import _download_ytdlp_inner

        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured['opts'] = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def download(self, urls):
                raise RuntimeError('stop')

        with patch('media.service.download.yt_dlp.YoutubeDL', FakeYDL):
            with self.assertRaises(RuntimeError):
                _download_ytdlp_inner('https://youtu.be/x', 'audio', self._tmp())

        self.assertNotIn('progress_hooks', captured['opts'])


class StalePartFileTest(TestCase):
    """A half-written file left by a killed worker must never be resumed.

    yt-dlp resumes from a .part file by default (continuedl). The cleanup used to
    preserve anything that was already on disk - which protected download.log but also
    protected stale .part files, so a retry appended the rest of the stream onto a
    beginning that could come from a different format. The file then played from the
    middle. Unlike the recovery race, this needs no long download: any hard interruption
    (container restart, OOM, deploy) leaves the .part behind.
    """

    def _tmp_with(self, *names):
        import shutil
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, path, True)
        for name in names:
            (path / name).write_bytes(b'x' * 32)
        return path

    def _run_download_in(self, tmp):
        """Run the download far enough to trigger the cleanup, then bail out."""
        from media.service.download import _download_ytdlp_inner

        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def download(self, urls):
                raise RuntimeError('stop after cleanup')

        with patch('media.service.download.yt_dlp.YoutubeDL', FakeYDL):
            with self.assertRaises(RuntimeError):
                _download_ytdlp_inner('https://youtu.be/x', 'audio', tmp)

    def test_stale_part_file_is_removed(self):
        tmp = self._tmp_with('download.log', 'download.m4a.part')

        self._run_download_in(tmp)

        self.assertFalse((tmp / 'download.m4a.part').exists())

    def test_fragment_and_ytdl_leftovers_are_removed(self):
        tmp = self._tmp_with('download.log', 'download.f140.m4a.part-Frag3', 'download.ytdl')

        self._run_download_in(tmp)

        remaining = sorted(p.name for p in tmp.iterdir())
        self.assertEqual(remaining, ['download.log'])

    def test_the_log_is_kept(self):
        """The log is written by the caller and must survive the cleanup"""
        tmp = self._tmp_with('download.log')

        self._run_download_in(tmp)

        self.assertTrue((tmp / 'download.log').exists())


class HeartbeatSurvivesABusyWorkerTest(TestCase):
    """The heartbeat must prove the worker is alive, not that it is idle.

    It used to be a periodic task only. Periodic tasks queue behind everything else, so
    a worker with a backlog - which is a worker very much alive - stopped refreshing it,
    the application declared itself dead, and new downloads were failed on sight.
    """

    def setUp(self):
        from media.tasks import _heartbeat_state, heartbeat_path

        self.path = heartbeat_path()
        if self.path.exists():
            self.path.unlink()
        _heartbeat_state['last'] = 0.0
        self.addCleanup(lambda: self.path.exists() and self.path.unlink())

    def _stop_thread(self):
        """Let the next test start a beat of its own."""
        import media.tasks as tasks

        tasks._heartbeat_thread = None

    def test_task_activity_refreshes_the_heartbeat(self):
        from media.tasks import _heartbeat_on_activity, worker_is_alive

        self.assertFalse(worker_is_alive())

        _heartbeat_on_activity('executing', None)

        self.assertTrue(worker_is_alive())

    def test_writes_are_throttled(self):
        """Signals fire constantly on a busy worker; one write each would be wasteful"""
        from media.tasks import _heartbeat_state, touch_heartbeat

        touch_heartbeat()
        first = self.path.stat().st_mtime
        before = _heartbeat_state['last']

        touch_heartbeat()

        self.assertEqual(self.path.stat().st_mtime, first)
        self.assertEqual(_heartbeat_state['last'], before)

    def test_the_heartbeat_thread_refreshes_even_when_throttled(self):
        """The thread must not be blocked by the throttle meant for signals"""
        import time

        from media.tasks import start_heartbeat_thread, touch_heartbeat, worker_is_alive

        touch_heartbeat()
        self.path.unlink()

        thread = start_heartbeat_thread(interval_seconds=60)
        self.addCleanup(self._stop_thread)
        for _ in range(50):
            if self.path.exists():
                break
            time.sleep(0.02)

        self.assertTrue(thread.is_alive())
        self.assertTrue(worker_is_alive())

    def test_a_second_start_does_not_add_another_thread(self):
        """on_startup fires once per worker thread; one beat is enough"""
        from media.tasks import start_heartbeat_thread

        first = start_heartbeat_thread(interval_seconds=60)
        self.addCleanup(self._stop_thread)

        self.assertIs(start_heartbeat_thread(interval_seconds=60), first)

    def test_the_thread_keeps_beating_while_every_worker_is_busy(self):
        """The reported failure: both threads inside a long transcription for 27 minutes,
        no task ever starts or finishes, and the signal backstop therefore says nothing"""
        import time

        from media.tasks import start_heartbeat_thread, worker_is_alive

        start_heartbeat_thread(interval_seconds=0.01)
        self.addCleanup(self._stop_thread)
        time.sleep(0.05)
        self.path.unlink()  # as if the last write had aged out
        time.sleep(0.05)

        self.assertTrue(worker_is_alive())

    def test_an_unwritable_path_does_not_raise(self):
        """Proving liveness must never be the thing that breaks a task"""
        from media.tasks import _heartbeat_state, touch_heartbeat

        _heartbeat_state['last'] = 0.0
        with patch('media.tasks.heartbeat_path') as mock_path:
            mock_path.return_value.parent.mkdir.side_effect = OSError('read-only')
            touch_heartbeat()


@override_settings(STASHCAST_WORKER_HEARTBEAT_STALE_SECONDS=180)
class WorkerUnavailableGraceTest(TestCase):
    """A new item must not be failed the instant the heartbeat looks stale"""

    def setUp(self):
        from media.tasks import heartbeat_path

        path = heartbeat_path()
        if path.exists():
            path.unlink()

    def _item(self, slug, minutes_waiting):
        item = MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug=slug,
            title=slug,
            status=MediaItem.STATUS_PREFETCHING,
        )
        MediaItem.objects.filter(pk=item.pk).update(
            updated_at=timezone.now() - timedelta(minutes=minutes_waiting)
        )
        item.refresh_from_db()
        return item

    def _read_one_event(self, item):
        from django.test import Client

        response = Client().get(f'/stash/{item.guid}/stream/')
        next(iter(response.streaming_content))

    def test_a_brand_new_item_is_not_failed(self):
        """The reported bug: "the task has been waiting 0 seconds\""""
        item = self._item('swiezy', minutes_waiting=0)

        self._read_one_event(item)

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_PREFETCHING)

    def test_an_item_waiting_far_too_long_is_still_reported(self):
        """A worker that really is down must still be surfaced"""
        item = self._item('stary', minutes_waiting=30)

        self._read_one_event(item)

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_ERROR)
        self.assertIn('Worker unavailable', item.error_message)

    def test_a_live_worker_keeps_a_long_waiting_item_alive(self):
        """Queued behind a backlog is not the same as abandoned"""
        from media.tasks import touch_heartbeat

        item = self._item('w-kolejce', minutes_waiting=30)
        touch_heartbeat(interval_seconds=0)

        self._read_one_event(item)

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_PREFETCHING)


class InterruptedTranscriptionsTest(TestCase):
    """A restart leaves nothing running, so nothing may still claim to be running"""

    def setUp(self):
        import media.tasks as tasks

        tasks._startup_cleanup_done = False
        self.addCleanup(setattr, tasks, '_startup_cleanup_done', False)

    def _item(self, slug, transcript_status):
        return MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug=slug,
            title=slug,
            status=MediaItem.STATUS_READY,
            transcript_status=transcript_status,
        )

    def test_running_items_are_released(self):
        """Otherwise the admin shows "Transcribing" forever and the item can never requeue"""
        from media.tasks import release_interrupted_transcriptions

        item = self._item('przerwany', MediaItem.TRANSCRIPT_RUNNING)

        self.assertEqual(release_interrupted_transcriptions(), 1)

        item.refresh_from_db()
        self.assertEqual(item.transcript_status, MediaItem.TRANSCRIPT_FAILED)
        self.assertIn('worker restart', item.transcript_error)
        self.assertFalse(item.is_transcribing)

    def test_finished_and_waiting_items_are_left_alone(self):
        from media.tasks import release_interrupted_transcriptions

        done = self._item('gotowy', MediaItem.TRANSCRIPT_DONE)
        waiting = self._item('czeka', MediaItem.TRANSCRIPT_QUEUED)

        release_interrupted_transcriptions()

        done.refresh_from_db()
        waiting.refresh_from_db()
        self.assertEqual(done.transcript_status, MediaItem.TRANSCRIPT_DONE)
        self.assertEqual(waiting.transcript_status, MediaItem.TRANSCRIPT_QUEUED)

    def test_it_only_runs_once_per_process(self):
        """Every worker thread calls it, but by then one of them may really be running"""
        from media.tasks import release_interrupted_transcriptions

        release_interrupted_transcriptions()
        started_since = self._item('juz-dziala', MediaItem.TRANSCRIPT_RUNNING)

        self.assertEqual(release_interrupted_transcriptions(), 0)

        started_since.refresh_from_db()
        self.assertEqual(started_since.transcript_status, MediaItem.TRANSCRIPT_RUNNING)


class TaskPriorityTest(TestCase):
    """Downloads must not queue behind hours of background transcription"""

    def _priority(self, task):
        return task.settings['default_priority']

    def test_downloads_outrank_transcription(self):
        from media.tasks import process_media, process_media_batch, transcribe_media

        self.assertGreater(self._priority(process_media), self._priority(transcribe_media))
        self.assertGreater(self._priority(process_media_batch), self._priority(transcribe_media))

    def test_the_queue_release_outranks_transcription_too(self):
        """It is the task that lets queued downloads through at all"""
        from media.tasks import process_download_queue, transcribe_media

        self.assertGreater(self._priority(process_download_queue), self._priority(transcribe_media))

    def test_a_download_is_dequeued_ahead_of_a_waiting_transcription(self):
        """The numbers only matter if the queue really reorders on them.

        The reported state was 28 transcriptions and 13 downloads in one queue, with both
        worker threads busy; whichever is handed out first decides whether anything the
        user is waiting for happens today.
        """
        import tempfile

        from huey import SqliteHuey

        from media.tasks import PRIORITY_DOWNLOAD, PRIORITY_TRANSCRIPTION

        # A file, not ':memory:': huey opens a connection per thread, and each one would
        # get a database of its own
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        queue = SqliteHuey(filename=str(Path(directory.name) / 'queue.db'), immediate=False)

        @queue.task(priority=PRIORITY_TRANSCRIPTION)
        def slow_background_work():
            pass

        @queue.task(priority=PRIORITY_DOWNLOAD)
        def what_someone_is_waiting_for():
            pass

        for _ in range(5):
            slow_background_work()
        what_someone_is_waiting_for()

        self.assertEqual(queue.dequeue().name, 'what_someone_is_waiting_for')
