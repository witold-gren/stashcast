"""
Tests for the YouTube button: the userscript in userscript/ and what the server does
for it - picking the group by the video's channel, the default group, skipping videos
it already has, and handing out a configured copy of the script.
"""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from media.admin import DEMO_GROUP
from media.models import MediaGroup, MediaItem
from media.operations import find_group_for_channel
from media.service.resolve import PrefetchResult
from media.service.youtube_channel import channel_keys
from media.views import USERSCRIPT_PATH, build_userscript

User = get_user_model()

TOKEN = 'test-token'
VIDEO = 'https://www.youtube.com/watch?v=abcdefghijk'


class ChannelKeysTest(TestCase):
    def test_handle_url(self):
        self.assertEqual(channel_keys('https://www.youtube.com/@Name/videos'), {'handle:@name'})

    def test_channel_id_url(self):
        self.assertEqual(channel_keys('https://www.youtube.com/channel/UC123'), {'id:UC123'})

    def test_legacy_urls(self):
        self.assertEqual(channel_keys('https://www.youtube.com/c/Some'), {'c:some'})
        self.assertEqual(channel_keys('https://www.youtube.com/user/Some'), {'user:some'})

    def test_video_metadata(self):
        self.assertEqual(
            channel_keys(None, channel_id='UC123', handle='@Name'), {'id:UC123', 'handle:@name'}
        )

    def test_nothing_known(self):
        self.assertEqual(channel_keys('https://example.com/video.mp4'), set())


class FindGroupForChannelTest(TestCase):
    def setUp(self):
        self.by_handle = MediaGroup.objects.create(
            name='Handle', youtube_channel_url='https://www.youtube.com/@BuildChannel'
        )
        self.by_id = MediaGroup.objects.create(
            name='Id', youtube_channel_url='https://www.youtube.com/channel/UCxyz/videos'
        )
        MediaGroup.objects.create(name='No channel')

    def test_matches_handle_case_insensitively(self):
        self.assertEqual(find_group_for_channel(handle='@buildchannel'), self.by_handle)

    def test_matches_id_when_the_group_uses_the_id_form(self):
        self.assertEqual(find_group_for_channel(channel_id='UCxyz', handle='@other'), self.by_id)

    def test_matches_by_channel_url(self):
        self.assertEqual(
            find_group_for_channel(channel_url='https://www.youtube.com/channel/UCxyz'),
            self.by_id,
        )

    def test_unknown_channel(self):
        self.assertIsNone(find_group_for_channel(channel_id='UCnope', handle='@nope'))
        self.assertIsNone(find_group_for_channel())


@override_settings(STASHCAST_USER_TOKEN=TOKEN)
class StashGroupTest(TestCase):
    def setUp(self):
        self.default = MediaGroup.objects.create(name='Inbox', download_type='audio')
        self.channel_group = MediaGroup.objects.create(
            name='Builders',
            download_type='video',
            youtube_channel_url='https://www.youtube.com/@builders',
        )
        self.process_media = patch('media.views.process_media').start()
        self.addCleanup(patch.stopall)

    def _prefetch(self, **fields):
        result = PrefetchResult(title='Video', **fields)
        return patch('media.service.resolve.prefetch', return_value=result)

    def _stash(self, **params):
        data = {'token': TOKEN, 'url': VIDEO, 'type': 'auto', **params}
        return self.client.post('/stash/', data)

    def test_unknown_channel_goes_to_the_default_group(self):
        with self._prefetch(channel_id='UCnobody', channel_handle='@nobody'):
            response = self._stash(group='inbox')

        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['group'], 'Inbox')
        self.assertFalse(data['matched_channel'])
        item = MediaItem.objects.get(guid=data['guid'])
        self.assertEqual(item.group, self.default)
        # 'auto' follows the group's download type
        self.assertEqual(item.requested_type, 'audio')
        self.process_media.assert_called_once_with(item.guid)

    def test_followed_channel_wins_over_the_default_group(self):
        with self._prefetch(channel_handle='@Builders'):
            data = self._stash(group='inbox').json()

        self.assertEqual(data['group'], 'Builders')
        self.assertTrue(data['matched_channel'])
        item = MediaItem.objects.get(guid=data['guid'])
        self.assertEqual(item.group, self.channel_group)
        self.assertEqual(item.requested_type, 'video')

    def test_group_can_be_given_by_name(self):
        with self._prefetch():
            data = self._stash(group='INBOX').json()

        self.assertEqual(data['group'], 'Inbox')

    def test_unknown_group_is_refused(self):
        response = self._stash(group='nope')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(MediaItem.objects.exists())

    def test_without_group_nothing_changes(self):
        with self._prefetch():
            data = self._stash().json()

        self.assertIsNone(data['group'])
        self.assertIsNone(MediaItem.objects.get(guid=data['guid']).group)

    def test_existing_video_is_not_downloaded_again(self):
        item = MediaItem.objects.create(
            source_url='https://youtu.be/abcdefghijk',
            requested_type='audio',
            slug='video',
            title='Already here',
            status=MediaItem.STATUS_READY,
            group=self.default,
        )

        with self._prefetch() as prefetch:
            data = self._stash(skip_existing='true').json()

        self.assertTrue(data['already'])
        self.assertEqual(data['guid'], item.guid)
        self.assertEqual(data['group'], 'Inbox')
        prefetch.assert_not_called()
        self.process_media.assert_not_called()

    def test_rejected_video_is_downloaded_when_asked_again(self):
        """Clicking the button is a deliberate request - it overrides an earlier reject"""
        rejected = MediaItem.objects.create(
            source_url=VIDEO,
            requested_type='video',
            slug='pending',
            status=MediaItem.STATUS_REJECTED,
            group=self.channel_group,
        )

        with self._prefetch(channel_handle='@builders'):
            data = self._stash(skip_existing='true').json()

        self.assertNotIn('already', data)
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, MediaItem.STATUS_PREFETCHING)
        self.process_media.assert_called_once_with(rejected.guid)

    def test_without_skip_existing_a_ready_video_is_fetched_again(self):
        """The bookmarklet's re-fetch behaviour stays as it was"""
        MediaItem.objects.create(
            source_url=VIDEO, requested_type='auto', slug='v', status=MediaItem.STATUS_READY
        )

        with self._prefetch():
            data = self._stash().json()

        self.assertNotIn('already', data)
        self.process_media.assert_called_once()


class UserscriptTest(TestCase):
    def test_repository_script_has_every_setting(self):
        """build_userscript raises if a configuration line went missing"""
        build_userscript('https://x', 'tok', 'grp', 'audio')

    def test_values_are_written_as_js_strings(self):
        script = build_userscript('https://stash.example', 'to"ken', 'my-group', 'video')

        self.assertIn('const STASHCAST_URL = "https://stash.example";', script)
        self.assertIn(f'const TOKEN = {json.dumps("to" + chr(34) + "ken")};', script)
        self.assertIn('const DEFAULT_GROUP = "my-group";', script)
        self.assertIn('const TYPE = "video";', script)

    def test_userscript_header(self):
        source = USERSCRIPT_PATH.read_text()

        self.assertIn('// ==UserScript==', source)
        self.assertIn('@match        https://www.youtube.com/*', source)
        self.assertIn('@grant        GM.xmlHttpRequest', source)


@override_settings(STASHCAST_USER_TOKEN=TOKEN)
class YoutubeButtonPageTest(TestCase):
    def setUp(self):
        self.group = MediaGroup.objects.create(name='Inbox')
        User.objects.create_superuser('admin', 'a@example.com', 'password')
        self.client.login(username='admin', password='password')

    def test_page_shows_a_configured_script(self):
        response = self.client.get(reverse('youtube_button'), {'group': 'inbox', 'type': 'audio'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'const TOKEN = &quot;{TOKEN}&quot;;')
        self.assertContains(response, 'const DEFAULT_GROUP = &quot;inbox&quot;;')
        self.assertContains(response, 'const STASHCAST_URL = &quot;http://testserver&quot;;')

    def test_download_is_a_userscript_file(self):
        response = self.client.get(reverse('youtube_button_script'), {'group': 'inbox'})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/javascript'))
        self.assertTrue(reverse('youtube_button_script').endswith('.user.js'))
        script = response.content.decode()
        self.assertIn('const DEFAULT_GROUP = "inbox";', script)
        self.assertIn(f'const TOKEN = "{TOKEN}";', script)

    def test_invalid_options_fall_back_to_defaults(self):
        script = self.client.get(
            reverse('youtube_button_script'), {'group': 'missing', 'type': 'nope'}
        ).content.decode()

        self.assertIn('const DEFAULT_GROUP = "";', script)
        self.assertIn('const TYPE = "auto";', script)

    def test_demo_user_gets_no_real_token(self):
        demo = User.objects.create_user('demo', password='password', is_staff=True)
        demo.groups.add(Group.objects.create(name=DEMO_GROUP))
        self.client.login(username='demo', password='password')

        script = self.client.get(reverse('youtube_button_script')).content.decode()

        self.assertNotIn(TOKEN, script)

    def test_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse('youtube_button_script'))

        self.assertEqual(response.status_code, 302)
