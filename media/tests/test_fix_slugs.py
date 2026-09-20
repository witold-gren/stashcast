"""
Tests for bringing existing directory names in line with the current slug rules.

Slugs used to be built by stripping non-ASCII characters, which took pieces out of
accented words. Titles were never damaged, so the readable name can be recomputed - but
the directory on disk has to move with it, or the database stops matching the files.
"""

import shutil
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from media.models import MediaItem


class FixSlugsTest(TestCase):
    def _item(self, slug, title, status=MediaItem.STATUS_READY, with_files=True):
        item = MediaItem.objects.create(
            source_url=f'https://youtu.be/{slug}',
            requested_type=MediaItem.REQUESTED_TYPE_AUDIO,
            media_type=MediaItem.MEDIA_TYPE_AUDIO,
            slug=slug,
            title=title,
            status=status,
            content_path='content.m4a',
            downloaded_at=timezone.now(),
        )
        if with_files:
            base = Path(settings.STASHCAST_MEDIA_DIR) / slug
            base.mkdir(parents=True, exist_ok=True)
            (base / 'content.m4a').write_bytes(b'x' * 64)
            self.addCleanup(shutil.rmtree, base, True)
        self.addCleanup(
            shutil.rmtree, Path(settings.STASHCAST_MEDIA_DIR) / 'zazolc-gesla-jazn', True
        )
        return item

    def _run(self, *args):
        out = StringIO()
        call_command('fix_slugs', *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_directory_and_slug_move_together(self):
        item = self._item('zaz-gesl-jazn', 'Zażółć gęślą jaźń')
        media = Path(settings.STASHCAST_MEDIA_DIR)

        self._run('--force')

        item.refresh_from_db()
        self.assertEqual(item.slug, 'zazolc-gesla-jazn')
        self.assertTrue((media / 'zazolc-gesla-jazn' / 'content.m4a').exists())
        self.assertFalse((media / 'zaz-gesl-jazn').exists())

    def test_item_stays_reachable_afterwards(self):
        """The whole point: the database must still find the files"""
        item = self._item('zaz-gesl-jazn', 'Zażółć gęślą jaźń')

        self._run('--force')

        item.refresh_from_db()
        self.assertTrue(item.get_absolute_content_path().exists())

    def test_dry_run_changes_nothing(self):
        item = self._item('zaz-gesl-jazn', 'Zażółć gęślą jaźń')

        output = self._run('--dry-run')

        item.refresh_from_db()
        self.assertEqual(item.slug, 'zaz-gesl-jazn')
        self.assertIn('zazolc-gesla-jazn', output)
        self.assertIn('nothing changed', output)

    def test_downloading_items_are_left_alone(self):
        """Renaming a directory a worker is writing to would break the download"""
        item = self._item(
            'zaz-gesl-jazn', 'Zażółć gęślą jaźń', status=MediaItem.STATUS_DOWNLOADING
        )

        self._run('--force')

        item.refresh_from_db()
        self.assertEqual(item.slug, 'zaz-gesl-jazn')

    def test_queued_items_are_left_alone(self):
        item = self._item('zaz-gesl-jazn', 'Zażółć gęślą jaźń', status=MediaItem.STATUS_QUEUED)

        self._run('--force')

        item.refresh_from_db()
        self.assertEqual(item.slug, 'zaz-gesl-jazn')

    def test_correct_slugs_are_not_touched(self):
        item = self._item('zazolc-gesla-jazn', 'Zażółć gęślą jaźń')

        output = self._run('--force')

        item.refresh_from_db()
        self.assertEqual(item.slug, 'zazolc-gesla-jazn')
        self.assertIn('already up to date', output)

    def test_two_items_wanting_the_same_name_do_not_collide(self):
        """Uniqueness has to account for names handed out earlier in the same run"""
        first = self._item('stary-1', 'Zażółć gęślą jaźń')
        second = self._item('stary-2', 'Zażółć gęślą jaźń')

        self._run('--force')

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.slug, second.slug)
        self.assertEqual(MediaItem.objects.filter(slug=first.slug).count(), 1)

    def test_item_without_files_still_gets_its_slug_fixed(self):
        item = self._item('zaz-gesl-jazn', 'Zażółć gęślą jaźń', with_files=False)

        self._run('--force')

        item.refresh_from_db()
        self.assertEqual(item.slug, 'zazolc-gesla-jazn')

    def test_untitled_items_are_skipped(self):
        item = self._item('jakis-slug', '', with_files=False)

        self._run('--force')

        item.refresh_from_db()
        self.assertEqual(item.slug, 'jakis-slug')

    def test_limit_is_respected(self):
        self._item('stary-1', 'Zażółć gęślą jaźń', with_files=False)
        self._item('stary-2', 'Przestań być łatwym celem', with_files=False)

        self._run('--force', '-n', '1')

        changed = MediaItem.objects.exclude(slug__startswith='stary-').count()
        self.assertEqual(changed, 1)
