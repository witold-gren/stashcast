"""
Management command to check group YouTube channels for new uploads.

Runs the same sync used by the periodic Huey task, but in the foreground. Useful
for testing a channel URL or triggering an immediate check without waiting for the
next scheduled run.

New uploads are added to the paced download queue (status QUEUED) instead of being
enqueued all at once, so a channel with a large backlog cannot flood the workers. Use
``./manage.py download_queue`` to watch that queue.

Examples:
    ./manage.py sync_youtube                 # sync all groups (queue downloads)
    ./manage.py sync_youtube --group lekcje  # sync a single group by slug
    ./manage.py sync_youtube --wait          # download synchronously (blocking)
    ./manage.py sync_youtube --max 10        # consider the 10 most recent uploads
    ./manage.py sync_youtube --all           # queue the channel's ENTIRE back-catalogue
    ./manage.py sync_youtube --immediate     # enqueue everything at once (old behaviour)
"""

from django.core.management.base import BaseCommand, CommandError

from media.models import MediaGroup
from media.operations import sync_all_youtube_channels, sync_group_channel


class Command(BaseCommand):
    help = 'Check group YouTube channels for new uploads and stash them as audio'

    def add_arguments(self, parser):
        parser.add_argument(
            '--group',
            type=str,
            default=None,
            help='Sync only the group with this slug (default: all configured groups)',
        )
        parser.add_argument(
            '--wait',
            action='store_true',
            help='Download synchronously instead of enqueuing background tasks',
        )
        parser.add_argument(
            '--immediate',
            action='store_true',
            help='Enqueue all new uploads at once instead of using the paced queue',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            dest='all_videos',
            help='Queue every upload on the channel, not just the most recent ones',
        )
        parser.add_argument(
            '--max',
            type=int,
            default=None,
            dest='max_videos',
            help='How many recent uploads to consider (default: STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS)',
        )

    def handle(self, *args, **options):
        slug = options['group']
        wait = options['wait']
        immediate = options['immediate']
        max_videos = options['max_videos']

        # 0 means "no cap" all the way down to list_channel_videos
        if options['all_videos']:
            if max_videos:
                raise CommandError('Use either --all or --max, not both')
            max_videos = 0

        def log(message):
            self.stdout.write(message)

        if slug:
            try:
                group = MediaGroup.objects.get(slug=slug)
            except MediaGroup.DoesNotExist:
                raise CommandError(f'No group with slug "{slug}"')
            if not group.youtube_channel_url:
                raise CommandError(f'Group "{group.name}" has no YouTube channel configured')
            items = sync_group_channel(
                group, max_videos=max_videos, wait=wait, logger=log, immediate=immediate
            )
        else:
            items = sync_all_youtube_channels(wait=wait, logger=log, immediate=immediate)

        if wait or immediate:
            self.stdout.write(self.style.SUCCESS(f'✓ Stashed {len(items)} new upload(s)'))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'✓ Queued {len(items)} new upload(s) - '
                    'see ./manage.py download_queue for progress'
                )
            )
