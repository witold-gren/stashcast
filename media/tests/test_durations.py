"""
Tests for comparing a downloaded file against the duration its source reported.

``duration_seconds`` comes from the source metadata and is the truth.
``file_duration_seconds`` is measured in the file we ended up with. A large gap means
the download was cut short, which is otherwise invisible: the item is READY, the log
says success, and only playback reveals the file is incomplete.
"""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin as dj_admin
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from media.admin import MediaItemAdmin
from media.models import MediaItem
from media.tasks import check_durations, check_item_duration

HAS_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None
User = get_user_model()


def make_item(slug, source_seconds, **kwargs):
    """A READY item claiming ``source_seconds`` of content."""
    return MediaItem.objects.create(
        source_url=f'https://youtu.be/{slug}',
        requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
        media_type=MediaItem.MEDIA_TYPE_AUDIO,
        slug=slug,
        title=slug,
        status=MediaItem.STATUS_READY,
        content_path='content.m4a',
        duration_seconds=source_seconds,
        downloaded_at=timezone.now(),
        **kwargs,
    )


class DurationGapTest(TestCase):
    """The model property both the command and the admin build on"""

    def test_gap_is_the_absolute_difference(self):
        item = make_item('a', 120, file_duration_seconds=55)
        self.assertEqual(item.duration_gap_seconds, 65)

    def test_longer_file_also_counts_as_a_gap(self):
        item = make_item('a', 60, file_duration_seconds=75)
        self.assertEqual(item.duration_gap_seconds, 15)

    def test_unmeasured_file_has_no_gap(self):
        """None means "not checked", which must never read as "fine\""""
        self.assertIsNone(make_item('a', 60).duration_gap_seconds)

    def test_missing_source_duration_has_no_gap(self):
        item = make_item('a', None, file_duration_seconds=60)
        self.assertIsNone(item.duration_gap_seconds)


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not available')
class MeasureRealFilesTest(TestCase):
    """Measures actual media, since that is the whole point of the feature"""

    def _with_audio(self, slug, source_seconds, real_seconds):
        item = make_item(slug, source_seconds)
        base = Path(settings.STASHCAST_MEDIA_DIR) / slug
        base.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, base, True)
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', f'sine=frequency=440:duration={real_seconds}',
                '-c:a', 'aac', str(base / 'content.m4a'), '-y',
            ],
            check=True,
            capture_output=True,
        )
        return item

    def test_complete_file_measures_close_to_the_source(self):
        item = self._with_audio('pelny', 10, 10)

        gap = check_item_duration(item)

        self.assertLessEqual(gap, 3)

    def test_truncated_file_is_caught(self):
        """The reported symptom: file plays for far less than the source says"""
        item = self._with_audio('uciety', 120, 20)

        gap = check_item_duration(item)

        self.assertGreater(gap, 90)

    def test_measurement_is_stored(self):
        item = self._with_audio('zapis', 10, 10)

        check_item_duration(item)

        item.refresh_from_db()
        self.assertIsNotNone(item.file_duration_seconds)
        self.assertIsNotNone(item.duration_checked_at)

    @override_settings(STASHCAST_DURATION_TOLERANCE_SECONDS=3)
    def test_check_durations_separates_good_from_bad(self):
        self._with_audio('dobry', 10, 10)
        bad = self._with_audio('zly', 120, 20)

        checked, mismatched, _ = check_durations()

        self.assertEqual(checked, 2)
        self.assertEqual([i.slug for i in mismatched], [bad.slug])

    def test_missing_file_is_skipped_not_failed(self):
        make_item('bez-pliku', 60)

        checked, mismatched, skipped = check_durations()

        self.assertEqual((checked, mismatched, skipped), (0, [], 1))


class CheckDurationsSelectionTest(TestCase):
    """Which items the sweep looks at"""

    def test_only_unchecked_skips_measured_items(self):
        make_item('zmierzony', 60, file_duration_seconds=60)

        with patch('media.tasks.check_item_duration') as mock_check:
            check_durations(only_unchecked=True)

        mock_check.assert_not_called()

    def test_limit_is_respected(self):
        for name in ('a', 'b', 'c'):
            make_item(name, 60)

        with patch('media.tasks.check_item_duration', return_value=0) as mock_check:
            check_durations(limit=2)

        self.assertEqual(mock_check.call_count, 2)

    def test_items_still_downloading_are_ignored(self):
        item = make_item('w-toku', 60)
        item.status = MediaItem.STATUS_QUEUED
        item.save()

        with patch('media.tasks.check_item_duration') as mock_check:
            check_durations()

        mock_check.assert_not_called()


@override_settings(STASHCAST_DURATION_TOLERANCE_SECONDS=3)
class DurationAdminTest(TestCase):
    """The column and filter that let you find bad files in bulk"""

    def setUp(self):
        self.admin = MediaItemAdmin(MediaItem, dj_admin.site)
        self.client = Client()
        User.objects.create_superuser('dur', 'dur@test.com', 'password')
        self.client.login(username='dur', password='password')

    def _text(self, item):
        import html
        import re

        rendered = str(self.admin.duration_check_display(item))
        return html.unescape(re.sub(r'<[^>]+>', '', rendered)).strip()

    def test_column_reports_ok_within_tolerance(self):
        self.assertEqual(self._text(make_item('a', 60, file_duration_seconds=62)), 'ok')

    def test_column_reports_the_shortfall(self):
        self.assertEqual(
            self._text(make_item('a', 120, file_duration_seconds=55)), 'short 65s'
        )

    def test_column_marks_unmeasured_items(self):
        """An empty cell would read as "fine" - it must not"""
        self.assertEqual(self._text(make_item('a', 60)), '—')

    # Slugs are deliberately unrelated words: one being a substring of another would
    # make assertNotIn pass or fail for the wrong reason.
    def test_filter_lists_only_incomplete_files(self):
        make_item('alfa', 60, file_duration_seconds=60)
        make_item('beta', 120, file_duration_seconds=55)

        html_out = self.client.get(
            '/admin/media/mediaitem/?duration_check=incomplete'
        ).content.decode()

        self.assertIn('beta', html_out)
        self.assertNotIn('alfa', html_out)

    def test_filter_lists_complete_files(self):
        make_item('alfa', 60, file_duration_seconds=60)
        make_item('beta', 120, file_duration_seconds=55)

        html_out = self.client.get('/admin/media/mediaitem/?duration_check=ok').content.decode()

        self.assertIn('alfa', html_out)
        self.assertNotIn('beta', html_out)

    def test_filter_lists_unchecked_files(self):
        make_item('gamma', 60)
        make_item('alfa', 60, file_duration_seconds=60)

        html_out = self.client.get(
            '/admin/media/mediaitem/?duration_check=unchecked'
        ).content.decode()

        self.assertIn('gamma', html_out)
        self.assertNotIn('alfa', html_out)

    def test_unmeasured_item_is_not_reported_as_complete(self):
        make_item('gamma', 60)

        html_out = self.client.get('/admin/media/mediaitem/?duration_check=ok').content.decode()

        self.assertNotIn('gamma', html_out)

    def test_action_enqueues_a_measurement(self):
        item = make_item('a', 60)

        with patch('media.tasks.check_duration_for_item') as mock_task:
            response = self.client.post(
                '/admin/media/mediaitem/',
                {'action': 'check_file_durations', '_selected_action': [item.guid]},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        mock_task.assert_called_once_with(item.guid)


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not available')
class RecordDurationAfterDownloadTest(TestCase):
    """The measurement must describe the file that is on disk right now.

    It used to be written only by ./manage.py check_durations, so re-downloading a
    truncated episode replaced the file but left the old measurement in place - the item
    stayed flagged as incomplete no matter how many times it was fetched again.
    """

    def _item_with_file(self, slug, source_seconds, real_seconds, **kwargs):
        item = make_item(slug, source_seconds, **kwargs)
        base = Path(settings.STASHCAST_MEDIA_DIR) / slug
        base.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, base, True)
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', f'sine=frequency=440:duration={real_seconds}',
                '-c:a', 'aac', str(base / 'content.m4a'), '-y',
            ],
            check=True,
            capture_output=True,
        )
        return item

    def test_stale_measurement_is_replaced(self):
        """The reported bug: a repaired file kept showing the old shortfall"""
        from media.tasks import record_download_duration

        item = self._item_with_file('naprawiony', 120, 119, file_duration_seconds=55)

        record_download_duration(item)

        item.refresh_from_db()
        self.assertEqual(item.file_duration_seconds, 119)
        self.assertLessEqual(item.duration_gap_seconds, 3)

    def test_unreadable_file_does_not_raise(self):
        """Measuring is a nicety; it must never fail an otherwise good download"""
        from media.tasks import record_download_duration

        item = make_item('bez-pliku', 60)

        record_download_duration(item, log_path=None)

        item.refresh_from_db()
        self.assertIsNone(item.file_duration_seconds)


class DurationRecordedByPipelineTest(TestCase):
    """Guards the wiring - the measurement is only useful if the pipeline runs it"""

    def test_single_download_path_records_duration(self):
        import inspect

        from media import tasks

        source = inspect.getsource(tasks.process_media.func)
        self.assertIn('record_download_duration', source)

    def test_batch_download_path_records_duration(self):
        import inspect

        from media import tasks

        source = inspect.getsource(tasks.process_media_batch.func)
        self.assertIn('record_download_duration', source)

    def test_measurement_happens_after_the_move(self):
        """get_absolute_content_path resolves against the final directory, so measuring
        before the move would probe a path that does not exist yet."""
        import inspect

        from media import tasks

        source = inspect.getsource(tasks.process_media.func)
        self.assertLess(
            source.index('shutil.move(str(tmp_dir), str(final_dir))'),
            source.index('record_download_duration'),
        )
