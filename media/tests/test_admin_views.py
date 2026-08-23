"""
Tests for admin views (grid, list, item detail, progress, SSE).

These views require authentication and provide the admin interface for managing media.
"""

import datetime
import json

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from media.models import MediaItem

User = get_user_model()


class AdminGridViewTest(TestCase):
    """Test the grid view (/admin/tools/grid/)"""

    def setUp(self):
        self.client = Client()
        # Create superuser for authentication
        self.user = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

    def test_grid_view_loads(self):
        """Test that grid view page loads successfully"""
        response = self.client.get('/admin/tools/grid/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/grid_view.html')

    def test_grid_view_shows_ready_items(self):
        """Test that grid view shows READY items"""
        MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get('/admin/tools/grid/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Audio')

    def test_grid_view_hides_non_ready_items(self):
        """Test that grid view hides non-READY items"""
        MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
            title='Pending Item',
            status=MediaItem.STATUS_DOWNLOADING,
        )

        response = self.client.get('/admin/tools/grid/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Pending Item')

    def test_grid_view_filter_audio(self):
        """Test filtering grid view by audio type"""
        MediaItem.objects.create(
            source_url='http://example.com/audio.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Audio Item',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
        )
        MediaItem.objects.create(
            source_url='http://example.com/video.mp4',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            slug='test-video',
            title='Video Item',
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get('/admin/tools/grid/?type=audio')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Audio Item')
        self.assertNotContains(response, 'Video Item')

    def test_grid_view_filter_video(self):
        """Test filtering grid view by video type"""
        MediaItem.objects.create(
            source_url='http://example.com/audio.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Audio Item',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
        )
        MediaItem.objects.create(
            source_url='http://example.com/video.mp4',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            slug='test-video',
            title='Video Item',
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get('/admin/tools/grid/?type=video')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Audio Item')
        self.assertContains(response, 'Video Item')

    def test_grid_view_requires_authentication(self):
        """Test that grid view requires login"""
        self.client.logout()
        response = self.client.get('/admin/tools/grid/')
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/admin/login/'))


class AdminListViewTest(TestCase):
    """Test the list view (/admin/tools/list/)"""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

    def test_list_view_loads(self):
        """Test that list view page loads successfully"""
        response = self.client.get('/admin/tools/list/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/list_view.html')

    def test_list_view_shows_ready_items(self):
        """Test that list view shows READY items"""
        MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get('/admin/tools/list/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Audio')

    def test_list_view_shows_metadata(self):
        """Test that list view shows item metadata"""
        MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            description='Test description',
            author='Test Author',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
            duration_seconds=180,
        )

        response = self.client.get('/admin/tools/list/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Audio')
        # Description might be truncated, so just check title and author appear
        self.assertContains(response, 'Test Author')

    def test_list_view_filter_audio(self):
        """Test filtering list view by audio type"""
        MediaItem.objects.create(
            source_url='http://example.com/audio.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Audio Item',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
        )
        MediaItem.objects.create(
            source_url='http://example.com/video.mp4',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            slug='test-video',
            title='Video Item',
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get('/admin/tools/list/?type=audio')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Audio Item')
        self.assertNotContains(response, 'Video Item')


class AdminItemDetailViewTest(TestCase):
    """Test the item detail view (/admin/tools/item/<guid>/)"""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.login(username='admin', password='password')

    def test_item_detail_view_loads_audio(self):
        """Test that item detail page loads for audio"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
            content_path='content.mp3',
        )

        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/item_detail.html')
        self.assertContains(response, 'Test Audio')

    def test_item_detail_view_loads_video(self):
        """Test that item detail page loads for video"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp4',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            slug='test-video',
            title='Test Video',
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            status=MediaItem.STATUS_READY,
            content_path='content.mp4',
        )

        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Video')

    def test_item_detail_view_shows_player(self):
        """Test that item detail shows audio/video player"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            status=MediaItem.STATUS_READY,
            content_path='content.mp3',
        )

        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        self.assertEqual(response.status_code, 200)
        # Should contain audio or video tag
        self.assertIn(b'<audio', response.content)

    def test_item_detail_view_shows_thumbnail(self):
        """Test that item detail shows thumbnail if available"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp4',
            requested_type=MediaItem.REQUESTED_TYPE_VIDEO,
            slug='test-video',
            title='Test Video',
            media_type=MediaItem.MEDIA_TYPE_VIDEO,
            status=MediaItem.STATUS_READY,
            content_path='content.mp4',
            thumbnail_path='thumbnail.jpg',
        )

        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'thumbnail.jpg')

    def test_item_detail_view_404_for_invalid_guid(self):
        """Test that invalid GUID returns 404"""
        response = self.client.get('/admin/tools/item/invalid-guid-xyz/')
        self.assertEqual(response.status_code, 404)

    def test_item_detail_shows_generate_summary_when_no_summary(self):
        """Test that 'Generate Summary' button shows when subtitles exist but no summary"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            status=MediaItem.STATUS_READY,
            subtitle_path='subtitles.vtt',
        )

        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Generate Summary')
        self.assertNotContains(response, 'Regenerate Summary')

    def test_item_detail_shows_regenerate_summary_when_summary_exists(self):
        """Test that 'Regenerate Summary' button shows when summary already exists"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            status=MediaItem.STATUS_READY,
            subtitle_path='subtitles.vtt',
            summary='An existing summary of the episode.',
        )

        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Regenerate Summary')

    def test_item_detail_no_summary_button_without_subtitles(self):
        """Test that no summary button shows when there are no subtitles"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Generate Summary')
        self.assertNotContains(response, 'Regenerate Summary')

    def test_item_detail_view_requires_authentication(self):
        """Test that item detail requires login"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            status=MediaItem.STATUS_READY,
        )

        self.client.logout()
        response = self.client.get(f'/admin/tools/item/{item.guid}/')
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/admin/login/'))


class StashProgressViewTest(TestCase):
    """Test the progress view (/stash/<guid>/progress/)"""

    def setUp(self):
        self.client = Client()

    def test_progress_page_loads(self):
        """Test that progress page loads"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
            status=MediaItem.STATUS_DOWNLOADING,
        )

        response = self.client.get(f'/stash/{item.guid}/progress/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'media/stash_progress.html')

    def test_progress_page_shows_guid(self):
        """Test that progress page contains the GUID"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
            status=MediaItem.STATUS_DOWNLOADING,
        )

        response = self.client.get(f'/stash/{item.guid}/progress/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, item.guid)

    def test_progress_page_404_for_invalid_guid(self):
        """Test that invalid GUID returns 404"""
        response = self.client.get('/stash/invalid-guid-xyz/progress/')
        self.assertEqual(response.status_code, 404)


class SSEStatusStreamTest(TestCase):
    """Test the SSE endpoint (/stash/<guid>/status-stream/)"""

    def setUp(self):
        self.client = Client()

    def test_sse_endpoint_returns_stream(self):
        """Test that SSE endpoint returns correct response type and headers"""
        # Create item that's already READY so stream terminates immediately
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get(f'/stash/{item.guid}/stream/')

        # Verify response type and headers
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        self.assertEqual(response['Cache-Control'], 'no-cache')
        self.assertEqual(response['X-Accel-Buffering'], 'no')

    def test_sse_endpoint_returns_valid_data(self):
        """Test that SSE endpoint returns valid event data"""
        # Create item that's already READY so we can check the data format
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get(f'/stash/{item.guid}/stream/')

        # Get first event from stream
        chunks = list(response.streaming_content)
        self.assertGreater(len(chunks), 0)

        first_event = chunks[0].decode()

        # Verify SSE format: "data: {...}\n\n"
        self.assertTrue(first_event.startswith('data: '))
        # Extract JSON data (remove "data: " prefix and whitespace)
        json_str = first_event[6:].strip()
        data = json.loads(json_str)

        # Verify expected fields
        self.assertEqual(data['status'], MediaItem.STATUS_READY)  # Uppercase constant
        self.assertEqual(data['title'], 'Test Audio')
        self.assertTrue(data['is_ready'])
        self.assertFalse(data['has_error'])

    def test_sse_endpoint_completes_on_ready(self):
        """Test that SSE stream completes when item is READY"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='test-audio',
            title='Test Audio',
            status=MediaItem.STATUS_READY,
        )

        response = self.client.get(f'/stash/{item.guid}/stream/')

        # Get all events
        chunks = list(response.streaming_content)

        # Should have at least 2 chunks: data event + complete event
        self.assertGreaterEqual(len(chunks), 2)

        # Last chunk should be completion event
        last_event = chunks[-1].decode()
        self.assertIn('event: complete', last_event)

    def test_sse_endpoint_completes_on_error(self):
        """Test that SSE stream completes when item has ERROR"""
        item = MediaItem.objects.create(
            source_url='http://example.com/test.mp3',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
            title='Test Audio',
            status=MediaItem.STATUS_ERROR,
            error_message='Test error',
        )

        response = self.client.get(f'/stash/{item.guid}/stream/')

        # Get all events
        chunks = list(response.streaming_content)

        # Should complete quickly with error
        self.assertGreater(len(chunks), 0)

        # First data event should show error
        first_event = chunks[0].decode()
        json_str = first_event[6:].strip()
        data = json.loads(json_str)

        self.assertEqual(data['status'], MediaItem.STATUS_ERROR)  # Uppercase constant
        self.assertTrue(data['has_error'])
        self.assertEqual(data['error_message'], 'Test error')

    def test_sse_endpoint_handles_missing_item(self):
        """Test that SSE gracefully handles deleted items"""
        response = self.client.get('/stash/nonexistent-guid/stream/')

        chunks = list(response.streaming_content)

        # Should yield error event
        self.assertGreater(len(chunks), 0)
        first_chunk = chunks[0].decode()
        self.assertIn('event: error', first_chunk)
        self.assertIn('Item not found', first_chunk)


class AdminActionTest(TestCase):
    """Tests for the custom admin actions added for channels and publish dates"""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('admin2', 'admin2@test.com', 'password')
        self.client.login(username='admin2', password='password')

    def _post_action(self, url, action, pks):
        return self.client.post(
            url, {'action': action, '_selected_action': pks}, follow=True
        )

    def test_download_entire_channel_enqueues_scan(self):
        """The group action kicks off a background scan rather than blocking"""
        from media.models import MediaGroup

        group = MediaGroup.objects.create(
            name='Lekcje', youtube_channel_url='https://www.youtube.com/@lekcje'
        )

        with patch('media.tasks.sync_channel_full') as mock_task:
            response = self._post_action(
                '/admin/media/mediagroup/', 'download_entire_channel', [group.pk]
            )

        self.assertEqual(response.status_code, 200)
        mock_task.assert_called_once_with(group.pk)

    def test_download_entire_channel_skips_groups_without_a_channel(self):
        from media.models import MediaGroup

        plain = MediaGroup.objects.create(name='Bez kanalu')

        with patch('media.tasks.sync_channel_full') as mock_task:
            self._post_action(
                '/admin/media/mediagroup/', 'download_entire_channel', [plain.pk]
            )

        mock_task.assert_not_called()

    def test_refresh_publish_dates_enqueues_per_item(self):
        items = [
            MediaItem.objects.create(
                source_url=f'https://youtu.be/v{i}',
                requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
                slug=f'item-{i}',
                status=MediaItem.STATUS_READY,
            )
            for i in range(2)
        ]

        with patch('media.tasks.refresh_publish_date') as mock_task:
            response = self._post_action(
                '/admin/media/mediaitem/', 'refresh_publish_dates', [i.guid for i in items]
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_task.call_count, 2)

    def test_requeue_items_resets_attempts(self):
        item = MediaItem.objects.create(
            source_url='https://youtu.be/failed',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='failed',
            status=MediaItem.STATUS_ERROR,
            download_attempts=3,
            error_message='HTTP Error 403: Forbidden',
        )

        self._post_action('/admin/media/mediaitem/', 'requeue_items', [item.guid])

        item.refresh_from_db()
        self.assertEqual(item.status, MediaItem.STATUS_QUEUED)
        self.assertEqual(item.download_attempts, 0)
        self.assertEqual(item.error_message, '')


class AdminPublishDateColumnTest(TestCase):
    """Tests for the publication date column on the Media items changelist"""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('admin3', 'admin3@test.com', 'password')
        self.client.login(username='admin3', password='password')

        self.dated = MediaItem.objects.create(
            source_url='https://youtu.be/dated',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='dated',
            title='Ma date',
            status=MediaItem.STATUS_READY,
            publish_date=datetime.datetime(2014, 11, 10, 14, 5, 55, tzinfo=datetime.timezone.utc),
        )
        self.undated = MediaItem.objects.create(
            source_url='https://youtu.be/undated',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='undated',
            title='Bez daty',
            status=MediaItem.STATUS_READY,
        )

    def test_column_shows_the_date(self):
        response = self.client.get('/admin/media/mediaitem/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Published', html)
        self.assertIn('2014-11-10', html)

    def test_missing_date_is_marked_explicitly(self):
        """An empty cell would not tell you whether the date is missing"""
        html = self.client.get('/admin/media/mediaitem/').content.decode()
        self.assertIn('not set', html)

    def test_renders_without_a_date_present(self):
        """Regression: the "not set" branch used to raise TypeError from format_html,
        which broke the whole changelist for anyone with an undated item."""
        self.dated.delete()
        response = self.client.get('/admin/media/mediaitem/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('not set', response.content.decode())

    def test_can_filter_to_items_missing_a_date(self):
        """Pairs with the "Fetch publication date from source" action"""
        html = self.client.get(
            '/admin/media/mediaitem/?publish_date__isempty=1'
        ).content.decode()
        self.assertIn('Bez daty', html)
        self.assertNotIn('Ma date', html)

    def test_can_filter_to_items_having_a_date(self):
        html = self.client.get(
            '/admin/media/mediaitem/?publish_date__isempty=0'
        ).content.decode()
        self.assertIn('Ma date', html)
        self.assertNotIn('Bez daty', html)

    def test_column_is_sortable(self):
        from media.admin import MediaItemAdmin

        self.assertEqual(
            MediaItemAdmin.publish_date_display.admin_order_field, 'publish_date'
        )


class ProxyHttpsUrlTest(TestCase):
    """Tests that generated URLs respect X-Forwarded-Proto from the reverse proxy.

    The app runs behind a TLS-terminating proxy, so requests reach Django over plain
    HTTP. SECURE_PROXY_SSL_HEADER makes Django trust X-Forwarded-Proto; these tests
    pin that behaviour so absolute URLs do not silently revert to http://.
    """

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser('proxy', 'proxy@test.com', 'password')
        self.client.login(username='proxy', password='password')

    def test_feed_links_page_uses_https_behind_proxy(self):
        response = self.client.get(
            '/admin/tools/feeds/',
            HTTP_HOST='testserver',
            HTTP_X_FORWARDED_PROTO='https',
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('https://testserver/feeds/', html)
        self.assertNotIn('http://testserver/feeds/', html)

    def test_feed_links_page_stays_http_without_the_header(self):
        """Without the header we must not claim https - that would break local use"""
        html = self.client.get('/admin/tools/feeds/', HTTP_HOST='testserver').content.decode()
        self.assertIn('http://testserver/feeds/', html)

    def test_feed_enclosure_urls_use_https_behind_proxy(self):
        """Media enclosures matter most: http:// URLs in an https feed are mixed content"""
        MediaItem.objects.create(
            source_url='https://youtu.be/v1',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug='odcinek',
            title='Odcinek',
            status=MediaItem.STATUS_READY,
            content_path='odcinek.m4a',
            file_size=1234,
            downloaded_at=timezone.now(),
        )

        response = self.client.get(
            '/feeds/audio.xml',
            HTTP_HOST='testserver',
            HTTP_X_FORWARDED_PROTO='https',
        )
        self.assertEqual(response.status_code, 200)
        xml = response.content.decode()
        self.assertNotIn('http://testserver', xml)
        self.assertIn('https://testserver', xml)
