"""
Tests for Apple Podcasts / iOS video compatibility.

A bare "+ba" audio selector makes yt-dlp pick Opus, which cannot be muxed into MP4, so
it silently falls back to a Matroska container. Apple Podcasts plays neither, and the
episode fails on the device with "Cannot play this episode on this device".
"""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from media.models import MediaItem
from media.service.media_info import get_mime_type, is_ios_compatible_video, probe_codecs
from media.tasks import repair_video_for_item

HAS_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


class VideoFormatSpecTest(TestCase):
    """Regression guard on the default video format selector"""

    def test_audio_selector_pins_aac(self):
        """Without ext=m4a yt-dlp picks Opus and falls back to an .mkv container"""
        args = settings.STASHCAST_DEFAULT_YTDLP_ARGS_VIDEO
        self.assertIn('ba[ext=m4a]', args)

    def test_no_bare_best_audio_selector(self):
        """A bare '+ba' is what caused unplayable downloads"""
        self.assertNotIn('+ba/', settings.STASHCAST_DEFAULT_YTDLP_ARGS_VIDEO)

    def test_merges_into_mp4(self):
        self.assertIn('--merge-output-format mp4', settings.STASHCAST_DEFAULT_YTDLP_ARGS_VIDEO)


class MimeTypeTest(TestCase):
    """Tests that clients are told the truth about the container"""

    def test_known_containers(self):
        self.assertEqual(get_mime_type('a.mp4'), 'video/mp4')
        self.assertEqual(get_mime_type('a.m4a'), 'audio/mp4')
        self.assertEqual(get_mime_type('a.mp3'), 'audio/mpeg')

    def test_matroska_is_not_reported_as_mp4(self):
        """Claiming video/mp4 for an .mkv would make a client fail after downloading"""
        self.assertEqual(get_mime_type('a.mkv'), 'video/x-matroska')

    def test_webm(self):
        self.assertEqual(get_mime_type('a.webm'), 'video/webm')

    def test_unknown_extension(self):
        self.assertEqual(get_mime_type('a.whatever'), 'application/octet-stream')


class IosCompatibilityTest(TestCase):
    """Decision table for whether a file plays on Apple Podcasts as-is"""

    def _compat(self, name, video, audio):
        with patch(
            'media.service.media_info.probe_codecs',
            return_value={'video_codec': video, 'audio_codec': audio},
        ):
            return is_ios_compatible_video(name)

    def test_mp4_h264_aac_is_compatible(self):
        self.assertTrue(self._compat('a.mp4', 'h264', 'aac'))

    def test_mkv_is_rejected_on_container_alone(self):
        self.assertFalse(self._compat('a.mkv', 'h264', 'aac'))

    def test_opus_audio_is_rejected(self):
        """The exact case produced by a bare '+ba' selector"""
        self.assertFalse(self._compat('a.mp4', 'h264', 'opus'))

    def test_vp9_video_is_rejected(self):
        self.assertFalse(self._compat('a.mp4', 'vp9', 'aac'))

    def test_silent_video_is_still_compatible(self):
        """No audio track at all is fine - it just plays without sound"""
        self.assertTrue(self._compat('a.mp4', 'h264', None))

    def test_unprobeable_file_is_not_assumed_good(self):
        self.assertFalse(self._compat('a.mp4', None, None))


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not available')
class RemuxTest(TestCase):
    """Tests the real ffmpeg invocation on a file reproducing the problem"""

    def setUp(self):
        self.tmp = Path(settings.STASHCAST_MEDIA_DIR) / 'remux-test'
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _make_mkv_with_opus(self, name='problem.mkv'):
        path = self.tmp / name
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10:duration=1',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                '-c:v', 'libx264', '-preset', 'ultrafast',
                '-c:a', 'libopus',
                str(path), '-y',
            ],
            check=True,
            capture_output=True,
        )
        return path

    def test_reproduction_file_is_not_compatible(self):
        """Sanity check: the fixture really is the broken shape"""
        source = self._make_mkv_with_opus()
        self.assertEqual(probe_codecs(source), {'video_codec': 'h264', 'audio_codec': 'opus'})
        self.assertFalse(is_ios_compatible_video(source))

    def test_remux_produces_a_playable_mp4(self):
        from media.service.process import remux_to_compatible_mp4

        source = self._make_mkv_with_opus()
        target = self.tmp / 'fixed.mp4'

        remux_to_compatible_mp4(source, target)

        self.assertTrue(target.exists())
        self.assertEqual(probe_codecs(target), {'video_codec': 'h264', 'audio_codec': 'aac'})
        self.assertTrue(is_ios_compatible_video(target))

    def test_h264_video_is_copied_not_reencoded(self):
        """Re-encoding video would make repairing a large library impractical"""
        from media.service.process import remux_to_compatible_mp4

        source = self._make_mkv_with_opus()
        logs = []
        remux_to_compatible_mp4(source, self.tmp / 'fixed.mp4', logger=logs.append)

        self.assertTrue(any('-c:v copy' in line for line in logs), logs)


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not available')
class RepairVideoForItemTest(TestCase):
    """Tests the bookkeeping around repairing an item's file in place"""

    def _item(self, filename, slug='film'):
        item = MediaItem.objects.create(
            source_url='https://youtu.be/v1',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            slug=slug,
            title='Film',
            status=MediaItem.STATUS_READY,
            content_path=filename,
            mime_type='application/octet-stream',
            downloaded_at=timezone.now(),
        )
        base = Path(settings.STASHCAST_MEDIA_DIR) / slug
        base.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, base, True)
        codec = ['-c:a', 'libopus'] if filename.endswith('.mkv') else ['-c:a', 'aac']
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10:duration=1',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                '-c:v', 'libx264', '-preset', 'ultrafast',
                *codec,
                str(base / filename), '-y',
            ],
            check=True,
            capture_output=True,
        )
        item.file_size = (base / filename).stat().st_size
        item.save()
        return item

    def test_mkv_item_is_converted_and_recorded(self):
        item = self._item('content.mkv')

        self.assertTrue(repair_video_for_item(item))

        item.refresh_from_db()
        self.assertEqual(item.content_path, 'content.mp4')
        self.assertEqual(item.mime_type, 'video/mp4')
        self.assertTrue(is_ios_compatible_video(item.get_absolute_content_path()))

    def test_old_file_is_removed(self):
        """Leaving the .mkv behind would double the disk usage of a whole library"""
        item = self._item('content.mkv')
        base = Path(settings.STASHCAST_MEDIA_DIR) / 'film'

        repair_video_for_item(item)

        self.assertEqual(sorted(p.name for p in base.iterdir()), ['content.mp4'])

    def test_already_compatible_item_is_left_alone(self):
        item = self._item('content.mp4', slug='film-ok')
        before = item.file_size

        self.assertFalse(repair_video_for_item(item))

        item.refresh_from_db()
        self.assertEqual(item.file_size, before)

    def test_repair_is_idempotent(self):
        item = self._item('content.mkv')

        self.assertTrue(repair_video_for_item(item))
        item.refresh_from_db()
        self.assertFalse(repair_video_for_item(item))

    def test_audio_items_are_ignored(self):
        item = self._item('content.mkv', slug='audio-item')
        item.media_type = MediaItem.MEDIA_TYPE_AUDIO
        item.save()

        self.assertFalse(repair_video_for_item(item))

    def test_missing_file_is_reported_not_raised(self):
        item = MediaItem.objects.create(
            source_url='https://youtu.be/gone',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            slug='brak-pliku',
            title='Brak',
            status=MediaItem.STATUS_READY,
            content_path='content.mkv',
        )

        self.assertFalse(repair_video_for_item(item))


class OutputExtensionTest(TestCase):
    """The pipeline must not relabel a container it did not convert"""

    def test_video_keeps_its_real_container(self):
        """Regression: a .webm used to be renamed to content.mp4 without converting,
        which made the file and its MIME type lie about what it was."""
        from media.service.media_info import get_output_extension

        self.assertEqual(get_output_extension('video', '.webm'), '.webm')
        self.assertEqual(get_output_extension('video', '.mkv'), '.mkv')

    def test_video_mp4_stays_mp4(self):
        from media.service.media_info import get_output_extension

        self.assertEqual(get_output_extension('video', '.mp4'), '.mp4')

    def test_video_without_a_known_extension_defaults_to_mp4(self):
        from media.service.media_info import get_output_extension

        self.assertEqual(get_output_extension('video', None), '.mp4')
        self.assertEqual(get_output_extension('video', '.bin'), '.mp4')

    def test_audio_is_still_normalised(self):
        """Audio genuinely is converted by the postprocessor, so it may be renamed"""
        from media.service.media_info import get_output_extension

        self.assertEqual(get_output_extension('audio', '.opus'), '.m4a')
        self.assertEqual(get_output_extension('audio', '.mp3'), '.mp3')


class SleepIntervalTest(TestCase):
    """Rate-limit delays must compose with the defaults, not replace them"""

    @override_settings(
        STASHCAST_YTDLP_SLEEP_INTERVAL=5, STASHCAST_YTDLP_MAX_SLEEP_INTERVAL=30
    )
    def test_sleep_settings_reach_ytdlp_options(self):
        from media.service.download import apply_network_opts

        opts = apply_network_opts({'quiet': True})

        self.assertEqual(opts['sleep_interval'], 5)
        self.assertEqual(opts['max_sleep_interval'], 30)

    @override_settings(STASHCAST_YTDLP_SLEEP_INTERVAL=0)
    def test_disabled_by_default(self):
        from media.service.download import apply_network_opts

        opts = apply_network_opts({'quiet': True})

        self.assertNotIn('sleep_interval', opts)

    @override_settings(
        STASHCAST_YTDLP_SLEEP_INTERVAL=5, STASHCAST_YTDLP_MAX_SLEEP_INTERVAL=30
    )
    def test_format_selector_is_untouched(self):
        """The whole point: rate limiting no longer costs you the format defaults"""
        self.assertIn('ba[ext=m4a]', settings.STASHCAST_DEFAULT_YTDLP_ARGS_VIDEO)


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg/ffprobe not available')
class EnsurePlayableVideoTest(TestCase):
    """Tests the download-time safety net"""

    def setUp(self):
        self.tmp = Path(settings.STASHCAST_MEDIA_DIR) / 'tmp-playable'
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / 'download.log'

    def _webm_item(self, name='content.webm'):
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10:duration=1',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                '-c:v', 'libvpx-vp9', '-b:v', '50k', '-c:a', 'libopus',
                str(self.tmp / name), '-y',
            ],
            check=True,
            capture_output=True,
        )
        return MediaItem.objects.create(
            source_url='https://youtu.be/webm',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            slug='playable',
            content_path=name,
        )

    @override_settings(STASHCAST_ENSURE_PLAYABLE_VIDEO=True)
    def test_webm_download_is_repacked(self):
        """The exact production case: 'bv*+ba' yields VP9/Opus in WebM"""
        from media.processing import ensure_playable_video

        item = self._webm_item()
        ensure_playable_video(item, self.tmp, self.log)

        item.refresh_from_db()
        self.assertEqual(item.content_path, 'content.mp4')
        self.assertTrue(is_ios_compatible_video(self.tmp / item.content_path))

    @override_settings(STASHCAST_ENSURE_PLAYABLE_VIDEO=True)
    def test_no_leftover_files(self):
        from media.processing import ensure_playable_video

        item = self._webm_item()
        ensure_playable_video(item, self.tmp, self.log)

        names = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(names, ['content.mp4', 'download.log'])

    @override_settings(STASHCAST_ENSURE_PLAYABLE_VIDEO=False)
    def test_can_be_switched_off(self):
        from media.processing import ensure_playable_video

        item = self._webm_item()
        ensure_playable_video(item, self.tmp, self.log)

        item.refresh_from_db()
        self.assertEqual(item.content_path, 'content.webm')

    @override_settings(STASHCAST_ENSURE_PLAYABLE_VIDEO=True)
    def test_audio_items_are_untouched(self):
        from media.processing import ensure_playable_video

        item = self._webm_item()
        item.media_type = MediaItem.MEDIA_TYPE_AUDIO
        item.save()

        ensure_playable_video(item, self.tmp, self.log)

        item.refresh_from_db()
        self.assertEqual(item.content_path, 'content.webm')
