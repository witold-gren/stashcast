"""
Management command to remove orphaned media directories.

An orphan is a directory in the media folder with no MediaItem pointing at it. These
were left behind by deletions made while the cleanup signal was not registered (see
MediaConfig.ready), so the database row went away but the files stayed on disk.

Deleting media items now removes their files automatically; this is for cleaning up
what earlier deletions left behind.

Examples:
    ./manage.py cleanup_orphans              # list orphans, ask before deleting
    ./manage.py cleanup_orphans --dry-run    # only list them
    ./manage.py cleanup_orphans --force      # delete without asking
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from media.models import MediaItem

# Directories in the media folder that are never media-item folders.
# group-images holds Django-uploaded group cover art - MEDIA_ROOT is the same directory
# as STASHCAST_MEDIA_DIR, so deleting it would destroy every group's image.
PROTECTED_NAMES = {'group-images'}

# Prefixes owned by in-flight work or by ./manage.py cleanup_tmp
PROTECTED_PREFIXES = ('tmp-', 'batch-')


def find_orphan_dirs():
    """Return media directories that no MediaItem refers to.

    Returns:
        list[Path]: Orphaned directories, sorted by name.
    """
    media_dir = Path(settings.STASHCAST_MEDIA_DIR)
    if not media_dir.exists():
        return []

    known_slugs = set(MediaItem.objects.exclude(slug='').values_list('slug', flat=True))

    orphans = []
    for entry in media_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in PROTECTED_NAMES:
            continue
        if entry.name.startswith(PROTECTED_PREFIXES):
            continue
        if entry.name in known_slugs:
            continue
        orphans.append(entry)

    return sorted(orphans, key=lambda p: p.name)


def dir_size(path):
    """Total size in bytes of everything under ``path``."""
    return sum(f.stat().st_size for f in Path(path).rglob('*') if f.is_file())


class Command(BaseCommand):
    help = 'Remove media directories left behind by deleted items'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without deleting anything',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Delete without asking for confirmation',
        )

    def handle(self, *args, **options):
        orphans = find_orphan_dirs()

        if not orphans:
            self.stdout.write(self.style.SUCCESS('No orphaned directories found'))
            return

        total = 0
        self.stdout.write(f'Found {len(orphans)} orphaned director(ies):')
        for path in orphans:
            size = dir_size(path)
            total += size
            files = sum(1 for f in path.rglob('*') if f.is_file())
            self.stdout.write(f'  {path.name}  ({files} file(s), {size / (1024 * 1024):.1f} MB)')

        self.stdout.write(f'Total: {total / (1024 * 1024):.1f} MB')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('Dry run - nothing deleted'))
            return

        if not options['force']:
            answer = input(f'Delete these {len(orphans)} director(ies)? [y/N] ')
            if answer.strip().lower() not in ('y', 'yes'):
                self.stdout.write('Aborted')
                return

        deleted = 0
        for path in orphans:
            try:
                shutil.rmtree(path)
                deleted += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  Failed to delete {path.name}: {e}'))

        freed = total / (1024 * 1024)
        self.stdout.write(
            self.style.SUCCESS(f'✓ Deleted {deleted} director(ies), freed {freed:.1f} MB')
        )
