"""
Media metadata and type helpers.

Centralizes ffprobe parsing and extension-based media detection.
"""

import datetime
import json
import subprocess
from pathlib import Path

from media.service.constants import AUDIO_EXTENSIONS, MIME_TYPES, VIDEO_EXTENSIONS
from media.service.config import get_target_audio_format, get_target_video_format

GENERIC_TITLES = {'content', 'downloaded-media', 'untitled', None, ''}


def normalize_extension(extension):
    """Normalize a file extension for comparison."""
    if not extension:
        return ''
    ext = extension.lower()
    if not ext.startswith('.'):
        ext = f'.{ext}'
    return ext


def get_media_type_from_extension(extension):
    """
    Determine if a file extension is audio or video.

    Args:
        extension: File extension (e.g., '.mp3', '.mp4')

    Returns:
        str: 'audio' or 'video'
    """
    ext = normalize_extension(extension)
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    return 'video'


def get_streams_from_extension(extension):
    """
    Infer audio/video stream presence from a file extension.

    Returns:
        tuple[bool, bool]: (has_audio, has_video)
    """
    ext = normalize_extension(extension)
    if ext in AUDIO_EXTENSIONS:
        return True, False
    if ext in VIDEO_EXTENSIONS:
        return True, True
    # Default to video+audio for unknown extensions
    return True, True


def extract_ffprobe_metadata(file_path):
    """
    Extract metadata from a media file using ffprobe.

    Returns:
        dict: {'duration_seconds': int|None, 'tags': dict}
    """
    try:
        result = subprocess.run(
            [
                'ffprobe',
                '-v',
                'quiet',
                '-print_format',
                'json',
                '-show_format',
                '-show_streams',
                str(file_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except Exception:
        return {}

    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    tags = metadata.get('format', {}).get('tags', {}) or {}
    normalized_tags = {str(k).lower(): v for k, v in tags.items()}

    duration_seconds = None
    duration_raw = metadata.get('format', {}).get('duration')
    if duration_raw is not None:
        try:
            duration_seconds = int(float(duration_raw))
        except (TypeError, ValueError):
            duration_seconds = None

    return {
        'duration_seconds': duration_seconds,
        'tags': normalized_tags,
    }


def get_title_from_metadata(file_path):
    """
    Return the embedded title from media metadata, if present.

    Args:
        file_path: Path to media file

    Returns:
        str | None
    """
    metadata = extract_ffprobe_metadata(file_path) or {}
    tags = metadata.get('tags', {}) or {}
    title = tags.get('title')
    return title or None


def resolve_title_from_metadata(title, file_path):
    """
    Resolve a generic title by checking embedded media metadata.

    Replaces the title with embedded metadata if:
    1. Current title is generic (content, untitled, etc.), OR
    2. Current title looks like a filename (short, no spaces, likely from URL)

    Args:
        title: Current title
        file_path: Path to media file

    Returns:
        str: Updated title if metadata exists, otherwise original title
    """

    # Always check for metadata
    meta_title = get_title_from_metadata(file_path)

    # If no metadata, return original
    if not meta_title:
        return title

    # Replace generic titles
    if title in GENERIC_TITLES:
        return meta_title

    # Replace titles that look like filenames:
    # - Short (< 30 chars)
    # - No spaces
    # - Likely extracted from URL path
    # This handles direct downloads where title defaults to filename from URL
    if title and len(title) < 30 and ' ' not in title:
        # Also check if the metadata title is more descriptive (has spaces/longer)
        if ' ' in meta_title or len(meta_title) > len(title):
            return meta_title

    # Keep original title if it's meaningful
    return title


def get_output_extension(resolved_type, source_extension=None):
    """
    Determine output extension for a resolved media type.

    Args:
        resolved_type: 'audio' or 'video'
        source_extension: Optional extension from source file

    Returns:
        str: Output extension (e.g., '.mp3', '.m4a', '.mp4')
    """
    if resolved_type == 'audio':
        if normalize_extension(source_extension) == '.mp3':
            return '.mp3'
        return get_target_audio_format()

    # Video: keep the container the file actually is. Renaming e.g. a .webm to .mp4
    # converts nothing - it only makes the file lie about itself, so a client that
    # trusts the extension (and the MIME type derived from it) downloads the episode
    # and only then refuses to play it.
    ext = normalize_extension(source_extension)
    if ext in VIDEO_EXTENSIONS:
        return ext
    return get_target_video_format()


def parse_publish_date(info):
    """
    Extract the original publication date from a yt-dlp info dict.

    This is the date the video/episode went up on the source platform, which is what
    a podcast client should show - not the date we downloaded it.

    Fields are tried most-precise first: the epoch timestamps carry a time of day,
    while the ``YYYYMMDD`` strings only pin the day and land on midnight UTC.

    Args:
        info: yt-dlp info dict (or any mapping with the same keys)

    Returns:
        datetime.datetime in UTC (timezone-aware), or None when no date is available.
    """
    if not info:
        return None

    for key in ('timestamp', 'release_timestamp'):
        value = info.get(key)
        if value:
            try:
                return datetime.datetime.fromtimestamp(int(value), tz=datetime.timezone.utc)
            except (ValueError, TypeError, OSError, OverflowError):
                pass

    for key in ('upload_date', 'release_date'):
        value = info.get(key)
        if value:
            try:
                parsed = datetime.datetime.strptime(str(value).strip(), '%Y%m%d')
            except (ValueError, TypeError):
                continue
            return parsed.replace(tzinfo=datetime.timezone.utc)

    return None


def get_mime_type(path_or_extension):
    """
    MIME type for a media file, based on its container extension.

    Args:
        path_or_extension: File path or bare extension

    Returns:
        str: MIME type, or 'application/octet-stream' for unknown containers.
    """
    ext = normalize_extension(str(path_or_extension).rsplit('.', 1)[-1])
    return MIME_TYPES.get(ext, 'application/octet-stream')


# Codecs Apple Podcasts / iOS can play inside an MP4 container
IOS_VIDEO_CODECS = {'h264'}
IOS_AUDIO_CODECS = {'aac'}


def probe_codecs(file_path):
    """
    Read the video and audio codec names from a media file.

    ``extract_ffprobe_metadata`` deliberately returns only duration and tags; this is
    for the cases where the codecs themselves matter, e.g. deciding whether a file can
    be stream-copied into an MP4 or has to be re-encoded.

    Args:
        file_path: Path to the media file

    Returns:
        dict: {'video_codec': str|None, 'audio_codec': str|None}. Both None when the
        file cannot be probed.
    """
    try:
        result = subprocess.run(
            [
                'ffprobe',
                '-v',
                'quiet',
                '-print_format',
                'json',
                '-show_streams',
                str(file_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        streams = json.loads(result.stdout).get('streams', [])
    except Exception:
        return {'video_codec': None, 'audio_codec': None}

    video_codec = None
    audio_codec = None
    for stream in streams:
        kind = stream.get('codec_type')
        name = stream.get('codec_name')
        if kind == 'video' and video_codec is None:
            video_codec = name
        elif kind == 'audio' and audio_codec is None:
            audio_codec = name

    return {'video_codec': video_codec, 'audio_codec': audio_codec}


def is_ios_compatible_video(file_path):
    """
    Whether a video file should play in Apple Podcasts / iOS as-is.

    Requires an MP4 container with H.264 video and AAC audio. A Matroska container or
    Opus audio - what yt-dlp produces when AAC is not pinned - fails on those clients
    with "Cannot play this episode on this device".

    Args:
        file_path: Path to the media file

    Returns:
        bool
    """
    if normalize_extension(Path(file_path).suffix) != '.mp4':
        return False

    codecs = probe_codecs(file_path)
    if not codecs['video_codec']:
        return False
    if codecs['video_codec'] not in IOS_VIDEO_CODECS:
        return False
    # A video with no audio track at all is still playable
    if codecs['audio_codec'] and codecs['audio_codec'] not in IOS_AUDIO_CODECS:
        return False
    return True
