"""
Management command to find downloads that are shorter than they should be.

Every item carries the duration the source reported (``duration_seconds``). This
measures how long the downloaded file actually plays for and stores it, so incomplete
downloads can be found in bulk instead of one at a time - and filtered in the admin.

A gap of a second or two is normal (containers round, encoders pad). A larger gap means
the download was cut short and the item should be fetched again.

Examples:
    ./manage.py check_durations                  # measure everything, report the bad ones
    ./manage.py check_durations --only-unchecked # skip items already measured
    ./manage.py check_durations --tolerance 5    # allow a 5 second gap
    ./manage.py check_durations -n 50            # only the 50 most recent
    ./manage.py check_durations --requeue        # also requeue the bad ones for download
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from media.tasks import check_durations, queue_items_for_download


class Command(BaseCommand):
    help = 'Compare each download against the duration reported by its source'

    def add_arguments(self, parser):
        parser.add_argument(
            '-n',
            '--limit',
            type=int,
            default=None,
            help='Maximum number of items to check (default: all)',
        )
        parser.add_argument(
            '--tolerance',
            type=int,
            default=None,
            help='Allowed gap in seconds '
            f'(default: STASHCAST_DURATION_TOLERANCE_SECONDS = '
            f'{settings.STASHCAST_DURATION_TOLERANCE_SECONDS})',
        )
        parser.add_argument(
            '--only-unchecked',
            action='store_true',
            help='Skip items whose file duration has already been measured',
        )
        parser.add_argument(
            '--requeue',
            action='store_true',
            help='Put the mismatched items back into the paced download queue',
        )

    def handle(self, *args, **options):
        def log(message):
            self.stdout.write(message)

        tolerance = options['tolerance']
        if tolerance is None:
            tolerance = settings.STASHCAST_DURATION_TOLERANCE_SECONDS

        self.stdout.write(f'Measuring downloaded files (tolerance {tolerance}s)...')
        checked, mismatched, skipped = check_durations(
            limit=options['limit'],
            only_unchecked=options['only_unchecked'],
            tolerance=tolerance,
            logger=log,
        )

        if not mismatched:
            self.stdout.write(
                self.style.SUCCESS(f'✓ {checked} file(s) checked, all within {tolerance}s')
            )
            return

        self.stdout.write('')
        self.stdout.write(self.style.WARNING(f'{len(mismatched)} incomplete file(s):'))
        for item in mismatched:
            self.stdout.write(
                f'  expected {item.duration_seconds}s, got {item.file_duration_seconds}s '
                f'(short by {item.duration_gap_seconds}s) - {item.title or item.source_url}'
            )

        if not options['requeue']:
            self.stdout.write('')
            self.stdout.write(
                'Re-run with --requeue to download them again, or filter by '
                '"Duration: incomplete" in the admin.'
            )
            return

        queued = queue_items_for_download(item.guid for item in mismatched)
        self.stdout.write(
            self.style.SUCCESS(f'✓ Requeued {queued} item(s) for the paced download queue')
        )
