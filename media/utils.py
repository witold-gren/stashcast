import re
import unicodedata
from django.conf import settings
from nanoid import generate


# Letters that carry a stroke or are ligatures have no canonical decomposition, so
# normalising them changes nothing and they would simply be dropped - turning "łatwym"
# into "atwym" and leaving folder names looking truncated. Everything else (ą, ć, ę, ń,
# ó, ś, ź, ż and their equivalents in other languages) is a base letter plus a combining
# mark, which normalisation does handle.
_TRANSLITERATIONS = {
    'ł': 'l', 'Ł': 'L',
    'ø': 'o', 'Ø': 'O',
    'đ': 'd', 'Đ': 'D',
    'ð': 'd', 'Ð': 'D',
    'þ': 'th', 'Þ': 'Th',
    'æ': 'ae', 'Æ': 'Ae',
    'œ': 'oe', 'Œ': 'Oe',
    'ß': 'ss',
}


def transliterate(text):
    """
    Fold accented letters down to plain ASCII.

    ą -> a, ę -> e, ł -> l, ó -> o and so on, so that stripping non-ASCII afterwards
    removes punctuation rather than pieces of words.

    Args:
        text: Any string

    Returns:
        str: The same text with accents and strokes removed.
    """
    replaced = ''.join(_TRANSLITERATIONS.get(char, char) for char in text)
    decomposed = unicodedata.normalize('NFKD', replaced)
    return ''.join(char for char in decomposed if not unicodedata.combining(char))


def _join_within(words, max_chars):
    """Join words with hyphens, stopping before the limit is exceeded.

    Cutting the joined string at max_chars would leave a half word at the end, which is
    what made folder names look chopped off.
    """
    slug = ''
    for word in words:
        candidate = f'{slug}-{word}' if slug else word
        if len(candidate) > max_chars:
            break
        slug = candidate

    # A single word longer than the whole budget still has to be shortened somehow
    if not slug and words:
        slug = words[0][:max_chars]

    return slug


def generate_slug(title, max_words=None, max_chars=None):
    """
    Generate a slug from a title.

    Args:
        title: The title to slugify
        max_words: Maximum number of words (default from settings)
        max_chars: Maximum number of characters (default from settings)

    Returns:
        A slugified string
    """
    if max_words is None:
        max_words = settings.STASHCAST_SLUG_MAX_WORDS
    if max_chars is None:
        max_chars = settings.STASHCAST_SLUG_MAX_CHARS

    # Fold accents to ASCII first, so the filter below strips punctuation rather than
    # the letters themselves
    slug = transliterate(title).lower()

    # Replace non-alphanumeric characters with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)

    # Remove leading/trailing hyphens
    slug = slug.strip('-')

    # Split into words
    words = slug.split('-')

    # Limit by max words
    words = words[:max_words]

    # Join without cutting a word in half
    slug = _join_within(words, max_chars).rstrip('-')

    return slug or 'untitled'


def ensure_unique_slug(slug, source_url, existing_item=None, media_type=None):
    """
    Ensure slug is unique across all media types, handling same URL reuse and conflicts.

    Args:
        slug: The proposed slug
        source_url: The source URL for this item
        existing_item: Optional existing MediaItem if reusing same URL+type
        media_type: The media type (audio/video) to differentiate same URL downloads

    Returns:
        A unique slug
    """
    from media.models import MediaItem

    # If this is for an existing item with the same URL and type, reuse its slug
    if existing_item:
        return existing_item.slug

    existing_qs = MediaItem.objects.filter(slug=slug)

    if not existing_qs.exists():
        # Slug is unique
        return slug

    if existing_qs.filter(source_url=source_url, media_type=media_type).exists():
        # Same URL and same type, reuse the slug
        return slug

    # Slug collision: append NanoID suffix until unique
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    while True:
        nano_suffix = generate(alphabet, size=8)
        new_slug = f'{slug}-{nano_suffix}'
        if not MediaItem.objects.filter(slug=new_slug).exists():
            return new_slug


def build_media_url(item, filename, request=None, absolute_builder=None):
    """
    Build a media URL for a file, honoring STASHCAST_MEDIA_BASE_URL when set.

    Args:
        item: MediaItem instance
        filename: Path relative to the item directory (e.g., 'content.mp4')
        request: Optional Django request for absolute URL building
        absolute_builder: Optional callable for absolute URLs (e.g., feed absolute_url)

    Returns:
        str | None
    """
    rel_path = item.get_relative_path(filename)
    if not rel_path:
        return None

    base_url = settings.STASHCAST_MEDIA_BASE_URL
    if base_url:
        return f'{base_url.rstrip("/")}/{rel_path}'

    url = f'/media/files/{rel_path}'
    if absolute_builder:
        return absolute_builder(url)
    if request:
        return request.build_absolute_uri(url)
    return url


def build_group_image_url(group, request=None, absolute_builder=None):
    """
    Build a served URL for a group's cover image, honoring STASHCAST_MEDIA_BASE_URL.

    Mirrors :func:`build_media_url`: uploaded group images live under MEDIA_ROOT and
    are served through the same ``/media/files/`` route (or the CDN base URL).

    Returns:
        str | None
    """
    if not group or not getattr(group, 'image', None):
        return None

    rel_path = group.image.name
    if not rel_path:
        return None

    base_url = settings.STASHCAST_MEDIA_BASE_URL
    if base_url:
        return f'{base_url.rstrip("/")}/{rel_path}'

    url = f'/media/files/{rel_path}'
    if absolute_builder:
        return absolute_builder(url)
    if request:
        return request.build_absolute_uri(url)
    return url


def select_existing_item(source_url, webpage_url, media_type, exclude_guid=None):
    """
    Locate an existing MediaItem for slug reuse.

    Prefers webpage_url match when available.
    """
    from media.models import MediaItem

    if webpage_url:
        qs = MediaItem.objects.filter(webpage_url=webpage_url, media_type=media_type)
    else:
        qs = MediaItem.objects.filter(source_url=source_url, media_type=media_type)

    if exclude_guid:
        qs = qs.exclude(guid=exclude_guid)

    return qs.first()


def log_prefetch_result(log_fn, item):
    """Log key prefetch fields consistently."""
    log_fn(f'Title: {item.title}')
    log_fn(f'Media type: {item.media_type}')
    log_fn(f'Slug: {item.slug}')
    if item.duration_seconds:
        log_fn(f'Duration: {item.duration_seconds}s')
