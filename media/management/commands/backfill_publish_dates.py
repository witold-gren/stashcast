"""
Management command to fill in missing publication dates.

Items downloaded before the publication date was captured have an empty
``publish_date``, so podcast feeds date them by when they were downloaded instead of
when they were published. This re-queries the source platform for those items and
stores the real date. Metadata only - nothing is re-downloaded.

Examples:
    ./manage.py backfill_publish_dates              # fill in every undated item
    ./manage.py backfill_publish_dates -n 20        # only the 20 most recent
    ./manage.py backfill_publish_dates --dry-run    # show what would be updated
    ./manage.py backfill_publish_dates --all        # refresh dates on every item
"""

from django.core.management.base import BaseCommand

from media.models import MediaItem
from media.tasks import backfill_publish_dates


class Command(BaseCommand):
    help = 'Fetch missing publication dates from the source platform'

    def add_arguments(self, parser):
        parser.add_argument(
            '-n',
            '--limit',
            type=int,
            default=None,
            help='Maximum number of items to process (default: all)',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            dest='refresh_all',
            help='Refresh every item, not just the ones missing a date',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List the items that would be processed without contacting the source',
        )

    def handle(self, *args, **options):
        only_missing = not options['refresh_all']
        limit = options['limit']

        if options['dry_run']:
            self._dry_run(only_missing, limit)
            return

        def log(message):
            self.stdout.write(message)

        self.stdout.write('Fetching publication dates from the source...')
        updated, skipped = backfill_publish_dates(
            limit=limit, only_missing=only_missing, logger=log
        )
        self.stdout.write(
            self.style.SUCCESS(f'✓ {updated} date(s) updated, {skipped} skipped')
        )

    def _dry_run(self, only_missing, limit):
        """Print the items that would be processed."""
        items = MediaItem.objects.exclude(status=MediaItem.STATUS_QUEUED)
        if only_missing:
            items = items.filter(publish_date__isnull=True)
        items = items.order_by('-downloaded_at')
        total = items.count()
        if limit:
            items = items[: int(limit)]

        self.stdout.write(f'Would process {min(total, limit) if limit else total} of {total}:')
        for item in items:
            current = f'{item.publish_date:%Y-%m-%d}' if item.publish_date else 'no date'
            self.stdout.write(f'  [{current}] {item.title or item.source_url}')
