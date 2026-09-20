"""
Speech-to-text through a Wyoming protocol server (e.g. wyoming-whisper).

Wyoming is a small line-based TCP protocol: every message is a JSON header line,
optionally followed by a JSON data block and a binary payload, each with its length
announced in the header:

    {"type": "audio-chunk", "version": "1.5.0", "data_length": 61, "payload_length": 4096}\\n
    <61 bytes of JSON data>
    <4096 bytes of PCM>

A transcription is: ``transcribe`` -> ``audio-start`` -> N x ``audio-chunk`` ->
``audio-stop``, and the server answers with a single ``transcript`` event.

The client here is deliberately synchronous. The official ``wyoming`` package is
asyncio-based, and the rest of this application - Django and the Huey worker threads -
is not; bridging an event loop into a worker thread would buy nothing for a protocol
this small.

Whisper returns one block of text per request with no timings, so audio is fed to it in
fixed windows and the window boundaries become the cue timings of the resulting WebVTT.
"""

import json
import socket
import subprocess
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

# What wyoming-whisper expects: 16 kHz mono signed 16-bit little-endian PCM
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1

# Wyoming header keys
_TYPE = 'type'
_DATA = 'data'
_DATA_LENGTH = 'data_length'
_PAYLOAD_LENGTH = 'payload_length'
_VERSION = 'version'
_VERSION_NUMBER = '1.5.0'

# How much PCM to put in a single audio-chunk message
_CHUNK_BYTES = 16384


class TranscriptionError(Exception):
    """Raised when the speech-to-text server could not be used."""


@dataclass
class TranscriptSegment:
    """One transcribed window of audio."""

    start_seconds: float
    end_seconds: float
    text: str


def parse_server_uri(uri):
    """
    Split a Wyoming server address into host and port.

    Args:
        uri: 'tcp://host:port', 'host:port' or just 'host' (port defaults to 10300)

    Returns:
        tuple[str, int]: (host, port)

    Raises:
        TranscriptionError: When the address cannot be understood.
    """
    text = (uri or '').strip()
    if not text:
        raise TranscriptionError('No speech-to-text server configured')

    if '://' not in text:
        text = f'tcp://{text}'

    parsed = urlparse(text)
    if not parsed.hostname:
        raise TranscriptionError(f'Could not read a host from {uri!r}')

    return parsed.hostname, parsed.port or 10300


def _write_event(sock, event_type, data=None, payload=None):
    """Send one Wyoming event."""
    header = {_TYPE: event_type, _VERSION: _VERSION_NUMBER}

    data_bytes = None
    if data:
        data_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')
        header[_DATA_LENGTH] = len(data_bytes)
    if payload:
        header[_PAYLOAD_LENGTH] = len(payload)

    sock.sendall(json.dumps(header, ensure_ascii=False).encode('utf-8') + b'\n')
    if data_bytes:
        sock.sendall(data_bytes)
    if payload:
        sock.sendall(payload)


def _read_event(stream):
    """Read one Wyoming event.

    Args:
        stream: A buffered binary file object over the socket

    Returns:
        tuple[str, dict] or None: (event type, data) - None when the peer hung up.
    """
    line = stream.readline()
    if not line:
        return None

    try:
        header = json.loads(line)
    except json.JSONDecodeError as e:
        raise TranscriptionError(f'Malformed response from the server: {e}') from e

    data = {}
    data_length = header.get(_DATA_LENGTH)
    if data_length:
        data = json.loads(stream.read(data_length))

    payload_length = header.get(_PAYLOAD_LENGTH)
    if payload_length:
        # Payloads only travel towards the server in this flow; drain to stay in sync
        stream.read(payload_length)

    return header.get(_TYPE, ''), data


def transcribe_pcm(pcm, uri, language=None, timeout=300):
    """
    Send raw PCM audio to the server and return the text it heard.

    Args:
        pcm: Raw 16 kHz mono signed 16-bit little-endian audio
        uri: Server address, e.g. 'tcp://whisper:10300'
        language: Language hint, e.g. 'pl'. None lets the server decide.
        timeout: Socket timeout in seconds

    Returns:
        tuple[str, str|None]: The text (empty when the window held no speech) and the
        language the server reports having heard, when it reports one.

    Raises:
        TranscriptionError: On connection problems or an unusable answer.
    """
    host, port = parse_server_uri(uri)
    audio_header = {'rate': SAMPLE_RATE, 'width': SAMPLE_WIDTH, 'channels': CHANNELS}

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)

            _write_event(sock, 'transcribe', {'language': language} if language else {})
            _write_event(sock, 'audio-start', {**audio_header, 'timestamp': 0})

            for offset in range(0, len(pcm), _CHUNK_BYTES):
                _write_event(
                    sock,
                    'audio-chunk',
                    audio_header,
                    payload=pcm[offset : offset + _CHUNK_BYTES],
                )

            _write_event(sock, 'audio-stop', {'timestamp': len(pcm)})

            with sock.makefile('rb') as stream:
                while True:
                    event = _read_event(stream)
                    if event is None:
                        raise TranscriptionError(
                            'Server closed the connection without returning a transcript'
                        )
                    event_type, data = event
                    if event_type == 'transcript':
                        return (data.get('text') or '').strip(), data.get('language')
    except OSError as e:
        raise TranscriptionError(f'Cannot reach the speech-to-text server at {host}:{port}: {e}')


def extract_pcm(path, start_seconds=None, duration_seconds=None):
    """
    Decode part of a media file into the raw PCM the server expects.

    Args:
        path: Media file to read
        start_seconds: Where to start (None = the beginning)
        duration_seconds: How much to take (None = to the end)

    Returns:
        bytes: Raw PCM audio.

    Raises:
        TranscriptionError: When ffmpeg fails.
    """
    command = ['ffmpeg', '-v', 'error']
    if start_seconds is not None:
        # Before -i so ffmpeg seeks instead of decoding everything up to that point
        command += ['-ss', str(start_seconds)]
    command += ['-i', str(path)]
    if duration_seconds is not None:
        command += ['-t', str(duration_seconds)]
    command += [
        '-vn',
        '-ac', str(CHANNELS),
        '-ar', str(SAMPLE_RATE),
        '-f', 's16le',
        '-',
    ]

    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        tail = (result.stderr or b'').decode('utf-8', 'replace').strip().splitlines()[-3:]
        raise TranscriptionError(f'ffmpeg could not read the audio: {" | ".join(tail)}')

    return result.stdout


def transcribe_file(
    path,
    uri,
    language=None,
    total_seconds=None,
    window_seconds=15,
    timeout=300,
    logger=None,
) -> List[TranscriptSegment]:
    """
    Transcribe a media file window by window.

    Whisper answers with one block of text and no timings, so the audio is cut into
    fixed windows: each window is one request, and its position in the file becomes the
    timing of the resulting cue. That also bounds how much audio is held in memory and
    how long a single request can take.

    Args:
        path: Media file to transcribe
        uri: Server address
        language: Language hint, e.g. 'pl'
        total_seconds: Length of the file, when already known
        window_seconds: Length of each window
        timeout: Socket timeout per window
        logger: Optional callable(str) for progress logging

    Returns:
        list[TranscriptSegment]: One entry per window that produced text.
    """

    def log(message):
        if logger:
            logger(message)

    if total_seconds is None:
        total_seconds = probe_duration(path)
    if not total_seconds:
        raise TranscriptionError('Could not work out how long the audio is')

    window_seconds = max(5, int(window_seconds))
    segments: List[TranscriptSegment] = []
    windows = int(total_seconds // window_seconds) + 1

    # Whisper decides the language per request. Left to itself it can hear Polish in one
    # window and then start translating the next one into English, which produces a
    # transcript that switches language halfway through. So: whatever the first window
    # reports is pinned for every window after it.
    effective_language = language
    log(f'  language: {language or "auto-detect"}')

    for index in range(windows):
        start = index * window_seconds
        if start >= total_seconds:
            break
        length = min(window_seconds, total_seconds - start)

        pcm = extract_pcm(path, start_seconds=start, duration_seconds=length)
        if not pcm:
            continue

        text, detected = transcribe_pcm(
            pcm, uri=uri, language=effective_language, timeout=timeout
        )

        if not effective_language and detected:
            effective_language = detected
            log(f'  detected {detected}; pinning it for the remaining windows')

        log(f'  {_timestamp(start)} ({index + 1}/{windows}) {len(text)} chars')
        if text:
            segments.append(TranscriptSegment(start, start + length, text))

    return segments


def probe_duration(path) -> Optional[int]:
    """Length of a media file in whole seconds, or None when it cannot be read."""
    from media.service.media_info import extract_ffprobe_metadata

    return (extract_ffprobe_metadata(path) or {}).get('duration_seconds')


def _timestamp(seconds):
    """Format seconds as the HH:MM:SS.mmm WebVTT uses."""
    whole = int(seconds)
    milliseconds = int(round((seconds - whole) * 1000))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}'


def segments_to_vtt(segments) -> str:
    """Render segments as WebVTT, the format podcast clients expect."""
    lines = ['WEBVTT', '']
    for segment in segments:
        lines.append(f'{_timestamp(segment.start_seconds)} --> {_timestamp(segment.end_seconds)}')
        lines.append(segment.text)
        lines.append('')
    return '\n'.join(lines)


def segments_to_text(segments) -> str:
    """Render segments as plain running text."""
    return '\n'.join(segment.text for segment in segments).strip()
