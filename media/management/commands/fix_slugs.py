"""
Management command to bring existing directory names in line with the current rules.

Slugs used to be built by stripping every non-ASCII character, which took pieces out of
accented words: "Przestań być łatwym" became "przesta-by-atwym". Titles are unchanged in
the database, so the readable name can simply be recomputed - and the directory on disk
renamed to match.

The database row and the directory are kept in step: the rename happens first, and the
slug is only saved once it succeeded.

Examples:
    ./manage.py fix_slugs --dry-run   # list what would be renamed
    ./manage.py fix_slugs             # rename, asking for confirmation first
    ./manage.py fix_slugs --force     # rename without asking
"""

from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from media.models import MediaItem
from media.utils import ensure_unique_slug, generate_slug

# Items a worker may be writing to right now - renaming those would pull the directory
# out from under an in-flight download.
BUSY_STATUSES = (
    MediaItem.STATUS_QUEUED,
    *MediaItem.IN_PROGRESS_STATUSES,
)


class Command(BaseCommand):
    help = 'Rename item directories to match the current slug rules'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be renamed without touching anything',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Rename without asking for confirmation',
        )
        parser.add_argument(
            '-n',
            '--limit',
            type=int,
            default=None,
            help='Maximum number of items to process',
        )

    def handle(self, *args, **options):
        planned, busy = self._plan(options['limit'])

        if busy:
            self.stdout.write(
                self.style.WARNING(f'Skipping {busy} item(s) currently being downloaded')
            )

        if not planned:
            self.stdout.write(self.style.SUCCESS('All directory names are already up to date'))
            return

        self.stdout.write(f'{len(planned)} item(s) would be renamed:')
        for item, new_slug, directory in planned:
            on_disk = ' (no directory on disk)' if directory is None else ''
            self.stdout.write(f'  {item.slug}')
            self.stdout.write(f'    -> {new_slug}{on_disk}')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('Dry run - nothing changed'))
            return

        if not options['force']:
            answer = input(f'Rename these {len(planned)} item(s)? [y/N] ')
            if answer.strip().lower() not in ('y', 'yes'):
                self.stdout.write('Aborted')
                return

        renamed, failed = self._apply(planned)
        self.stdout.write(self.style.SUCCESS(f'✓ Renamed {renamed} item(s)'))
        if failed:
            self.stdout.write(self.style.ERROR(f'{failed} item(s) could not be renamed'))

    def _plan(self, limit):
        """Work out which items need a new slug.

        Returns:
            tuple[list, int]: [(item, new_slug, current directory or None)], skipped count
        """
        items = MediaItem.objects.exclude(slug='').exclude(slug='pending').order_by('slug')
        planned = []
        busy = 0
        # Names handed out earlier in this same run. ensure_unique_slug only knows what
        # the database holds right now, so without this two items whose titles produce
        # the same slug would both be promised it.
        claimed = set()

        for item in items:
            if item.status in BUSY_STATUSES:
                busy += 1
                continue
            if not item.title:
                continue

            wanted = generate_slug(item.title)
            if not wanted or wanted == item.slug:
                continue

            # Another item may already hold the readable name
            unique = ensure_unique_slug(wanted, item.source_url, None, item.media_type)
            while unique in claimed:
                unique = ensure_unique_slug(
                    f'{wanted}-{len(claimed)}', item.source_url, None, item.media_type
                )
            if unique == item.slug:
                continue
            claimed.add(unique)

            directory = item.get_base_dir()
            planned.append((item, unique, directory if directory and directory.exists() else None))

            if limit and len(planned) >= int(limit):
                break

        return planned, busy

    def _apply(self, planned):
        """Rename the directories and save the new slugs."""
        renamed = 0
        failed = 0

        for item, new_slug, directory in planned:
            target = Path(item.get_base_dir()).parent / new_slug if directory else None

            try:
                if directory is not None:
                    if target.exists():
                        raise FileExistsError(f'{target} already exists')
                    directory.rename(target)

                # Only record the new name once the directory really moved
                with transaction.atomic():
                    item.slug = new_slug
                    item.save(update_fields=['slug', 'updated_at'])

                renamed += 1
                self.stdout.write(f'  {new_slug}')
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f'  {item.slug}: {e}'))

        return renamed, failed
