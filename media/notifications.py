"""Web push notifications (django-webpush).

Item notifications go to the users picked in the item's group (MediaGroup.notify_users),
on every device they subscribed; an empty list means nobody. Push is optional:
without VAPID keys (STASHCAST_PUSH_ENABLED False) every function here is a no-op, and
no function ever raises - a notification is never worth failing a download for.

URLs in payloads are relative; the service worker resolves them against its own origin,
so no site domain has to be configured.
"""

import json
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.templatetags.static import static
from django.urls import reverse
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

# How long the Download / Reject buttons in a notification keep working
APPROVAL_TOKEN_MAX_AGE = 14 * 24 * 3600

APPROVAL_ACTIONS = ('approve', 'reject')

_DESCRIPTION_PREVIEW_CHARS = 160


def push_enabled():
    return bool(getattr(settings, 'STASHCAST_PUSH_ENABLED', False))


def _recipients():
    """Active staff users who may act on items (demo read-only accounts excluded)."""
    from media.admin import DEMO_GROUP

    return (
        get_user_model()
        .objects.filter(is_active=True, is_staff=True)
        .exclude(groups__name=DEMO_GROUP)
    )


def send_to_staff(payload, ttl=None, users=None):
    """Send ``payload`` to every subscribed device of every recipient.

    ``users`` narrows the recipients (e.g. a test sent to the current user only).

    Returns:
        int: How many devices the notification was delivered to.
    """
    if not push_enabled():
        return 0

    from webpush.models import PushInformation
    from webpush.utils import send_to_subscription

    if ttl is None:
        ttl = settings.STASHCAST_PUSH_TTL_SECONDS
    data = json.dumps(payload)

    sent = 0
    recipients = _recipients()
    if users is not None:
        recipients = recipients.filter(pk__in=[u.pk for u in users])
    push_infos = PushInformation.objects.filter(user__in=recipients).select_related('subscription')
    for push_info in push_infos:
        # One dead device must not stop the others from being notified
        try:
            send_to_subscription(push_info.subscription, data, ttl)
            sent += 1
        except Exception as e:
            logger.warning('Push to subscription %s failed: %s', push_info.subscription_id, e)
    return sent


def make_approval_token(item, action):
    """Signed token that allows exactly ``action`` on exactly this item."""
    return signing.TimestampSigner(salt=f'media-approval:{action}').sign(item.guid)


def check_approval_token(token, guid, action):
    """True when ``token`` was issued for ``action`` on item ``guid`` and has not expired."""
    if action not in APPROVAL_ACTIONS or not token:
        return False
    try:
        signed_guid = signing.TimestampSigner(salt=f'media-approval:{action}').unsign(
            token, max_age=APPROVAL_TOKEN_MAX_AGE
        )
    except signing.BadSignature:
        return False
    return signed_guid == guid


def _push_decision_url(item, action):
    url = reverse('push_decision', args=[item.guid, action])
    return f'{url}?token={make_approval_token(item, action)}'


def _item_body(item):
    parts = []
    if item.group:
        parts.append(item.group.name)
    if item.duration_seconds:
        minutes = round(item.duration_seconds / 60)
        parts.append(_('%(minutes)s min') % {'minutes': minutes})
    header = ' · '.join(parts)

    description = ' '.join((item.description or '').split())
    if len(description) > _DESCRIPTION_PREVIEW_CHARS:
        description = description[: _DESCRIPTION_PREVIEW_CHARS - 1].rstrip() + '…'

    return '\n'.join(p for p in (header, description) if p)


def _group_users(item):
    """Users who asked for notifications from the item's group (none without a group)."""
    if not item.group_id:
        return []
    return list(item.group.notify_users.all())


def _base_payload(item, title):
    return {
        'title': title,
        'body': _item_body(item),
        'icon': static('media/icons/icon-192.png'),
        'image': item.thumbnail_url or '',
        'tag': item.guid,
        'url': reverse('item_detail', args=[item.guid]),
    }


def notify_approval_request(item):
    """Ask whether a new channel upload should be downloaded."""
    if not push_enabled():
        return 0

    payload = _base_payload(item, _('New: %(title)s') % {'title': item.title or item.source_url})
    payload.update(
        {
            'approve_url': _push_decision_url(item, 'approve'),
            'reject_url': _push_decision_url(item, 'reject'),
            'actions': [
                {'action': 'approve', 'title': _('Download')},
                {'action': 'reject', 'title': _('Reject')},
            ],
            # Keep it on screen until answered
            'requireInteraction': True,
            # The service worker is plain JS and cannot translate; it shows these after a
            # button press
            'labels': {
                'downloading': _('Downloading'),
                'rejected': _('Rejected'),
                'already_handled': _('Already handled'),
                'failed': _('Request failed'),
                'unreachable': _('Could not reach StashCast'),
            },
        }
    )
    return send_to_staff(payload, users=_group_users(item))


def notify_download_ready_if_enabled(item):
    """Announce a finished download when the item's group asks for it.

    Groups that require approval are skipped: their user already chose to download the
    item from a notification, a second one would only repeat it.
    """
    group = item.group
    if not group or not group.notify_on_download or group.require_approval:
        return 0
    try:
        return send_to_staff(
            _base_payload(item, _('Ready: %(title)s') % {'title': item.title or item.source_url}),
            users=_group_users(item),
        )
    except Exception as e:
        logger.warning('Ready notification for %s failed: %s', item.guid, e)
        return 0
