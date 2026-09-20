"""
Tests for speech-to-text through a Wyoming protocol server.

The client speaks the protocol directly rather than through the asyncio ``wyoming``
package, so these tests run it against a real socket with a stand-in server: if the
framing were wrong, the stand-in could not read the events back.
"""

import json
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

    def __init__(self, reply='rozpoznany tekst', fail=False):
        self.reply = reply
        self.fail = fail
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

    def _reply(self, conn):
        data = json.dumps({'text': self.reply}).encode('utf-8')
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
        text = transcribe_pcm(b'\x00\x01' * 100, uri=self.server.uri, language='pl')

        self.assertEqual(text, 'rozpoznany tekst')

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

        item = self._item(error_message='')

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
