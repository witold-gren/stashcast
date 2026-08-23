"""
Management command to make downloaded videos playable on Apple Podcasts / iOS.

Episodes downloaded while the format selector still allowed Opus audio ended up in a
Matroska (.mkv) container, which Apple Podcasts refuses with "Cannot play this episode
on this device". This rewrites them as MP4 with H.264 video and AAC audio, copying the
video stream where possible so it is far cheaper than re-downloading.

Examples:
    ./manage.py repair_videos             # repair every affected video
    ./manage.py repair_videos -n 5        # only the 5 most recent
    ./manage.py repair_videos --dry-run   # list what would be repaired
"""

from django.core.management.base import BaseCommand

from media.models import MediaItem
from media.service.media_info import is_ios_compatible_video, probe_codecs
from media.tasks import repair_incompatible_videos


class Command(BaseCommand):
    help = 'Repack downloaded videos as MP4/H.264/AAC for Apple Podcasts and iOS'

    def add_arguments(self, parser):
        parser.add_argument(
            '-n',
            '--limit',
            type=int,
            default=None,
            help='Maximum number of items to process (default: all)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List the affected videos without rewriting anything',
        )

    def handle(self, *args, **options):
        if options['dry_run']:
            self._dry_run(options['limit'])
            return

        def log(message):
            self.stdout.write(message)

        self.stdout.write('Checking downloaded videos...')
        repaired, skipped = repair_incompatible_videos(limit=options['limit'], logger=log)
        self.stdout.write(
            self.style.SUCCESS(f'✓ {repaired} repaired, {skipped} already fine or skipped')
        )

    def _dry_run(self, limit):
        """Print which videos would be rewritten and why."""
        items = MediaItem.objects.filter(
            media_type=MediaItem.MEDIA_TYPE_VIDEO, status=MediaItem.STATUS_READY
        ).order_by('-downloaded_at')
        if limit:
            items = items[: int(limit)]

        affected = 0
        for item in items:
            path = item.get_absolute_content_path()
            if not path:
                continue
            from pathlib import Path

            if not Path(path).exists():
                self.stdout.write(f'  [missing file] {item.title or item.source_url}')
                continue
            if is_ios_compatible_video(path):
                continue
            codecs = probe_codecs(path)
            affected += 1
            self.stdout.write(
                f'  [{Path(path).suffix} {codecs["video_codec"]}/{codecs["audio_codec"]}] '
                f'{item.title or item.source_url}'
            )

        self.stdout.write(f'Would repair {affected} video(s)')
