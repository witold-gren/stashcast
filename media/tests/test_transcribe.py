"""
Tests for speech-to-text through a Wyoming protocol server.

The client speaks the protocol directly rather than through the asyncio ``wyoming``
package, so these tests run it against a real socket with a stand-in server: if the
framing were wrong, the stand-in could not read the events back.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from media.models import MediaItem
from media.service.transcribe import (
    TranscriptionError,
    TranscriptSegment,
    _read_event,
    parse_server_uri,
    segments_to_text,
    segments_to_vtt,
    transcribe_pcm,
)

HAS_FFMPEG = shutil.which('ffmpeg') is not None
User = get_user_model()


class FakeWyomingServer:
    """A stand-in Wyoming server that records what it was sent.

    Reads with the same framing helper the client writes with, so a protocol mistake
    shows up as a failure here rather than only against real hardware.
    """

    def __init__(self, reply='rozpoznany tekst', fail=False, detected_language=None):
        self.reply = reply
        self.fail = fail
        self.detected_language = detected_language
        self.events = []
        self.audio = b''
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(('127.0.0.1', 0))
        self._socket.listen(8)
        self.host, self.port = self._socket.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def uri(self):
        return f'tcp://{self.host}:{self.port}'

    def _serve(self):
        # One connection per audio window, so keep accepting: a single-shot accept made
        # anything longer than one window hang waiting for a server that had gone away.
        while True:
            try:
                conn, _ = self._socket.accept()
            except OSError:
                return
            self._handle(conn)

    def _handle(self, conn):
        with conn, conn.makefile('rb') as stream:
            while True:
                header = stream.readline()
                if not header:
                    break
                message = json.loads(header)
                data = {}
                if message.get('data_length'):
                    data = json.loads(stream.read(message['data_length']))
                payload = b''
                if message.get('payload_length'):
                    payload = stream.read(message['payload_length'])
                    self.audio += payload
                self.events.append((message['type'], data))

                if message['type'] == 'audio-stop':
                    if not self.fail:
                        self._reply(conn)
                    break

    def requested_languages(self):
        """The language asked for on each connection, in order."""
        return [
            data.get('language')
            for event_type, data in self.events
            if event_type == 'transcribe'
        ]

    def _reply(self, conn):
        body = {'text': self.reply}
        if self.detected_language:
            body['language'] = self.detected_language
        data = json.dumps(body).encode('utf-8')
        header = {'type': 'transcript', 'version': '1.5.0', 'data_length': len(data)}
        conn.sendall(json.dumps(header).encode('utf-8') + b'\n' + data)

    def stop(self):
        self._socket.close()

    def event_types(self):
        return [event_type for event_type, _ in self.events]


class ParseServerUriTest(TestCase):
    def test_full_uri(self):
        self.assertEqual(parse_server_uri('tcp://whisper:10300'), ('whisper', 10300))

    def test_host_and_port(self):
        self.assertEqual(parse_server_uri('192.168.1.10:10300'), ('192.168.1.10', 10300))

    def test_bare_host_defaults_to_the_wyoming_port(self):
        self.assertEqual(parse_server_uri('whisper'), ('whisper', 10300))

    def test_empty_is_rejected(self):
        with self.assertRaises(TranscriptionError):
            parse_server_uri('')


class TranscribePcmTest(TestCase):
    """Runs the real client against the stand-in server over a real socket"""

    def setUp(self):
        self.server = FakeWyomingServer()
        self.addCleanup(self.server.stop)

    def test_returns_the_text_the_server_sent(self):
        text, _ = transcribe_pcm(b'\x00\x01' * 100, uri=self.server.uri, language='pl')

        self.assertEqual(text, 'rozpoznany tekst')

    def test_returns_the_language_the_server_reports(self):
        server = FakeWyomingServer(detected_language='pl')
        self.addCleanup(server.stop)

        _, language = transcribe_pcm(b'\x00\x01' * 10, uri=server.uri)

        self.assertEqual(language, 'pl')

    def test_sends_the_expected_event_sequence(self):
        transcribe_pcm(b'\x00\x01' * 100, uri=self.server.uri, language='pl')

        self.assertEqual(
            self.server.event_types(),
            ['transcribe', 'audio-start', 'audio-chunk', 'audio-stop'],
        )

    def test_language_reaches_the_server(self):
        transcribe_pcm(b'\x00\x01' * 10, uri=self.server.uri, language='pl')

        transcribe_event = dict(self.server.events)['transcribe']
        self.assertEqual(transcribe_event['language'], 'pl')

    def test_audio_arrives_intact(self):
        """Chunking must not lose or reorder a single byte"""
        pcm = bytes(range(256)) * 200  # larger than one chunk

        transcribe_pcm(pcm, uri=self.server.uri)

        self.assertEqual(self.server.audio, pcm)

    def test_audio_format_is_announced(self):
        transcribe_pcm(b'\x00\x01' * 10, uri=self.server.uri)

        start = dict(self.server.events)['audio-start']
        self.assertEqual((start['rate'], start['width'], start['channels']), (16000, 2, 1))

    def test_unreachable_server_is_reported_clearly(self):
        with self.assertRaises(TranscriptionError) as caught:
            transcribe_pcm(b'\x00', uri='tcp://127.0.0.1:1', timeout=2)

        self.assertIn('Cannot reach', str(caught.exception))

    def test_server_hanging_up_is_reported(self):
        server = FakeWyomingServer(fail=True)
        self.addCleanup(server.stop)

        with self.assertRaises(TranscriptionError):
            transcribe_pcm(b'\x00\x01' * 10, uri=server.uri)


class ReadEventTest(TestCase):
    def test_rejects_a_malformed_header(self):
        import io

        with self.assertRaises(TranscriptionError):
            _read_event(io.BytesIO(b'not json\n'))

    def test_returns_none_at_end_of_stream(self):
        import io

        self.assertIsNone(_read_event(io.BytesIO(b'')))


class VttRenderingTest(TestCase):
    def test_cues_carry_the_window_timings(self):
        vtt = segments_to_vtt(
            [TranscriptSegment(0, 30, 'pierwszy'), TranscriptSegment(30, 60, 'drugi')]
        )

        self.assertTrue(vtt.startswith('WEBVTT'))
        self.assertIn('00:00:00.000 --> 00:00:30.000', vtt)
        self.assertIn('00:00:30.000 --> 00:01:00.000', vtt)
        self.assertIn('pierwszy', vtt)

    def test_hours_are_formatted(self):
        vtt = segments_to_vtt([TranscriptSegment(3661, 3691, 'późno')])

        self.assertIn('01:01:01.000 --> 01:01:31.000', vtt)

    def test_plain_text_joins_the_segments(self):
        text = segments_to_text(
            [TranscriptSegment(0, 30, 'pierwszy'), TranscriptSegment(30, 60, 'drugi')]
        )

        self.assertEqual(text, 'pierwszy\ndrugi')

    def test_empty_transcript_is_valid_vtt(self):
        self.assertEqual(segments_to_vtt([]).strip(), 'WEBVTT')


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg not available')
class TranscribeItemTest(TestCase):
    """End to end: real audio, real socket, stored results"""

    def setUp(self):
        self.server = FakeWyomingServer()
        self.addCleanup(self.server.stop)

    def _item(self, slug='odcinek', seconds=4):
        item = MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug=slug,
            title='Odcinek',
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            duration_seconds=seconds,
            downloaded_at=timezone.now(),
        )
        base = Path(settings.STASHCAST_MEDIA_DIR) / slug
        base.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, base, True)
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
                '-c:a', 'aac', str(base / 'content.m4a'), '-y',
            ],
            check=True,
            capture_output=True,
        )
        return item

    @override_settings(STASHCAST_WHISPER_ENABLED=True, STASHCAST_WHISPER_WINDOW_SECONDS=5)
    def test_transcript_is_stored_on_the_item(self):
        from media.tasks import transcribe_item

        item = self._item()

        with override_settings(STASHCAST_WHISPER_URI=self.server.uri):
            transcribe_item(item)

        item.refresh_from_db()
        self.assertIn('rozpoznany tekst', item.transcript)
        self.assertEqual(item.transcript_path, 'transcript.vtt')
        self.assertIsNotNone(item.transcript_created_at)

    @override_settings(STASHCAST_WHISPER_ENABLED=True, STASHCAST_WHISPER_WINDOW_SECONDS=5)
    def test_vtt_file_is_written_next_to_the_media(self):
        from media.tasks import transcribe_item

        item = self._item()

        with override_settings(STASHCAST_WHISPER_URI=self.server.uri):
            transcribe_item(item)

        item.refresh_from_db()
        written = item.get_absolute_transcript_path().read_text(encoding='utf-8')
        self.assertTrue(written.startswith('WEBVTT'))
        self.assertIn('rozpoznany tekst', written)

    @override_settings(STASHCAST_WHISPER_ENABLED=False)
    def test_refuses_to_run_when_switched_off(self):
        from media.tasks import transcribe_item

        with self.assertRaises(TranscriptionError):
            transcribe_item(self._item())

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_missing_media_file_is_reported(self):
        from media.tasks import transcribe_item

        item = MediaItem.objects.create(
            source_url='https://youtu.be/brak',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='brak',
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
        )

        with self.assertRaises(TranscriptionError):
            transcribe_item(item)

    def _video_item(self, slug='film', seconds=8):
        """A real video item: H.264 picture plus an AAC soundtrack."""
        item = MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            slug=slug,
            title='Film',
            status=MediaItem.STATUS_READY,
            content_path='content.mp4',
            duration_seconds=seconds,
            downloaded_at=timezone.now(),
        )
        base = Path(settings.STASHCAST_MEDIA_DIR) / slug
        base.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, base, True)
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', f'testsrc=size=160x120:rate=10:duration={seconds}',
                '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                str(base / 'content.mp4'), '-y',
            ],
            check=True,
            capture_output=True,
        )
        return item

    @override_settings(STASHCAST_WHISPER_ENABLED=True, STASHCAST_WHISPER_WINDOW_SECONDS=5)
    def test_video_files_are_transcribed_too(self):
        """Whisper only takes audio, so the soundtrack has to be pulled out of the video"""
        from media.tasks import transcribe_item

        item = self._video_item()

        with override_settings(STASHCAST_WHISPER_URI=self.server.uri):
            segments = transcribe_item(item)

        self.assertEqual(segments, 2)
        item.refresh_from_db()
        self.assertIn('rozpoznany tekst', item.transcript)

    @override_settings(STASHCAST_WHISPER_ENABLED=True, STASHCAST_WHISPER_WINDOW_SECONDS=5)
    def test_last_window_stops_at_the_real_end(self):
        """An 8 second file must not claim a cue running to 10 seconds"""
        from media.tasks import transcribe_item

        item = self._video_item(seconds=8)

        with override_settings(STASHCAST_WHISPER_URI=self.server.uri):
            transcribe_item(item)

        item.refresh_from_db()
        vtt = item.get_absolute_transcript_path().read_text(encoding='utf-8')
        self.assertIn('00:00:05.000 --> 00:00:08.000', vtt)
        self.assertNotIn('00:00:10.000', vtt)

    def test_audio_is_extracted_from_a_video_container(self):
        """Guards the ffmpeg call itself: video in, raw PCM out"""
        from media.service.transcribe import SAMPLE_RATE, SAMPLE_WIDTH, extract_pcm

        item = self._video_item(seconds=8)

        pcm = extract_pcm(item.get_absolute_content_path(), start_seconds=0, duration_seconds=4)

        seconds = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
        self.assertAlmostEqual(seconds, 4.0, delta=0.2)

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_subtitles_are_not_overwritten(self):
        """A generated transcript must not clobber subtitles that came with the video"""
        from media.tasks import transcribe_item

        item = self._item()
        item.subtitle_path = 'subtitles.vtt'
        item.save()

        with override_settings(STASHCAST_WHISPER_URI=self.server.uri):
            transcribe_item(item)

        item.refresh_from_db()
        self.assertEqual(item.subtitle_path, 'subtitles.vtt')
        self.assertEqual(item.transcript_path, 'transcript.vtt')


class TranscriptAdminTest(TestCase):
    def setUp(self):
        self.client = Client()
        User.objects.create_superuser('tr', 'tr@test.com', 'password')
        self.client.login(username='tr', password='password')
        self.item = MediaItem.objects.create(
            source_url='https://youtu.be/a',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='a',
            title='Odcinek',
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
        )

    def _run_action(self):
        return self.client.post(
            '/admin/media/mediaitem/',
            {'action': 'create_transcripts', '_selected_action': [self.item.guid]},
            follow=True,
        )

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_action_enqueues_the_work(self):
        with patch('media.tasks.transcribe_media') as mock_task:
            response = self._run_action()

        self.assertEqual(response.status_code, 200)
        mock_task.assert_called_once_with(self.item.guid)

    @override_settings(STASHCAST_WHISPER_ENABLED=False)
    def test_action_explains_when_the_feature_is_off(self):
        """Silently doing nothing would look like a broken action"""
        with patch('media.tasks.transcribe_media') as mock_task:
            response = self._run_action()

        mock_task.assert_not_called()
        self.assertContains(response, 'STASHCAST_WHISPER_ENABLED')


class TranscriptFeedTest(TestCase):
    """The transcript is only useful if podcast clients are told about it"""

    def _ready_item(self, **kwargs):
        return MediaItem.objects.create(
            source_url='https://youtu.be/a',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug='odcinek',
            title='Odcinek',
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            file_size=1234,
            downloaded_at=timezone.now(),
            **kwargs,
        )

    @override_settings(STASHCAST_SUBTITLE_LANGUAGE='pl')
    def test_feed_announces_the_transcript(self):
        self._ready_item(transcript_path='transcript.vtt', transcript='tekst')

        xml = Client().get('/feeds/audio.xml').content.decode()

        self.assertIn('podcast:transcript', xml)
        self.assertIn('transcript.vtt', xml)
        self.assertIn('text/vtt', xml)

    @override_settings(STASHCAST_SUBTITLE_LANGUAGE='pl')
    def test_language_is_not_hardcoded_to_english(self):
        self._ready_item(transcript_path='transcript.vtt', transcript='tekst')

        xml = Client().get('/feeds/audio.xml').content.decode()

        self.assertIn('language="pl"', xml)

    def test_generated_transcript_wins_over_subtitles(self):
        self._ready_item(transcript_path='transcript.vtt', subtitle_path='subtitles.vtt')

        xml = Client().get('/feeds/audio.xml').content.decode()

        self.assertIn('transcript.vtt', xml)
        self.assertNotIn('subtitles.vtt', xml)

    def test_subtitles_are_still_used_when_there_is_no_transcript(self):
        self._ready_item(subtitle_path='subtitles.vtt')

        xml = Client().get('/feeds/audio.xml').content.decode()

        self.assertIn('subtitles.vtt', xml)


class TranscriptStatusTest(TestCase):
    """Where an item is in the transcription process must be visible at every stage.

    Without this the admin action was a black box: no way to tell what was waiting,
    what was running, what had finished, or why something produced nothing.
    """

    def setUp(self):
        self.admin = __import__(
            'media.admin', fromlist=['MediaItemAdmin']
        ).MediaItemAdmin(MediaItem, __import__('django.contrib.admin', fromlist=['site']).site)

    def _item(self, slug='a', **kwargs):
        return MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug=slug,
            title=slug,
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            **kwargs,
        )

    def _column(self, item):
        import html as html_module
        import re

        rendered = str(self.admin.transcript_display(item))
        return html_module.unescape(re.sub(r'<[^>]+>', '', rendered)).strip()

    def test_queue_transcription_marks_items_as_waiting(self):
        from media.tasks import queue_transcription

        item = self._item()

        with patch('media.tasks.transcribe_media'):
            queued = queue_transcription([item])

        self.assertEqual(queued, 1)
        item.refresh_from_db()
        self.assertEqual(item.transcript_status, MediaItem.TRANSCRIPT_QUEUED)

    def test_column_shows_each_stage(self):
        self.assertEqual(
            self._column(self._item('q', transcript_status=MediaItem.TRANSCRIPT_QUEUED)),
            '⏳ waiting',
        )
        self.assertEqual(
            self._column(self._item('r', transcript_status=MediaItem.TRANSCRIPT_RUNNING)),
            '● transcribing',
        )
        self.assertEqual(
            self._column(self._item('f', transcript_status=MediaItem.TRANSCRIPT_FAILED)),
            'failed',
        )
        self.assertEqual(
            self._column(self._item('d', transcript='jeden dwa trzy')), '3 words'
        )
        self.assertEqual(self._column(self._item('n')), '—')

    def test_failure_does_not_touch_the_download_error(self):
        """A good download whose transcription failed must not look like a bad download"""
        from media.tasks import transcribe_media

        # Queued, because that is the only state from which the task does any work -
        # anything else means it was taken out of the queue while waiting
        item = self._item(error_message='', transcript_status=MediaItem.TRANSCRIPT_QUEUED)

        with patch('media.tasks.transcribe_item', side_effect=RuntimeError('whisper down')):
            transcribe_media.call_local(item.guid)

        item.refresh_from_db()
        self.assertEqual(item.transcript_status, MediaItem.TRANSCRIPT_FAILED)
        self.assertIn('whisper down', item.transcript_error)
        self.assertEqual(item.error_message, '')

    def test_is_transcribing_covers_waiting_and_running(self):
        self.assertTrue(
            self._item('q', transcript_status=MediaItem.TRANSCRIPT_QUEUED).is_transcribing
        )
        self.assertTrue(
            self._item('r', transcript_status=MediaItem.TRANSCRIPT_RUNNING).is_transcribing
        )
        self.assertFalse(
            self._item('d', transcript_status=MediaItem.TRANSCRIPT_DONE).is_transcribing
        )


class TranscriptVisibilityTest(TestCase):
    """The text has to be readable, not just counted"""

    def setUp(self):
        self.client = Client()
        User.objects.create_superuser('vis', 'vis@test.com', 'password')
        self.client.login(username='vis', password='password')

    def _item(self, **kwargs):
        return MediaItem.objects.create(
            source_url='https://youtu.be/a',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='a',
            title='Odcinek',
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            **kwargs,
        )

    def test_transcript_text_is_shown_on_the_item_page(self):
        item = self._item(transcript='To jest treść transkrypcji')

        html_out = self.client.get(f'/admin/media/mediaitem/{item.guid}/change/').content.decode()

        self.assertIn('treść transkrypcji', html_out)

    def test_failure_reason_is_shown_on_the_item_page(self):
        item = self._item(
            transcript_status=MediaItem.TRANSCRIPT_FAILED,
            transcript_error='Cannot reach the server at whisper:10300',
        )

        html_out = self.client.get(f'/admin/media/mediaitem/{item.guid}/change/').content.decode()

        self.assertIn('Cannot reach the server', html_out)

    def test_filter_lists_what_is_waiting(self):
        self._item(transcript_status=MediaItem.TRANSCRIPT_QUEUED)

        html_out = self.client.get(
            '/admin/media/mediaitem/?transcript_status__exact=QUEUED'
        ).content.decode()

        self.assertIn('Odcinek', html_out)

    def test_filter_lists_what_is_running(self):
        self._item(transcript_status=MediaItem.TRANSCRIPT_RUNNING)

        html_out = self.client.get(
            '/admin/media/mediaitem/?transcript_status__exact=RUNNING'
        ).content.decode()

        self.assertIn('Odcinek', html_out)


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg not available')
class LanguageConsistencyTest(TestCase):
    """The whole transcript must come out in one language.

    Whisper decides the language per request. Left to itself it heard Polish in the
    first window and then started translating the following ones into English, so a
    single episode came back half Polish, half English. Whatever the first window
    reports is therefore pinned for every window after it.
    """

    def _audio_item(self, seconds=12):
        base = Path(settings.STASHCAST_MEDIA_DIR) / 'jezyk'
        base.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, base, True)
        subprocess.run(
            [
                'ffmpeg', '-v', 'error',
                '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
                '-c:a', 'aac', str(base / 'content.m4a'), '-y',
            ],
            check=True,
            capture_output=True,
        )
        return base / 'content.m4a'

    def test_detected_language_is_reused_for_later_windows(self):
        from media.service.transcribe import transcribe_file

        server = FakeWyomingServer(detected_language='pl')
        self.addCleanup(server.stop)
        path = self._audio_item(seconds=12)

        transcribe_file(path, uri=server.uri, language=None, window_seconds=5)

        asked = server.requested_languages()
        self.assertEqual(len(asked), 3)
        # First window asks for nothing, the rest are pinned to what came back
        self.assertIsNone(asked[0])
        self.assertEqual(asked[1:], ['pl', 'pl'])

    def test_configured_language_is_used_for_every_window(self):
        from media.service.transcribe import transcribe_file

        server = FakeWyomingServer(detected_language='en')
        self.addCleanup(server.stop)
        path = self._audio_item(seconds=12)

        transcribe_file(path, uri=server.uri, language='pl', window_seconds=5)

        self.assertEqual(server.requested_languages(), ['pl', 'pl', 'pl'])

    def test_server_reporting_no_language_is_harmless(self):
        from media.service.transcribe import transcribe_file

        server = FakeWyomingServer()
        self.addCleanup(server.stop)
        path = self._audio_item(seconds=12)

        segments = transcribe_file(path, uri=server.uri, language=None, window_seconds=5)

        self.assertEqual(len(segments), 3)


class WhisperLanguageDefaultTest(TestCase):
    """The site language must not be mistaken for the language people speak"""

    def test_default_is_auto_detect_not_the_interface_language(self):
        """Inheriting LANGUAGE_CODE told Whisper that Polish episodes were English"""
        self.assertEqual(
            os.environ.get('STASHCAST_WHISPER_LANGUAGE', ''),
            '',
            'test environment should not pin the language',
        )
        self.assertEqual(settings.STASHCAST_WHISPER_LANGUAGE, '')


class GroupTranscriptionTest(TestCase):
    """Per-group opt-in for transcribing new downloads, plus the group-wide action"""

    def setUp(self):
        from media.models import MediaGroup

        self.MediaGroup = MediaGroup
        self.client = Client()
        User.objects.create_superuser('grp', 'grp@test.com', 'password')
        self.client.login(username='grp', password='password')

    def _group(self, name='Grupa', **kwargs):
        return self.MediaGroup.objects.create(name=name, **kwargs)

    def _item(self, slug, group=None):
        return MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug=slug,
            title=slug,
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            group=group,
        )

    def test_the_flag_is_off_by_default(self):
        """Transcribing costs real time, so a group must ask for it"""
        self.assertFalse(self._group().transcribe_new_downloads)

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_new_download_in_an_enabled_group_is_queued(self):
        from media.tasks import queue_transcription_if_enabled

        item = self._item('a', self._group(transcribe_new_downloads=True))

        with patch('media.tasks.transcribe_media'):
            self.assertTrue(queue_transcription_if_enabled(item))

        item.refresh_from_db()
        self.assertEqual(item.transcript_status, MediaItem.TRANSCRIPT_QUEUED)

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_disabled_group_is_left_alone(self):
        from media.tasks import queue_transcription_if_enabled

        item = self._item('a', self._group(transcribe_new_downloads=False))

        with patch('media.tasks.transcribe_media') as mock_task:
            self.assertFalse(queue_transcription_if_enabled(item))

        mock_task.assert_not_called()

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_item_without_a_group_is_left_alone(self):
        from media.tasks import queue_transcription_if_enabled

        self.assertFalse(queue_transcription_if_enabled(self._item('a')))

    @override_settings(STASHCAST_WHISPER_ENABLED=False)
    def test_global_switch_still_wins(self):
        from media.tasks import queue_transcription_if_enabled

        item = self._item('a', self._group(transcribe_new_downloads=True))

        self.assertFalse(queue_transcription_if_enabled(item))

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_a_transcription_failure_never_breaks_the_download(self):
        """The download task treats an exception here as a failed download, so this
        must swallow its own errors: a transcript is a bonus, not worth the episode."""
        from media.tasks import queue_transcription_if_enabled

        item = self._item('a', self._group(transcribe_new_downloads=True))

        with patch('media.tasks.queue_transcription', side_effect=RuntimeError('whisper down')):
            queued = queue_transcription_if_enabled(item)

        self.assertFalse(queued)
        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_READY)
        self.assertEqual(item.content_path, 'content.m4a')

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_group_action_transcribes_everything_in_the_group(self):
        group = self._group()
        self._item('a', group)
        self._item('b', group)
        self._item('c')  # different group - must not be touched

        with patch('media.tasks.transcribe_media') as mock_task:
            response = self.client.post(
                '/admin/media/mediagroup/',
                {'action': 'transcribe_group_items', '_selected_action': [group.pk]},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_task.call_count, 2)

    @override_settings(STASHCAST_WHISPER_ENABLED=False)
    def test_group_action_explains_when_switched_off(self):
        group = self._group()
        self._item('a', group)

        with patch('media.tasks.transcribe_media') as mock_task:
            response = self.client.post(
                '/admin/media/mediagroup/',
                {'action': 'transcribe_group_items', '_selected_action': [group.pk]},
                follow=True,
            )

        mock_task.assert_not_called()
        self.assertContains(response, 'STASHCAST_WHISPER_ENABLED')

    @override_settings(STASHCAST_WHISPER_ENABLED=True)
    def test_download_pipeline_calls_the_hook(self):
        """Guards the wiring, not just the helper"""
        import inspect

        from media import tasks

        self.assertIn(
            'queue_transcription_if_enabled', inspect.getsource(tasks.process_media.func)
        )
        self.assertIn(
            'queue_transcription_if_enabled',
            inspect.getsource(tasks.process_media_batch.func),
        )


class FeedTimelineTest(TestCase):
    """What Apple needs to line a timed transcript up against the audio"""

    def _item(self, **kwargs):
        return MediaItem.objects.create(
            source_url='https://youtu.be/a',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug='odcinek',
            title='Odcinek',
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            file_size=1234,
            downloaded_at=timezone.now(),
            **kwargs,
        )

    def _feed(self):
        return Client().get('/feeds/audio.xml').content.decode()

    def test_episode_duration_is_published(self):
        """Without it the feed gives a timed transcript no timeline to attach to"""
        self._item(duration_seconds=1272)

        self.assertIn('<itunes:duration>21:12</itunes:duration>', self._feed())

    def test_duration_over_an_hour_includes_hours(self):
        self._item(duration_seconds=3661)

        self.assertIn('<itunes:duration>1:01:01</itunes:duration>', self._feed())

    def test_measured_duration_is_used_when_the_source_gave_none(self):
        self._item(duration_seconds=None, file_duration_seconds=95)

        self.assertIn('<itunes:duration>1:35</itunes:duration>', self._feed())

    def test_no_duration_tag_when_the_length_is_unknown(self):
        self._item()

        self.assertNotIn('itunes:duration', self._feed())

    @override_settings(STASHCAST_WHISPER_LANGUAGE='pl', STASHCAST_SUBTITLE_LANGUAGE='en')
    def test_generated_transcript_declares_the_whisper_language(self):
        """The transcript is in the language Whisper worked in, not the interface one"""
        self._item(duration_seconds=60, transcript_path='transcript.vtt', transcript='tekst')

        self.assertIn('language="pl"', self._feed())

    @override_settings(STASHCAST_WHISPER_LANGUAGE='pl', STASHCAST_SUBTITLE_LANGUAGE='en')
    def test_downloaded_subtitles_keep_the_subtitle_language(self):
        self._item(duration_seconds=60, subtitle_path='subtitles.vtt')

        self.assertIn('language="en"', self._feed())


@override_settings(STASHCAST_TRANSCRIPT_HEADING='Transkrypcja')
class TranscriptInDescriptionTest(TestCase):
    """Merging the transcript into the description is decided per group.

    It suits short episodes and ruins long ones, so it is a property of the group rather
    than a global switch. The merge happens only while publishing: the database and the
    admin keep description and transcript apart.
    """

    def _item(self, merge=False, grouped=True, **kwargs):
        from media.models import MediaGroup

        group = None
        if grouped:
            group = MediaGroup.objects.create(
                name='Grupa', transcript_in_description=merge
            )
        return MediaItem.objects.create(
            source_url='https://youtu.be/a',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug='odcinek',
            title='Odcinek',
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            file_size=10,
            description='Zwykly opis.',
            group=group,
            downloaded_at=timezone.now(),
            **kwargs,
        )

    def _item_description(self):
        import xml.etree.ElementTree as ET

        xml = Client().get('/feeds/audio.xml').content.decode()
        for item in ET.fromstring(xml).find('channel').findall('item'):
            if item.findtext('title') == 'Odcinek':
                return item.findtext('description') or ''
        return ''

    def test_group_flag_is_off_by_default(self):
        from media.models import MediaGroup

        self.assertFalse(MediaGroup.objects.create(name='Nowa').transcript_in_description)

    def test_group_with_the_flag_off_keeps_the_plain_description(self):
        self._item(merge=False, transcript='tekst transkrypcji')

        self.assertEqual(self._item_description(), 'Zwykly opis.')

    def test_group_with_the_flag_on_gets_the_merged_description(self):
        self._item(merge=True, transcript='tekst transkrypcji')

        description = self._item_description()

        self.assertIn('Zwykly opis.', description)
        self.assertIn('tekst transkrypcji', description)
        self.assertIn('Transkrypcja', description)

    def test_item_without_a_group_is_never_merged(self):
        """There is no group to ask, so the safe answer is no"""
        self._item(grouped=False, transcript='tekst transkrypcji')

        self.assertEqual(self._item_description(), 'Zwykly opis.')

    def test_stored_fields_are_never_modified(self):
        """The merge is a publishing step, not an edit"""
        item = self._item(merge=True, transcript='tekst transkrypcji')

        self._item_description()

        item.refresh_from_db()
        self.assertEqual(item.description, 'Zwykly opis.')
        self.assertEqual(item.transcript, 'tekst transkrypcji')

    def test_vtt_tag_is_published_regardless_of_the_flag(self):
        """Apps that understand the tag must keep the timed version either way"""
        self._item(merge=False, transcript='tekst', transcript_path='transcript.vtt')

        xml = Client().get('/feeds/audio.xml').content.decode()

        self.assertIn('podcast:transcript', xml)
        self.assertIn('transcript.vtt', xml)

    def test_window_lines_are_joined_into_running_text(self):
        """Stored transcripts hold one line per audio window, which reads as a column"""
        self._item(merge=True, transcript='pierwsze zdanie\ndrugie zdanie\ntrzecie')

        self.assertIn('pierwsze zdanie drugie zdanie trzecie', self._item_description())

    def test_item_without_a_transcript_is_unchanged(self):
        self._item(merge=True)

        self.assertEqual(self._item_description(), 'Zwykly opis.')

    @override_settings(STASHCAST_TRANSCRIPT_IN_DESCRIPTION_MAX_CHARS=20)
    def test_limit_trims_on_a_word_boundary(self):
        """A full transcript in every item can add megabytes to the feed"""
        self._item(merge=True, transcript='jedno dwa trzy cztery piec szesc siedem osiem')

        description = self._item_description()

        self.assertIn('…', description)
        self.assertNotIn('siedem', description)

    @override_settings(STASHCAST_TRANSCRIPT_IN_DESCRIPTION_MAX_CHARS=0)
    def test_zero_means_no_limit(self):
        self._item(merge=True, transcript='jedno dwa trzy cztery piec szesc siedem osiem')

        self.assertIn('osiem', self._item_description())
