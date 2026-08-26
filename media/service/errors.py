"""
Classification of download errors.

Not every failed download deserves the same treatment. Retrying a members-only video
every few minutes is pure waste - it will never succeed without an account. Retrying a
premiere five times in half an hour is equally pointless when it airs in three hours.
Sorting errors into a few categories lets the queue wait the right amount of time, or
give up immediately, and lets the admin show what the user can actually do about it.

The strings matched here are YouTube's own ``playabilityStatus.reason`` texts, passed
through verbatim by yt-dlp. yt-dlp classifies them by substring too, so matching is
best-effort: an unrecognised message simply falls back to the normal retry behaviour.
"""

import re
from datetime import timedelta

# The download can never succeed as configured - retrying only burns attempts.
CATEGORY_PERMANENT = 'permanent'
# Not available yet, but will be at a known-ish time (premiere, scheduled live).
CATEGORY_SCHEDULED = 'scheduled'
# Refused because of who/where we are: bot checks, rate limits, sign-in walls.
# Retrying may work later, and cookies or a proxy usually fix it for good.
CATEGORY_BLOCKED = 'blocked'
# Anything else: network blips, transient 5xx, expiring stream URLs.
CATEGORY_TRANSIENT = 'transient'

_PERMANENT_PATTERNS = (
    'join this channel to get access',
    'members-only',
    'members only',
    'private video',
    'video unavailable',
    'this video is not available',
    'video has been removed',
    'account associated with this video has been terminated',
    'removed by the uploader',
    'no longer available',
    'not available in your country',
    'blocked it in your country',
    'copyright',
    'unsupported url',
)

_SCHEDULED_PATTERNS = (
    'premieres in',
    'premiere will begin',
    'this live event will begin',
    'live event scheduled',
    'is scheduled for',
    'this video is upcoming',
    'not yet available',
)

_BLOCKED_PATTERNS = (
    "sign in to confirm you're not a bot",
    'confirm you are not a bot',
    'sign in to confirm your age',
    'confirm your age',
    'age-restricted',
    'sign in to view',
    'login required',
    'http error 429',
    'too many requests',
    'rate limit',
    'rate-limit',
)

# "Premieres in 3 hours", "Premieres in 25 minutes", "Premieres in 2 days"
_WAIT_RE = re.compile(
    r'\bin\s+(?P<amount>\d+)\s+(?P<unit>second|minute|hour|day)s?\b', re.IGNORECASE
)

_UNIT_TO_KWARG = {
    'second': 'seconds',
    'minute': 'minutes',
    'hour': 'hours',
    'day': 'days',
}


def _normalize(message):
    """Lowercase the message and fold typographic quotes into ASCII ones.

    YouTube writes "you’re" with U+2019, so a plain ASCII pattern would never match.
    """
    text = str(message or '').lower()
    return text.replace('’', "'").replace('‘', "'")


def classify_download_error(message):
    """
    Sort a download error message into one of the CATEGORY_* buckets.

    Args:
        message: The error text stored on the item (yt-dlp's message)

    Returns:
        str: One of CATEGORY_PERMANENT / _SCHEDULED / _BLOCKED / _TRANSIENT.
    """
    text = _normalize(message)
    if not text:
        return CATEGORY_TRANSIENT

    # Scheduled first: "Premieres in ..." is a availability message, and some phrasings
    # also contain words that look permanent ("not yet available").
    for pattern in _SCHEDULED_PATTERNS:
        if pattern in text:
            return CATEGORY_SCHEDULED

    for pattern in _PERMANENT_PATTERNS:
        if pattern in text:
            return CATEGORY_PERMANENT

    for pattern in _BLOCKED_PATTERNS:
        if pattern in text:
            return CATEGORY_BLOCKED

    return CATEGORY_TRANSIENT


def parse_wait_hint(message):
    """
    Extract how long to wait from a message like "Premieres in 3 hours".

    Args:
        message: The error text

    Returns:
        timedelta or None when the message carries no usable hint.
    """
    match = _WAIT_RE.search(_normalize(message))
    if not match:
        return None

    amount = int(match.group('amount'))
    unit = _UNIT_TO_KWARG.get(match.group('unit'))
    if not unit or amount <= 0:
        return None

    # Overshoot slightly: "in 3 hours" is rounded down by YouTube, and a premiere needs
    # a moment after airing before the full video is downloadable.
    return timedelta(**{unit: amount}) + timedelta(minutes=10)


def explain_download_error(category):
    """
    A short, actionable note to store alongside the error.

    Args:
        category: One of the CATEGORY_* values

    Returns:
        str: Hint for the user, or '' when there is nothing useful to add.
    """
    if category == CATEGORY_PERMANENT:
        return (
            'This video cannot be downloaded with the current configuration '
            '(members-only, private, removed or region-blocked). Not retrying.'
        )
    if category == CATEGORY_SCHEDULED:
        return 'Not published yet (premiere or scheduled live); waiting until it airs.'
    if category == CATEGORY_BLOCKED:
        return (
            'YouTube refused the request (bot check, sign-in or rate limit). '
            'Set STASHCAST_YTDLP_COOKIES_FILE, or a proxy - see docs/YOUTUBE_AUTH.md.'
        )
    return ''
