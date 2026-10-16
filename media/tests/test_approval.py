"""
Tests for per-group approval of channel uploads, push notifications and auto-delete.

yt-dlp, the download pipeline and the push service are mocked: nothing touches the
network, and "a push was sent" means send_to_subscription was called.
"""

import json
import shutil
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from webpush.models import PushInformation, SubscriptionInfo

from media.admin import DEMO_GROUP
from media.models import MediaGroup, MediaItem
from media.notifications import (
    check_approval_token,
    make_approval_token,
    notify_approval_request,
    notify_download_ready_if_enabled,
    send_to_staff,
)
from media.operations import (
    approve_item,
    cleanup_expired_group_items,
    reject_item,
    sync_group_channel,
)
from media.service.youtube_channel import ChannelVideo

User = get_user_model()

PUSH_ON = {
    'STASHCAST_PUSH_ENABLED': True,
    'WEBPUSH_SETTINGS': {
        'VAPID_PUBLIC_KEY': 'public',
        'VAPID_PRIVATE_KEY': 'private',
        'VAPID_ADMIN_EMAIL': 'admin@example.com',
    },
}

VIDEO = ChannelVideo(
    url='https://www.youtube.com/watch?v=aaa', title='Listing title', external_id='aaa'
)


def subscribe(user, endpoint='https://push.example.com/1'):
    subscription = SubscriptionInfo.objects.create(
        browser='chrome', endpoint=endpoint, auth='auth', p256dh='p256dh'
    )
    PushInformation.objects.create(user=user, subscription=subscription)
    return subscription


def awaiting_item(group, **fields):
    defaults = dict(
        source_url=VIDEO.url,
        requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
        slug='pending',
        title='Awaiting',
        group=group,
        status=MediaItem.STATUS_AWAITING_APPROVAL,
    )
    defaults.update(fields)
    return MediaItem.objects.create(**defaults)


class ApprovalSyncTest(TestCase):
    def setUp(self):
        self.group = MediaGroup.objects.create(
            name='Big channel',
            youtube_channel_url='https://www.youtube.com/@big',
            require_approval=True,
        )
        self.process_media = patch('media.tasks.process_media').start()
        self.notify = patch('media.tasks.notify_approval_request').start()
        patch('media.service.youtube_channel.list_channel_videos', return_value=[VIDEO]).start()
        self.addCleanup(patch.stopall)

    def _prefetch(self, item, tmp_dir, log_path, allow_live=False):
        item.title = 'Full title'
        item.description = 'Full description'
        item.thumbnail_url = 'https://i.ytimg.com/vi/aaa/hq.jpg'
        item.slug = 'full-title'
        item.save()

    def test_sync_creates_awaiting_item_without_downloading(self):
        with patch('media.tasks.prefetch_ytdlp', side_effect=self._prefetch):
            items = sync_group_channel(self.group)

        self.assertEqual(len(items), 1)
        item = MediaItem.objects.get(guid=items[0].guid)
        self.assertEqual(item.status, MediaItem.STATUS_AWAITING_APPROVAL)
        self.assertEqual(item.title, 'Full title')
        self.assertEqual(item.thumbnail_url, 'https://i.ytimg.com/vi/aaa/hq.jpg')
        self.process_media.assert_not_called()
        self.notify.assert_called_once()

    def test_failed_prefetch_still_notifies_with_listing_title(self):
        with patch('media.tasks.prefetch_ytdlp', side_effect=Exception('boom')):
            items = sync_group_channel(self.group)

        item = MediaItem.objects.get(guid=items[0].guid)
        self.assertEqual(item.status, MediaItem.STATUS_AWAITING_APPROVAL)
        self.assertEqual(item.title, 'Listing title')
        self.notify.assert_called_once()

    def test_rejected_upload_is_not_offered_again(self):
        with patch('media.tasks.prefetch_ytdlp', side_effect=self._prefetch):
            item = sync_group_channel(self.group)[0]
            reject_item(item)
            again = sync_group_channel(self.group)

        self.assertEqual(again, [])
        self.assertEqual(MediaItem.objects.count(), 1)
        self.assertEqual(self.notify.call_count, 1)

    def test_group_without_approval_downloads_as_before(self):
        self.group.require_approval = False
        self.group.save()

        items = sync_group_channel(self.group)

        self.assertEqual(items[0].status, MediaItem.STATUS_QUEUED)
        self.notify.assert_not_called()


class ApproveRejectTest(TestCase):
    def setUp(self):
        self.group = MediaGroup.objects.create(name='G', require_approval=True)
        self.process_media = patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

    def test_approve_starts_download(self):
        item = awaiting_item(self.group)

        self.assertTrue(approve_item(item))

        self.assertEqual(item.status, MediaItem.STATUS_PREFETCHING)
        self.process_media.assert_called_once_with(item.guid)

    def test_second_answer_is_ignored(self):
        item = awaiting_item(self.group)
        reject_item(item)

        self.assertFalse(approve_item(item))
        self.assertFalse(reject_item(item))
        self.assertEqual(item.status, MediaItem.STATUS_REJECTED)
        self.process_media.assert_not_called()

    def test_feed_excludes_awaiting_and_rejected_items(self):
        awaiting_item(self.group, title='Pending one')
        awaiting_item(
            self.group,
            source_url='https://www.youtube.com/watch?v=bbb',
            title='Rejected one',
            status=MediaItem.STATUS_REJECTED,
        )

        response = self.client.get(reverse('group_feed', args=[self.group.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Pending one')
        self.assertNotContains(response, 'Rejected one')


class ApprovalViewsTest(TestCase):
    def setUp(self):
        self.group = MediaGroup.objects.create(name='G', require_approval=True)
        self.item = awaiting_item(self.group)
        self.process_media = patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

    def _push_url(self, action, token):
        return reverse('push_decision', args=[self.item.guid, action]) + f'?token={token}'

    def test_push_approve_with_valid_token(self):
        token = make_approval_token(self.item, 'approve')

        response = self.client.post(self._push_url('approve', token))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['changed'])
        self.process_media.assert_called_once_with(self.item.guid)

    def test_push_reject_with_valid_token(self):
        token = make_approval_token(self.item, 'reject')

        response = self.client.post(self._push_url('reject', token))

        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, MediaItem.STATUS_REJECTED)

    def test_token_for_one_action_cannot_be_used_for_the_other(self):
        token = make_approval_token(self.item, 'reject')

        response = self.client.post(self._push_url('approve', token))

        self.assertEqual(response.status_code, 403)
        self.process_media.assert_not_called()

    def test_token_for_another_item_is_refused(self):
        other = awaiting_item(self.group, source_url='https://www.youtube.com/watch?v=ccc')
        token = make_approval_token(other, 'approve')

        response = self.client.post(self._push_url('approve', token))

        self.assertEqual(response.status_code, 403)

    def test_garbage_token_is_refused(self):
        response = self.client.post(self._push_url('approve', 'nope'))
        self.assertEqual(response.status_code, 403)

    def test_expired_token_is_refused(self):
        token = make_approval_token(self.item, 'approve')
        with patch('media.notifications.APPROVAL_TOKEN_MAX_AGE', -1):
            self.assertFalse(check_approval_token(token, self.item.guid, 'approve'))

    def test_item_page_buttons_require_staff(self):
        url = reverse('item_decision', args=[self.item.guid, 'approve'])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])
        self.process_media.assert_not_called()

    def test_item_page_approve(self):
        User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

        response = self.client.post(reverse('item_decision', args=[self.item.guid, 'approve']))

        self.assertRedirects(
            response, reverse('item_detail', args=[self.item.guid]), fetch_redirect_response=False
        )
        self.process_media.assert_called_once_with(self.item.guid)

    def test_item_page_refuses_demo_user(self):
        demo = User.objects.create_user('demo', password='password', is_staff=True)
        demo.groups.add(Group.objects.create(name=DEMO_GROUP))
        self.client.login(username='demo', password='password')

        response = self.client.post(reverse('item_decision', args=[self.item.guid, 'reject']))

        self.assertEqual(response.status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, MediaItem.STATUS_AWAITING_APPROVAL)

    def test_item_page_shows_buttons_while_awaiting(self):
        User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

        response = self.client.get(reverse('item_detail', args=[self.item.guid]))

        self.assertContains(response, reverse('item_decision', args=[self.item.guid, 'approve']))
        self.assertContains(response, reverse('item_decision', args=[self.item.guid, 'reject']))


@override_settings(**PUSH_ON)
class NotificationsTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        subscribe(self.admin)
        self.send = patch('webpush.utils.send_to_subscription').start()
        self.addCleanup(patch.stopall)

    def _sent_payloads(self):
        return [json.loads(call.args[1]) for call in self.send.call_args_list]

    def _group(self, users=None, **fields):
        group = MediaGroup.objects.create(name='G', **fields)
        group.notify_users.set([self.admin] if users is None else users)
        return group

    def test_approval_request_has_download_and_reject_buttons(self):
        group = self._group(require_approval=True)
        item = awaiting_item(group, thumbnail_url='https://i.ytimg.com/x.jpg')

        self.assertEqual(notify_approval_request(item), 1)

        payload = self._sent_payloads()[0]
        self.assertEqual([a['action'] for a in payload['actions']], ['approve', 'reject'])
        self.assertEqual(payload['image'], 'https://i.ytimg.com/x.jpg')
        self.assertEqual(payload['url'], reverse('item_detail', args=[item.guid]))
        token = payload['approve_url'].split('token=')[1]
        self.assertTrue(check_approval_token(token, item.guid, 'approve'))

    def test_ready_notification_when_group_asks_for_it(self):
        group = self._group(notify_on_download=True)
        item = awaiting_item(group, status=MediaItem.STATUS_READY, title='Episode')

        self.assertEqual(notify_download_ready_if_enabled(item), 1)
        self.assertIn('Episode', self._sent_payloads()[0]['title'])

    def test_no_ready_notification_when_group_requires_approval(self):
        group = self._group(notify_on_download=True, require_approval=True)
        item = awaiting_item(group, status=MediaItem.STATUS_READY)

        self.assertEqual(notify_download_ready_if_enabled(item), 0)
        self.send.assert_not_called()

    def test_no_ready_notification_when_switched_off(self):
        group = self._group()
        item = awaiting_item(group, status=MediaItem.STATUS_READY)

        self.assertEqual(notify_download_ready_if_enabled(item), 0)
        self.send.assert_not_called()

    def test_only_the_groups_users_are_notified(self):
        other = User.objects.create_superuser('other', 'o@test.com', 'password')
        subscribe(other, endpoint='https://push.example.com/other')
        group = self._group(users=[other], require_approval=True)

        self.assertEqual(notify_approval_request(awaiting_item(group)), 1)
        self.assertEqual(self.send.call_args.args[0].endpoint, 'https://push.example.com/other')

    def test_nobody_is_notified_while_the_list_is_empty(self):
        group = self._group(users=[], require_approval=True, notify_on_download=True)
        item = awaiting_item(group)

        self.assertEqual(notify_approval_request(item), 0)
        item.status = MediaItem.STATUS_READY
        self.assertEqual(notify_download_ready_if_enabled(item), 0)
        self.send.assert_not_called()

    def test_selected_demo_user_still_gets_nothing(self):
        demo = User.objects.create_user('demo', password='password', is_staff=True)
        demo.groups.add(Group.objects.create(name=DEMO_GROUP))
        subscribe(demo, endpoint='https://push.example.com/demo')
        group = self._group(users=[demo], require_approval=True)

        self.assertEqual(notify_approval_request(awaiting_item(group)), 0)

    @override_settings(STASHCAST_PUSH_ENABLED=False)
    def test_nothing_sent_without_vapid_keys(self):
        self.assertEqual(send_to_staff({'title': 'x'}), 0)
        self.send.assert_not_called()

    def test_failing_device_does_not_stop_the_others(self):
        subscribe(self.admin, endpoint='https://push.example.com/2')
        self.send.side_effect = [Exception('gone'), None]

        self.assertEqual(send_to_staff({'title': 'x'}), 1)
        self.assertEqual(self.send.call_count, 2)

    def test_demo_users_are_not_notified(self):
        demo = User.objects.create_user('demo', password='password', is_staff=True)
        demo.groups.add(Group.objects.create(name=DEMO_GROUP))
        subscribe(demo, endpoint='https://push.example.com/demo')

        self.assertEqual(send_to_staff({'title': 'x'}), 1)

    def test_test_notification_goes_to_current_user_only(self):
        other = User.objects.create_superuser('other', 'o@test.com', 'password')
        subscribe(other, endpoint='https://push.example.com/other')
        self.client.login(username='admin', password='password')

        response = self.client.post(reverse('notifications'))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.send.call_count, 1)

    def test_notifications_page_renders(self):
        self.client.login(username='admin', password='password')

        response = self.client.get(reverse('notifications'))

        self.assertContains(response, 'vapid-public-key')


class AutoDeleteTest(TestCase):
    def _ready_item(self, group, slug, days_ago, status=MediaItem.STATUS_READY):
        item = MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug=slug,
            title=slug,
            group=group,
            status=status,
            content_path='content.m4a',
            downloaded_at=timezone.now() - timedelta(days=days_ago),
        )
        base = Path(settings.STASHCAST_MEDIA_DIR) / slug
        base.mkdir(parents=True, exist_ok=True)
        (base / 'content.m4a').write_bytes(b'x')
        self.addCleanup(shutil.rmtree, base, True)
        return item, base

    def test_deletes_old_items_and_files_in_enabled_groups(self):
        group = MediaGroup.objects.create(name='Cleaned', auto_delete_enabled=True)
        old, old_dir = self._ready_item(group, 'old-one', days_ago=91)
        archived, _ = self._ready_item(
            group, 'old-archived', days_ago=120, status=MediaItem.STATUS_ARCHIVED
        )
        fresh, fresh_dir = self._ready_item(group, 'fresh-one', days_ago=10)

        self.assertEqual(cleanup_expired_group_items(), 2)

        self.assertFalse(MediaItem.objects.filter(guid__in=[old.guid, archived.guid]).exists())
        self.assertFalse(old_dir.exists())
        self.assertTrue(MediaItem.objects.filter(guid=fresh.guid).exists())
        self.assertTrue(fresh_dir.exists())

    def test_respects_custom_retention(self):
        group = MediaGroup.objects.create(
            name='Short', auto_delete_enabled=True, auto_delete_after_days=7
        )
        self._ready_item(group, 'week-old', days_ago=8)

        self.assertEqual(cleanup_expired_group_items(), 1)

    def test_groups_without_auto_delete_are_untouched(self):
        group = MediaGroup.objects.create(name='Kept')
        item, base = self._ready_item(group, 'ancient', days_ago=1000)

        self.assertEqual(cleanup_expired_group_items(), 0)
        self.assertTrue(MediaItem.objects.filter(guid=item.guid).exists())
        self.assertTrue(base.exists())

    def test_rejected_items_are_kept(self):
        group = MediaGroup.objects.create(name='Cleaned', auto_delete_enabled=True)
        rejected = awaiting_item(
            group,
            status=MediaItem.STATUS_REJECTED,
            downloaded_at=timezone.now() - timedelta(days=365),
        )

        cleanup_expired_group_items()

        self.assertTrue(MediaItem.objects.filter(guid=rejected.guid).exists())


class ServiceWorkerTest(TestCase):
    def test_served_from_root_as_javascript(self):
        response = self.client.get('/sw.js')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/javascript')
        content = response.content.decode()
        self.assertIn("addEventListener('push'", content)
        self.assertIn("addEventListener('notificationclick'", content)


class GroupNotifyUsersAdminTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

    def _post(self, **fields):
        data = {
            'name': 'Channel',
            'slug': 'channel',
            'download_type': 'audio',
            'auto_delete_after_days': 90,
        }
        data.update(fields)
        return self.client.post(reverse('admin:media_mediagroup_add'), data, follow=True)

    def test_notify_on_download_needs_someone_to_notify(self):
        response = self._post(notify_on_download='on')

        self.assertContains(response, 'Pick at least one user to notify')
        self.assertFalse(MediaGroup.objects.exists())

    def test_users_are_saved(self):
        self._post(notify_on_download='on', notify_users=[self.admin.pk])

        group = MediaGroup.objects.get()
        self.assertEqual(list(group.notify_users.all()), [self.admin])

    def test_approval_without_users_saves_with_a_warning(self):
        response = self._post(require_approval='on')

        self.assertTrue(MediaGroup.objects.filter(require_approval=True).exists())
        self.assertContains(response, 'notifies nobody')

    def test_only_active_staff_can_be_picked(self):
        User.objects.create_user('listener', password='password')

        response = self.client.get(reverse('admin:media_mediagroup_add'))

        self.assertContains(response, 'admin')
        self.assertNotContains(response, '>listener<')
