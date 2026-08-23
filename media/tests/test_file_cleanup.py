"""
Tests that deleting a MediaItem also removes its files from disk.

The cleanup receiver in media/signals.py existed but was never imported, so it was not
connected: deleting items left their whole directory behind. These tests pin both the
registration and the behaviour, because the failure mode is completely silent - the
delete succeeds and only the disk usage tells you something is wrong.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models.signals import pre_delete
from django.test import Client, TestCase
from django.utils import timezone

from media.management.commands.cleanup_orphans import find_orphan_dirs
from media.models import MediaGroup, MediaItem

User = get_user_model()


class MediaItemFileCleanupTest(TestCase):
    """Tests the pre_delete cleanup across every delete path"""

    def _item_with_files(self, slug='odcinek'):
        item = MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug=slug,
            title=slug,
            status=MediaItem.STATUS_READY,
            content_path='content.m4a',
            downloaded_at=timezone.now(),
        )
        base = Path(settings.STASHCAST_MEDIA_DIR) / slug
        base.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, base, True)
        for name in ('content.m4a', 'thumbnail.png', 'download.log', 'subtitles.vtt'):
            (base / name).write_bytes(b'x' * 128)
        return item, base

    def test_cleanup_receiver_is_registered(self):
        """Regression guard: the receiver module used to never be imported"""
        receivers = pre_delete._live_receivers(MediaItem)[0]
        names = [getattr(r, '__name__', '') for r in receivers]
        self.assertIn('cleanup_media_files', names)

    def test_single_delete_removes_the_directory(self):
        item, base = self._item_with_files()

        item.delete()

        self.assertFalse(base.exists())

    def test_bulk_delete_removes_the_directory(self):
        """queryset.delete() never calls Model.delete(), which is why pre_delete is used"""
        item, base = self._item_with_files('odcinek-bulk')

        MediaItem.objects.filter(pk=item.pk).delete()

        self.assertFalse(base.exists())

    def test_admin_delete_selected_removes_the_directory(self):
        item, base = self._item_with_files('odcinek-admin')
        User.objects.create_superuser('deladmin', 'del@test.com', 'password')
        client = Client()
        client.login(username='deladmin', password='password')

        client.post(
            '/admin/media/mediaitem/',
            {'action': 'delete_selected', '_selected_action': [item.guid], 'post': 'yes'},
            follow=True,
        )

        self.assertFalse(MediaItem.objects.filter(pk=item.pk).exists())
        self.assertFalse(base.exists())

    def test_deleting_an_unfinished_item_does_not_raise(self):
        """An item still on slug='pending' has no directory of its own"""
        item = MediaItem.objects.create(
            source_url='https://youtu.be/pending',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='pending',
        )

        item.delete()

        self.assertFalse(MediaItem.objects.filter(pk=item.pk).exists())

    def test_only_the_deleted_items_directory_is_removed(self):
        keep, keep_base = self._item_with_files('zostaje')
        drop, drop_base = self._item_with_files('znika')

        drop.delete()

        self.assertFalse(drop_base.exists())
        self.assertTrue(keep_base.exists())


class FindOrphanDirsTest(TestCase):
    """Tests which directories cleanup_orphans considers safe to delete"""

    def setUp(self):
        self.media = Path(settings.STASHCAST_MEDIA_DIR)
        self.media.mkdir(parents=True, exist_ok=True)

    def _make_dir(self, name):
        path = self.media / name
        path.mkdir(parents=True, exist_ok=True)
        (path / 'a-file').write_bytes(b'x')
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def _names(self):
        return {p.name for p in find_orphan_dirs()}

    def test_orphan_directory_is_found(self):
        self._make_dir('osierocony')
        self.assertIn('osierocony', self._names())

    def test_directory_of_an_existing_item_is_kept(self):
        MediaItem.objects.create(
            source_url='https://youtu.be/keep',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='istniejacy',
            status=MediaItem.STATUS_READY,
        )
        self._make_dir('istniejacy')

        self.assertNotIn('istniejacy', self._names())

    def test_group_images_is_protected(self):
        """MEDIA_ROOT is the same directory, so this holds every group's cover art"""
        self._make_dir('group-images')
        self.assertNotIn('group-images', self._names())

    def test_in_flight_directories_are_protected(self):
        self._make_dir('tmp-abc123')
        self._make_dir('batch-xyz789')

        names = self._names()
        self.assertNotIn('tmp-abc123', names)
        self.assertNotIn('batch-xyz789', names)

    def test_queued_item_directory_is_kept(self):
        """A queued item already owns its slug before anything is downloaded"""
        MediaItem.objects.create(
            source_url='https://youtu.be/queued',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            slug='w-kolejce',
            status=MediaItem.STATUS_QUEUED,
        )
        self._make_dir('w-kolejce')

        self.assertNotIn('w-kolejce', self._names())

    def test_files_are_not_reported_as_directories(self):
        stray = self.media / 'stray-file.txt'
        stray.write_text('x')
        self.addCleanup(stray.unlink, True)

        self.assertNotIn('stray-file.txt', self._names())

    def test_group_image_upload_survives_orphan_scan(self):
        """End-to-end: a real group image must never look like an orphan"""
        MediaGroup.objects.create(name='Lekcje')
        uploads = self._make_dir('group-images')
        (uploads / 'cover.png').write_bytes(b'PNG')

        self.assertNotIn('group-images', self._names())
        self.assertTrue((uploads / 'cover.png').exists())
