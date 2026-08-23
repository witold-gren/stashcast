"""
Tests for the media groups feature.

Covers the MediaGroup model, group filtering in the grid/list views, per-group
RSS feeds, the feed-links listing, and group selection on the stash form.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from media.models import MediaGroup, MediaItem
from media.utils import build_group_image_url

User = get_user_model()


def _ready_item(slug, title, group=None, media_type=MediaItem.MEDIA_TYPE_VIDEO):
    return MediaItem.objects.create(
        source_url=f'http://example.com/{slug}',
        requested_type=MediaItem.REQUESTED_TYPE_AUTO,
        slug=slug,
        title=title,
        media_type=media_type,
        status=MediaItem.STATUS_READY,
        group=group,
    )


class MediaGroupModelTest(TestCase):
    def test_slug_generated_from_name(self):
        group = MediaGroup.objects.create(name='Lekcje')
        self.assertEqual(group.slug, 'lekcje')

    def test_slug_unique_when_names_collide_on_base(self):
        MediaGroup.objects.create(name='Kitchen')
        # Different name that slugifies to the same base
        second = MediaGroup.objects.create(name='Kitchen!')
        self.assertEqual(second.slug, 'kitchen-2')

    def test_slug_falls_back_when_unslugifiable(self):
        group = MediaGroup.objects.create(name='!!!')
        self.assertTrue(group.slug.startswith('group'))

    def test_item_count_only_counts_ready(self):
        group = MediaGroup.objects.create(name='Other')
        _ready_item('r1', 'Ready One', group=group)
        MediaItem.objects.create(
            source_url='http://example.com/x',
            requested_type=MediaItem.REQUESTED_TYPE_AUTO,
            slug='pending-one',
            status=MediaItem.STATUS_DOWNLOADING,
            group=group,
        )
        self.assertEqual(group.item_count, 1)

    def test_group_set_null_on_delete(self):
        group = MediaGroup.objects.create(name='Temp')
        item = _ready_item('r2', 'Item', group=group)
        group.delete()
        item.refresh_from_db()
        self.assertIsNone(item.group)


class GroupImageUrlTest(TestCase):
    def test_no_image_returns_none(self):
        group = MediaGroup.objects.create(name='Lekcje')
        self.assertIsNone(build_group_image_url(group))

    @override_settings(STASHCAST_MEDIA_BASE_URL=None)
    def test_local_serving_url(self):
        group = MediaGroup.objects.create(name='Lekcje')
        group.image = 'group-images/cover.png'
        self.assertEqual(
            build_group_image_url(group), '/media/files/group-images/cover.png'
        )

    @override_settings(STASHCAST_MEDIA_BASE_URL='https://cdn.example.com/')
    def test_cdn_base_url(self):
        group = MediaGroup.objects.create(name='Lekcje')
        group.image = 'group-images/cover.png'
        self.assertEqual(
            build_group_image_url(group),
            'https://cdn.example.com/group-images/cover.png',
        )


class GroupFilterViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')
        self.lekcje = MediaGroup.objects.create(name='Lekcje')
        _ready_item('grouped', 'Grouped Item', group=self.lekcje)
        _ready_item('loose', 'Loose Item', group=None)

    def test_grid_filter_by_group_slug(self):
        response = self.client.get('/admin/tools/grid/?group=lekcje')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Grouped Item')
        self.assertNotContains(response, 'Loose Item')

    def test_grid_filter_ungrouped(self):
        response = self.client.get('/admin/tools/grid/?group=none')
        self.assertContains(response, 'Loose Item')
        self.assertNotContains(response, 'Grouped Item')

    def test_list_filter_by_group_slug(self):
        response = self.client.get('/admin/tools/list/?group=lekcje')
        self.assertContains(response, 'Grouped Item')
        self.assertNotContains(response, 'Loose Item')

    def test_group_dropdown_rendered(self):
        response = self.client.get('/admin/tools/grid/')
        self.assertContains(response, 'value="lekcje"')


class GroupFeedTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.lekcje = MediaGroup.objects.create(name='Lekcje')
        self.inne = MediaGroup.objects.create(name='Inne')
        _ready_item('in-lekcje', 'Lesson Video', group=self.lekcje)
        _ready_item('in-inne', 'Other Video', group=self.inne)
        _ready_item('no-group', 'Ungrouped Video', group=None)

    def test_group_feed_contains_only_group_items(self):
        response = self.client.get('/feeds/group/lekcje.xml')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Lesson Video', body)
        self.assertNotIn('Other Video', body)
        self.assertNotIn('Ungrouped Video', body)

    def test_group_feed_title_has_no_stashcast_prefix(self):
        response = self.client.get('/feeds/group/lekcje.xml')
        body = response.content.decode()
        # Channel title is the bare group name, without the "StashCast —" prefix.
        self.assertIn('<title>Lekcje</title>', body)
        self.assertNotIn('StashCast — Lekcje', body)

    def test_group_feed_uses_default_image_without_custom_image(self):
        response = self.client.get('/feeds/group/lekcje.xml')
        body = response.content.decode()
        self.assertIn('<image>', body)
        self.assertIn('feed-combined.png', body)

    def test_group_feed_uses_custom_image_when_set(self):
        # Assigning a name string sets the file reference without touching storage.
        self.lekcje.image = 'group-images/lekcje-cover.png'
        self.lekcje.save(update_fields=['image'])
        response = self.client.get('/feeds/group/lekcje.xml')
        body = response.content.decode()
        self.assertIn('<image>', body)
        self.assertIn('group-images/lekcje-cover.png', body)
        self.assertNotIn('feed-combined.png', body)

    def test_unknown_group_feed_returns_404(self):
        response = self.client.get('/feeds/group/does-not-exist.xml')
        self.assertEqual(response.status_code, 404)

    def test_static_feeds_still_work(self):
        # Regression: refactor to obj-based feeds must not break existing feeds.
        response = self.client.get('/feeds/video.xml')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Lesson Video', body)
        self.assertIn('Other Video', body)


class FeedLinksGroupsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

    def test_feed_links_lists_group_feed(self):
        MediaGroup.objects.create(name='Kuchnia')
        response = self.client.get('/admin/tools/feeds/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Kuchnia')
        self.assertContains(response, '/feeds/group/kuchnia.xml')

    def test_feed_links_empty_state(self):
        response = self.client.get('/admin/tools/feeds/')
        self.assertContains(response, 'No groups yet')

    def test_feed_links_shows_group_cover_image(self):
        group = MediaGroup.objects.create(name='Kuchnia')
        group.image = 'group-images/kuchnia.png'
        group.save(update_fields=['image'])
        response = self.client.get('/admin/tools/feeds/')
        self.assertContains(response, 'group-images/kuchnia.png')

    def test_feed_links_falls_back_to_default_image(self):
        MediaGroup.objects.create(name='Bezobrazkowa')
        response = self.client.get('/admin/tools/feeds/')
        self.assertContains(response, 'feed-combined.png')


class StashFormGroupTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

    def test_form_renders_group_selector(self):
        MediaGroup.objects.create(name='Lekcje')
        response = self.client.get('/admin/tools/add-url/')
        self.assertContains(response, 'name="group"')
        self.assertContains(response, 'name="new_group"')
        self.assertContains(response, 'Lekcje')

    @patch('media.views.process_media_batch')
    def test_bulk_submit_creates_and_assigns_new_group(self, mock_batch):
        self.client.post(
            '/admin/tools/add-url/',
            {
                'type': 'auto',
                'new_group': 'Kuchnia',
                'bulk_urls': 'http://example.com/a\nhttp://example.com/b',
            },
        )
        group = MediaGroup.objects.get(name='Kuchnia')
        items = MediaItem.objects.filter(source_url__startswith='http://example.com/')
        self.assertEqual(items.count(), 2)
        for item in items:
            self.assertEqual(item.group_id, group.pk)

    @patch('media.views.process_media_batch')
    def test_bulk_submit_assigns_existing_group(self, mock_batch):
        group = MediaGroup.objects.create(name='Inne')
        self.client.post(
            '/admin/tools/add-url/',
            {
                'type': 'auto',
                'group': str(group.pk),
                'bulk_urls': 'http://example.com/c',
            },
        )
        item = MediaItem.objects.get(source_url='http://example.com/c')
        self.assertEqual(item.group_id, group.pk)

    @patch('media.views.process_media_batch')
    def test_new_group_takes_precedence_over_selected(self, mock_batch):
        existing = MediaGroup.objects.create(name='Inne')
        self.client.post(
            '/admin/tools/add-url/',
            {
                'type': 'auto',
                'group': str(existing.pk),
                'new_group': 'Lekcje',
                'bulk_urls': 'http://example.com/d',
            },
        )
        item = MediaItem.objects.get(source_url='http://example.com/d')
        self.assertEqual(item.group.name, 'Lekcje')


class GroupDownloadTypeTest(TestCase):
    """Tests for a group's audio/video download convention"""

    def test_default_is_audio(self):
        """Existing groups keep downloading audio, as the channel sync always did"""
        group = MediaGroup.objects.create(name='Lekcje')
        self.assertEqual(group.download_type, MediaGroup.DOWNLOAD_TYPE_AUDIO)

    def test_choices_match_requested_type_values(self):
        """The value is passed straight through as a requested type, so it must match"""
        self.assertEqual(MediaGroup.DOWNLOAD_TYPE_AUDIO, MediaItem.REQUESTED_TYPE_AUDIO)
        self.assertEqual(MediaGroup.DOWNLOAD_TYPE_VIDEO, MediaItem.REQUESTED_TYPE_VIDEO)


class ResolveRequestedTypeTest(TestCase):
    """Tests for how a group's convention combines with a per-download choice"""

    def setUp(self):
        from media.operations import resolve_requested_type

        self.resolve = resolve_requested_type
        self.audio_group = MediaGroup.objects.create(
            name='Audio', download_type=MediaGroup.DOWNLOAD_TYPE_AUDIO
        )
        self.video_group = MediaGroup.objects.create(
            name='Video', download_type=MediaGroup.DOWNLOAD_TYPE_VIDEO
        )

    def test_auto_defers_to_the_group(self):
        self.assertEqual(self.resolve('auto', self.video_group), 'video')
        self.assertEqual(self.resolve('auto', self.audio_group), 'audio')

    def test_explicit_choice_wins_over_the_group(self):
        """Asking for video explicitly must not be overridden by an audio group"""
        self.assertEqual(self.resolve('video', self.audio_group), 'video')
        self.assertEqual(self.resolve('audio', self.video_group), 'audio')

    def test_no_group_leaves_the_type_alone(self):
        self.assertEqual(self.resolve('auto', None), 'auto')
        self.assertEqual(self.resolve('video', None), 'video')

    def test_empty_type_is_treated_as_no_preference(self):
        self.assertEqual(self.resolve('', self.video_group), 'video')
        self.assertEqual(self.resolve(None, self.video_group), 'video')
        self.assertEqual(self.resolve(None, None), 'auto')


class StashUrlGroupTypeTest(TestCase):
    """Tests that stash_url honours the group's convention"""

    def setUp(self):
        self.process_media = patch('media.tasks.process_media').start()
        self.addCleanup(patch.stopall)

    def test_video_group_gives_video_items(self):
        from media.operations import stash_url

        group = MediaGroup.objects.create(
            name='Filmy', download_type=MediaGroup.DOWNLOAD_TYPE_VIDEO
        )
        item = stash_url('https://youtu.be/v1', requested_type='auto', group=group)

        self.assertEqual(item.requested_type, MediaItem.REQUESTED_TYPE_VIDEO)

    def test_audio_group_gives_audio_items(self):
        from media.operations import stash_url

        group = MediaGroup.objects.create(
            name='Podcasty', download_type=MediaGroup.DOWNLOAD_TYPE_AUDIO
        )
        item = stash_url('https://youtu.be/v1', requested_type='auto', group=group)

        self.assertEqual(item.requested_type, MediaItem.REQUESTED_TYPE_AUDIO)

    def test_explicit_type_survives(self):
        from media.operations import stash_url

        group = MediaGroup.objects.create(
            name='Podcasty', download_type=MediaGroup.DOWNLOAD_TYPE_AUDIO
        )
        item = stash_url('https://youtu.be/v1', requested_type='video', group=group)

        self.assertEqual(item.requested_type, MediaItem.REQUESTED_TYPE_VIDEO)

    def test_ungrouped_auto_stays_auto(self):
        from media.operations import stash_url

        item = stash_url('https://youtu.be/v1', requested_type='auto')

        self.assertEqual(item.requested_type, MediaItem.REQUESTED_TYPE_AUTO)
