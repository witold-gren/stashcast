"""
Management command to inspect and drive the paced download queue.

Items found by the YouTube channel sync wait in status QUEUED and are released a
few at a time by the periodic task, so a channel with many uploads cannot flood the
Huey workers. This command shows that queue and lets you push it along by hand.

Examples:
    ./manage.py download_queue                  # show queue status
    ./manage.py download_queue --run-now        # release one batch immediately
    ./manage.py download_queue --run-now -n 5   # release 5 items immediately
    ./manage.py download_queue --recover        # requeue items abandoned mid-download
    ./manage.py download_queue --retry-errors   # give every failed item a fresh start
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from media.models import MediaItem
from media.tasks import (
    recover_stuck_items,
    release_download_queue,
    retry_failed_items,
    worker_is_alive,
)


class Command(BaseCommand):
    help = 'Inspect and drive the paced download queue'

    def add_arguments(self, parser):
        parser.add_argument(
            '--run-now',
            action='store_true',
            help='Release a batch of queued items immediately instead of waiting',
        )
        parser.add_argument(
            '-n',
            '--limit',
            type=int,
            default=None,
            help='How many items to release with --run-now '
            '(default: STASHCAST_DOWNLOAD_QUEUE_BATCH)',
        )
        parser.add_argument(
            '--recover',
            action='store_true',
            help='Requeue items left behind by a worker that died mid-download',
        )
        parser.add_argument(
            '--retry-errors',
            action='store_true',
            help='Reset every ERROR item to QUEUED with a fresh set of attempts',
        )

    def handle(self, *args, **options):
        def log(message):
            self.stdout.write(message)

        if options['recover']:
            self.stdout.write('Recovering stuck items...')
            recovered = recover_stuck_items(logger=log)
            self.stdout.write(self.style.SUCCESS(f'✓ Recovered {len(recovered)} item(s)'))

        if options['retry_errors']:
            self.stdout.write('Requeuing failed items...')
            requeued = retry_failed_items(logger=log)
            self.stdout.write(self.style.SUCCESS(f'✓ Requeued {len(requeued)} item(s)'))

        if options['run_now']:
            self.stdout.write('Releasing queued items...')
            released = release_download_queue(limit=options['limit'], logger=log)
            self.stdout.write(self.style.SUCCESS(f'✓ Released {len(released)} item(s)'))

        self._show_status()

    def _show_status(self):
        """Print queue counts, the next few items, and worker liveness."""
        now = timezone.now()

        queued = MediaItem.objects.filter(status=MediaItem.STATUS_QUEUED)
        due = queued.filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        in_progress = MediaItem.objects.filter(status__in=MediaItem.IN_PROGRESS_STATUSES)
        errored = MediaItem.objects.filter(status=MediaItem.STATUS_ERROR)

        interval = settings.STASHCAST_DOWNLOAD_QUEUE_MINUTES
        batch = settings.STASHCAST_DOWNLOAD_QUEUE_BATCH

        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('Download queue'))
        self.stdout.write(f'  Rate:        {batch} item(s) every {interval} min')
        self.stdout.write(f'  Max tries:   {settings.STASHCAST_DOWNLOAD_MAX_ATTEMPTS}')
        self.stdout.write(f'  Queued:      {queued.count()} ({due.count()} due now)')
        self.stdout.write(f'  In progress: {in_progress.count()}')
        self.stdout.write(f'  Failed:      {errored.count()}')

        if queued.exists():
            # Estimate assuming the queue keeps releasing at its configured rate
            cycles = (queued.count() + batch - 1) // batch
            self.stdout.write(f'  Drains in:   ~{cycles * interval} min at the current rate')

            self.stdout.write('')
            self.stdout.write('  Next up:')
            for item in queued.order_by('created_at')[:5]:
                when = 'now'
                if item.next_attempt_at and item.next_attempt_at > now:
                    when = f'at {item.next_attempt_at:%H:%M}'
                tries = f' (try {item.download_attempts + 1})' if item.download_attempts else ''
                self.stdout.write(f'    - {item.title or item.source_url} [{when}]{tries}')

        self.stdout.write('')
        if worker_is_alive():
            self.stdout.write(self.style.SUCCESS('  Worker: alive'))
        else:
            self.stdout.write(
                self.style.WARNING(
                    '  Worker: no recent heartbeat - start it with: python manage.py run_huey'
                )
            )
