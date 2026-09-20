"""
Media format constants.

Centralized definitions of file extensions and media types.
"""

# All supported media file extensions
MEDIA_EXTENSIONS = [
    '.mp3',
    '.m4a',
    '.mp4',
    '.webm',
    '.ogg',
    '.wav',
    '.aac',
    '.flac',
    '.opus',
    '.mkv',
    '.avi',
    '.mov',
]

# Audio-specific file extensions
AUDIO_EXTENSIONS = ['.mp3', '.m4a', '.ogg', '.wav', '.aac', '.flac', '.opus']

# Video-specific file extensions
VIDEO_EXTENSIONS = ['.mp4', '.webm', '.mkv', '.avi', '.mov']

# Container extension -> MIME type advertised to clients (RSS enclosures).
# Being honest here matters: a podcast client decides whether it can play an episode
# from this value, so a wrong or generic type makes it refuse before downloading.
MIME_TYPES = {
    '.mp3': 'audio/mpeg',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
    '.ogg': 'audio/ogg',
    '.opus': 'audio/ogg',
    '.wav': 'audio/wav',
    '.flac': 'audio/flac',
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    # Transcript / subtitle formats, announced in the feed's <podcast:transcript> tag.
    # Clients decide whether they can render a transcript from this value.
    '.vtt': 'text/vtt',
    '.srt': 'application/x-subrip',
    '.txt': 'text/plain',
}
