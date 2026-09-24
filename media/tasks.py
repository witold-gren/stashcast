import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import List

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from huey import crontab, signals
from huey.contrib.djhuey import db_periodic_task, db_task, signal

from media.models import MediaItem
from media.processing import (
    apply_download_info,
    download_direct,
    download_ytdlp,
    ensure_playable_video,
    prefetch_file,
    prefetch_direct,
    prefetch_ytdlp,
    process_files,
    write_log,
)
from media.progress_tracker import update_progress
from media.service.config import get_ytdlp_args_for_type
from media.service.download import prefetch_ytdlp_batch, download_ytdlp_batch
from media.service.strategy import choose_download_strategy
from media.utils import generate_slug, ensure_unique_slug


def check_episode_limit():
    """Check if the episode limit has been reached.

    Returns None if OK, or an error message string if at capacity.
    """
    max_episodes = settings.STASHCAST_MAX_EPISODES
    if max_episodes <= 0:
        return None
    current_count = MediaItem.objects.filter(status=MediaItem.STATUS_READY).count()
    if current_count >= max_episodes:
        return (
            f'Episode limit reached ({current_count}/{max_episodes}). '
            f'Delete existing episodes to make room for new downloads.'
        )
    return None


def _backoff_delay_minutes(attempts):
    """Minutes to wait before retry number ``attempts``, growing exponentially.

    Starts at the queue interval and doubles per attempt, capped at 6 hours so a
    permanently broken URL cannot push its retry arbitrarily far into the future.
    """
    base = max(1, int(settings.STASHCAST_DOWNLOAD_QUEUE_MINUTES))
    return min(base * (2 ** max(0, attempts - 1)), 6 * 60)


def schedule_retry_or_fail(item, reason):
    """Requeue a failed item, or mark it ERROR once its attempts are exhausted.

    How long to wait - and whether to bother at all - depends on why the download
    failed. A members-only or deleted video is never going to succeed, so it fails
    immediately instead of burning the whole retry budget; a premiere is retried around
    the time it actually airs; a bot check is given a long rest.

    Args:
        item: MediaItem whose download_attempts already includes the failed attempt
        reason: Error message to store on the item

    Returns:
        bool: True when the item was requeued, False when it was marked ERROR.
    """
    from media.service.errors import (
        CATEGORY_BLOCKED,
        CATEGORY_PERMANENT,
        CATEGORY_SCHEDULED,
        classify_download_error,
        explain_download_error,
        parse_wait_hint,
    )

    category = classify_download_error(reason)
    hint = explain_download_error(category)

    # Nothing we can do by trying again
    if category == CATEGORY_PERMANENT:
        item.status = MediaItem.STATUS_ERROR
        item.error_message = f'{reason} ({hint})'
        item.next_attempt_at = None
        item.save()
        return False

    if category == CATEGORY_SCHEDULED:
        max_attempts = max(1, int(settings.STASHCAST_DOWNLOAD_SCHEDULED_MAX_ATTEMPTS))
    else:
        max_attempts = max(1, int(settings.STASHCAST_DOWNLOAD_MAX_ATTEMPTS))

    if item.download_attempts >= max_attempts:
        message = f'{reason} (gave up after {item.download_attempts} attempt(s))'
        item.status = MediaItem.STATUS_ERROR
        item.error_message = f'{message} {hint}'.strip()
        item.next_attempt_at = None
        item.save()
        return False

    if category == CATEGORY_SCHEDULED:
        # "Premieres in 3 hours" tells us exactly how long to wait
        wait = parse_wait_hint(reason) or timedelta(
            minutes=settings.STASHCAST_DOWNLOAD_RETRY_SCHEDULED_MINUTES
        )
    elif category == CATEGORY_BLOCKED:
        wait = timedelta(minutes=settings.STASHCAST_DOWNLOAD_RETRY_BLOCKED_MINUTES)
    else:
        wait = timedelta(minutes=_backoff_delay_minutes(item.download_attempts))

    minutes = max(1, int(wait.total_seconds() // 60))
    item.status = MediaItem.STATUS_QUEUED
    item.error_message = (
        f'{reason} (attempt {item.download_attempts}/{max_attempts}, '
        f'retrying in {minutes} min) {hint}'
    ).strip()
    item.next_attempt_at = timezone.now() + wait
    item.save()
    return True


_heartbeat_state = {'last': 0.0}


def touch_heartbeat(interval_seconds=10):
    """Record that the worker is alive, at most once every ``interval_seconds``.

    Args:
        interval_seconds: Minimum gap between writes
    """
    now = time.monotonic()
    if now - _heartbeat_state['last'] < interval_seconds:
        return
    _heartbeat_state['last'] = now

    path = heartbeat_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        # Proving liveness must never be the thing that breaks a task
        pass


@signal(signals.SIGNAL_EXECUTING, signals.SIGNAL_COMPLETE, signals.SIGNAL_ERROR)
def _heartbeat_on_activity(signal_name, task, exc=None):
    """Refresh the heartbeat whenever the worker does anything at all.

    The periodic task below cannot be relied on by itself: it queues behind everything
    else, so a worker with a backlog - which is a worker that is very much alive - stops
    refreshing the heartbeat and the application declares itself dead. Signals fire on
    the worker thread as each task runs, so a busy worker keeps proving it is working.
    """
    touch_heartbeat()


def heartbeat_path():
    """Path of the file the worker touches to prove it is alive."""
    return Path(settings.STASHCAST_DATA_DIR) / 'worker-heartbeat'


def worker_is_alive():
    """True when a Huey worker wrote its heartbeat recently enough.

    Replaces the old "item has waited too long, so the worker must be down" guess,
    which could not tell a dead worker from a busy one.
    """
    path = heartbeat_path()
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age <= settings.STASHCAST_WORKER_HEARTBEAT_STALE_SECONDS


def release_download_queue(limit=None, logger=None):
    """Hand a download slot to the oldest queued items that are due.

    This is what keeps a freshly added YouTube channel from flooding the workers:
    items sit in STATUS_QUEUED and only a small batch is enqueued per interval.

    Args:
        limit: How many items to release (default: STASHCAST_DOWNLOAD_QUEUE_BATCH)
        logger: Optional callable(message) for logging

    Returns:
        list[MediaItem]: The items that were enqueued.
    """

    def log(message):
        if logger:
            logger(message)

    if limit is None:
        limit = settings.STASHCAST_DOWNLOAD_QUEUE_BATCH
    limit = max(1, int(limit))

    # An item is due when it has no backoff deadline or the deadline has passed
    now = timezone.now()
    due = MediaItem.objects.filter(status=MediaItem.STATUS_QUEUED).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    )

    # Oldest first, so a backlog drains in the order it arrived
    batch = list(due.order_by('created_at')[:limit])
    if not batch:
        log('Download queue: nothing due')
        return []

    waiting = due.count()
    log(f'Download queue: releasing {len(batch)} of {waiting} due item(s)')

    for item in batch:
        # Claim it before enqueuing so a second run cannot release it twice
        item.status = MediaItem.STATUS_PREFETCHING
        item.save(update_fields=['status', 'updated_at'])
        log(f'  Enqueued: {item.title or item.source_url}')
        process_media(item.guid)

    return batch


def recover_stuck_items(logger=None):
    """Requeue items abandoned by a worker that died mid-task.

    Without this, an item left in PREFETCHING / DOWNLOADING / PROCESSING stays there
    forever and has to be re-synced by hand.

    Args:
        logger: Optional callable(message) for logging

    Returns:
        list[MediaItem]: The items that were requeued or failed.
    """

    def log(message):
        if logger:
            logger(message)

    timeout = max(1, int(settings.STASHCAST_STUCK_TIMEOUT_MINUTES))
    cutoff = timezone.now() - timedelta(minutes=timeout)

    stuck = MediaItem.objects.filter(
        status__in=MediaItem.IN_PROGRESS_STATUSES, updated_at__lt=cutoff
    )

    recovered = []
    for item in stuck:
        reason = f'Abandoned in {item.status} for over {timeout} min (worker likely died)'
        # Count the abandoned attempt here: a worker that died before process_media
        # ran never incremented it, and without this the item would bounce between
        # QUEUED and PREFETCHING forever instead of eventually giving up. An attempt
        # that did start is counted twice, which only costs one retry.
        item.download_attempts += 1
        requeued = schedule_retry_or_fail(item, reason)
        log(f'  {"Requeued" if requeued else "Failed"}: {item.title or item.source_url}')
        recovered.append(item)

    if recovered:
        log(f'Recovered {len(recovered)} stuck item(s)')
    return recovered


def retry_failed_items(logger=None):
    """Give every ERROR item a fresh set of attempts and put it back in the queue.

    Manual escape hatch for the "download failed, now it just sits there" case -
    exposed as ``./manage.py download_queue --retry-errors``.

    Args:
        logger: Optional callable(message) for logging

    Returns:
        list[MediaItem]: The items that were requeued.
    """

    def log(message):
        if logger:
            logger(message)

    failed = MediaItem.objects.filter(status=MediaItem.STATUS_ERROR)
    requeued = []
    for item in failed:
        item.status = MediaItem.STATUS_QUEUED
        item.download_attempts = 0
        item.next_attempt_at = None
        item.error_message = ''
        item.save()
        log(f'  Requeued: {item.title or item.source_url}')
        requeued.append(item)

    log(f'Requeued {len(requeued)} failed item(s)')
    return requeued


@db_task()
def process_media(guid):
    """
    Main processing task for media download and conversion.

    Steps:
    1. PREFETCHING - Extract metadata and validate
    2. DOWNLOADING - Download media files
    3. PROCESSING - Transcode if necessary
    4. READY - Finalize and mark complete

    Downloads occur in tmp-{guid}/ directory in the media folder, then moved to final
    slug-based directory after successful completion. This makes debugging easier and
    keeps download progress visible.
    """
    try:
        item = MediaItem.objects.get(guid=guid)
    except MediaItem.DoesNotExist:
        return

    # Check episode limit before doing any work
    limit_error = check_episode_limit()
    if limit_error:
        item.status = MediaItem.STATUS_ERROR
        item.error_message = limit_error
        item.save()
        return

    # NOTE: there used to be a "stuck in PREFETCHING for >30s means the worker is
    # down" check here. It fired on a healthy but busy queue - stash_url saves the
    # item (updated_at = now) before enqueuing, so anything waiting behind other
    # downloads was failed the moment a worker picked it up. Worker liveness is now
    # reported by the heartbeat file (see worker_is_alive), and abandoned items are
    # requeued by recover_stuck_items.

    # Count this attempt up front so a worker that dies mid-task still leaves an
    # accurate attempt number behind for recover_stuck_items to act on
    item.download_attempts += 1
    item.next_attempt_at = None
    item.save(update_fields=['download_attempts', 'next_attempt_at', 'updated_at'])

    # Create tmp directory in media folder for this download
    # Format: <media_dir>/tmp-{guid}/
    tmp_dir = None
    log_path = None

    try:
        # Determine base media directory (don't know slug yet)
        media_base = Path(settings.STASHCAST_MEDIA_DIR)
        media_base.mkdir(parents=True, exist_ok=True)

        # Create tmp directory with GUID
        tmp_dir = media_base / f'tmp-{guid}'
        tmp_dir.mkdir(exist_ok=True)

        # Create log file immediately in tmp directory
        log_path = tmp_dir / 'download.log'
        write_log(log_path, '=== TASK STARTED ===')
        write_log(log_path, f'GUID: {guid}')
        write_log(log_path, f'URL: {item.source_url}')
        write_log(log_path, f'Requested type: {item.requested_type}')
        write_log(log_path, f'Tmp directory: {tmp_dir}')

        # PREFETCHING
        item.status = MediaItem.STATUS_PREFETCHING
        item.save()
        write_log(log_path, '=== PREFETCHING ===')
        update_progress(item.guid, MediaItem.STATUS_PREFETCHING, 0)

        # Determine download strategy
        strategy = choose_download_strategy(item.source_url)
        is_direct = strategy in ('direct', 'file')

        if is_direct:
            # Direct download - minimal metadata
            if Path(item.source_url).exists():
                prefetch_file(item, tmp_dir, log_path)
            else:
                prefetch_direct(item, tmp_dir, log_path)
        else:
            # Use yt-dlp to extract metadata (may fallback to HTML extractor)
            prefetch_ytdlp(item, tmp_dir, log_path)

        # Re-check if URL is now direct (HTML extractor may have found direct media)
        item.refresh_from_db()
        strategy = choose_download_strategy(item.source_url)
        is_direct = strategy in ('direct', 'file')

        write_log(log_path, f'Direct media URL: {is_direct}')
        update_progress(item.guid, MediaItem.STATUS_PREFETCHING, 10)

        # DOWNLOADING
        item.status = MediaItem.STATUS_DOWNLOADING
        item.save()
        write_log(log_path, '=== DOWNLOADING ===')

        if is_direct:
            download_direct(item, tmp_dir, log_path)
        else:
            download_ytdlp(item, tmp_dir, log_path)

        # Repack the file if the download landed in a container/codec podcast clients
        # cannot play (see STASHCAST_ENSURE_PLAYABLE_VIDEO)
        ensure_playable_video(item, tmp_dir, log_path)

        # PROCESSING
        item.status = MediaItem.STATUS_PROCESSING
        item.save()
        write_log(log_path, '=== PROCESSING ===')
        update_progress(item.guid, MediaItem.STATUS_PROCESSING, 40)

        process_files(item, tmp_dir, log_path)

        # Move from tmp directory to final slug-based directory
        write_log(log_path, '=== MOVING TO FINAL DIRECTORY ===')
        final_dir = item.get_base_dir()
        final_dir.parent.mkdir(parents=True, exist_ok=True)

        # If final directory exists, remove it (overwrite behavior)
        if final_dir.exists():
            write_log(log_path, f'Removing existing directory: {final_dir}')
            shutil.rmtree(final_dir)

        # Move tmp directory to final location
        shutil.move(str(tmp_dir), str(final_dir))

        # IMPORTANT: Update log_path BEFORE writing more logs to prevent creating
        # files in the old tmp directory, which would leave it behind after the move
        log_path = final_dir / 'download.log'
        write_log(log_path, f'Moved to: {final_dir}')

        # Measure what we actually ended up with, now that the file is in its final
        # place. Doing it here keeps the stored measurement in step with the file: a
        # re-download used to leave the previous file's duration behind, so an item
        # repaired by re-fetching still showed up as incomplete forever.
        record_download_duration(item, log_path)

        # READY
        item.status = MediaItem.STATUS_READY
        item.downloaded_at = timezone.now()
        item.save()
        write_log(log_path, '=== READY ===')
        write_log(log_path, f'Completed successfully: {item.title}')
        update_progress(item.guid, MediaItem.STATUS_READY, 100)

        # Clean up progress tracker
        from media.progress_tracker import clear_progress

        clear_progress(item.guid)

        # Transcribe when the item's group is set up for it
        queue_transcription_if_enabled(item, log_path)

        # Generate summary if subtitles are available
        if item.subtitle_path and settings.STASHCAST_SUMMARY_SENTENCES > 0:
            write_log(log_path, 'Enqueuing summary generation task')
            generate_summary(item.guid)

    except Exception as e:
        # Retry via the paced queue, or give up once the attempts are exhausted
        requeued = schedule_retry_or_fail(item, str(e))
        if log_path:
            write_log(log_path, '=== ERROR ===')
            write_log(log_path, f'Error: {str(e)}')
            if requeued:
                write_log(
                    log_path,
                    f'Requeued for retry {item.download_attempts + 1}/'
                    f'{settings.STASHCAST_DOWNLOAD_MAX_ATTEMPTS} '
                    f'at {item.next_attempt_at:%Y-%m-%d %H:%M:%S}',
                )
            else:
                write_log(log_path, f'Giving up after {item.download_attempts} attempt(s)')

        # Clean up tmp directory on error
        if tmp_dir and tmp_dir.exists():
            write_log(log_path, f'Cleaning up tmp directory: {tmp_dir}')
            try:
                shutil.rmtree(tmp_dir)
            except Exception as cleanup_error:
                write_log(log_path, f'Failed to clean up tmp: {cleanup_error}')

        raise


@db_task()
def generate_summary(guid):
    """
    Generate summary from subtitle file using extractive summarization.
    Future: Could be extended to use transcription from audio.
    """
    # Skip summary generation if STASHCAST_SUMMARY_SENTENCES is set to 0
    num_sentences = settings.STASHCAST_SUMMARY_SENTENCES
    if num_sentences <= 0:
        return

    try:
        item = MediaItem.objects.get(guid=guid)
    except MediaItem.DoesNotExist:
        return

    log_path = item.get_absolute_log_path() if item.log_path else None

    subtitle_path = item.get_absolute_subtitle_path()
    if not subtitle_path or not os.path.exists(subtitle_path):
        if log_path:
            write_log(log_path, 'No subtitles available for summary generation')
        return

    try:
        if log_path:
            write_log(log_path, '=== GENERATING SUMMARY ===')
            write_log(log_path, f'Reading subtitles from: {subtitle_path}')
        # Read subtitle file and extract text
        with open(subtitle_path, 'r', encoding='utf-8') as f:
            subtitle_text = f.read()

        # Remove VTT formatting
        import re

        lines = subtitle_text.split('\n')
        text_lines = []
        for line in lines:
            # Skip VTT headers, timestamps, cue IDs, and blank lines
            if (
                not line.startswith('WEBVTT')
                and not line.startswith('Kind:')
                and not line.startswith('Language:')
                and '-->' not in line
                and not re.match(r'^\d+$', line.strip())
                and 'align:' not in line
                and 'position:' not in line
                and line.strip()
            ):
                # Remove timing tags like <00:00:00.400> and <c>
                clean_line = re.sub(r'<[^>]+>', '', line)
                if clean_line.strip():
                    text_lines.append(clean_line.strip())

        full_text = ' '.join(text_lines)

        if not full_text:
            return

        # Use sumy for extractive summarization
        from sumy.nlp.tokenizers import Tokenizer
        from sumy.parsers.plaintext import PlaintextParser
        from sumy.summarizers.lex_rank import LexRankSummarizer

        parser = PlaintextParser.from_string(full_text, Tokenizer('english'))
        summarizer = LexRankSummarizer()

        # Generate summary with configured number of sentences
        num_sentences = settings.STASHCAST_SUMMARY_SENTENCES
        summary_sentences = summarizer(parser.document, num_sentences)
        summary = ' '.join(str(sentence) for sentence in summary_sentences)

        item.summary = summary
        item.save()

        if log_path:
            write_log(log_path, f'Generated {len(list(summary_sentences))} sentence summary')

    except Exception as e:
        # Don't fail the whole item if summary generation fails
        log_path = item.get_absolute_log_path()
        if log_path:
            write_log(log_path, f'Summary generation failed: {str(e)}')


@db_task()
def process_media_batch(guids: List[str]):
    """
    Batch processing task for multiple media downloads.

    Uses exactly TWO yt-dlp calls:
    1. Single prefetch pass - extracts metadata for all URLs, expands playlists
    2. Single download pass - downloads all individual videos

    MediaItems are created upfront after prefetch so they can be tracked.

    Args:
        guids: List of MediaItem GUIDs to process
    """
    if not guids:
        return

    # Get initial items
    initial_items = {guid: MediaItem.objects.filter(guid=guid).first() for guid in guids}
    initial_items = {k: v for k, v in initial_items.items() if v is not None}

    if not initial_items:
        return

    # Check episode limit before doing any work
    limit_error = check_episode_limit()
    if limit_error:
        for guid, item in initial_items.items():
            item.status = MediaItem.STATUS_ERROR
            item.error_message = limit_error
            item.save()
        return

    # Create batch tmp directory
    media_base = Path(settings.STASHCAST_MEDIA_DIR)
    media_base.mkdir(parents=True, exist_ok=True)
    batch_id = guids[0][:8]
    batch_tmp_dir = media_base / f'batch-{batch_id}'
    batch_tmp_dir.mkdir(exist_ok=True)

    batch_log_path = batch_tmp_dir / 'batch.log'
    write_log(batch_log_path, '=== BATCH TASK STARTED ===')
    write_log(batch_log_path, f'Processing {len(initial_items)} input URLs')

    # Separate URLs by strategy
    ytdlp_urls = []
    direct_items = {}  # guid -> item for direct downloads

    for guid, item in initial_items.items():
        item.status = MediaItem.STATUS_PREFETCHING
        item.save()

        strategy = choose_download_strategy(item.source_url)
        if strategy in ('direct', 'file'):
            direct_items[guid] = item
        else:
            ytdlp_urls.append(item.source_url)

    write_log(batch_log_path, f'{len(ytdlp_urls)} yt-dlp URLs, {len(direct_items)} direct URLs')

    # Track all items that will be processed (includes expanded playlist entries)
    all_items = {}  # guid -> item
    item_tmp_dirs = {}  # guid -> tmp_dir
    item_log_paths = {}  # guid -> log_path
    guid_by_video_url = {}  # video_url -> guid

    try:
        # === PHASE 1: SINGLE PREFETCH CALL ===
        write_log(batch_log_path, '=== PREFETCH PHASE (single yt-dlp call) ===')

        if ytdlp_urls:
            # Get the requested_type from first item
            first_item = next(iter(initial_items.values()))
            requested_type = first_item.requested_type

            prefetch_result = prefetch_ytdlp_batch(
                urls=ytdlp_urls,
                logger=lambda m: write_log(batch_log_path, m),
            )

            # Handle prefetch errors - mark original items as failed
            for url, error in prefetch_result.errors.items():
                # Find the original item for this URL
                for guid, item in initial_items.items():
                    if item.source_url == url:
                        item.status = MediaItem.STATUS_ERROR
                        item.error_message = f'Prefetch failed: {error}'
                        item.save()
                        write_log(batch_log_path, f'FAILED: {url} - {error}')
                        break

            # Create/update MediaItems for all videos (playlists expanded)
            write_log(
                batch_log_path, f'Creating MediaItems for {len(prefetch_result.videos)} videos'
            )

            for video_info in prefetch_result.videos:
                # Check if this is from a playlist or original URL
                is_from_playlist = video_info.playlist_title is not None

                # Find or create MediaItem for this video
                existing = MediaItem.objects.filter(
                    source_url=video_info.url, requested_type=requested_type
                ).first()

                if existing:
                    item = existing
                    item.status = MediaItem.STATUS_DOWNLOADING
                    item.error_message = ''
                else:
                    item = MediaItem.objects.create(
                        source_url=video_info.url,
                        requested_type=requested_type,
                        slug='pending',
                        status=MediaItem.STATUS_DOWNLOADING,
                    )

                # Apply metadata from prefetch
                item.title = video_info.title or 'Untitled'
                item.description = video_info.description or ''
                item.author = video_info.author or ''
                item.duration_seconds = video_info.duration_seconds
                item.extractor = video_info.extractor or ''
                item.external_id = video_info.external_id or ''
                item.webpage_url = video_info.webpage_url
                # Publication date from the source platform, not the download date
                if video_info.publish_date:
                    item.publish_date = video_info.publish_date

                # Determine media type
                if requested_type == 'auto':
                    item.media_type = 'video' if video_info.has_video else 'audio'
                else:
                    item.media_type = requested_type

                # Generate slug
                slug = generate_slug(item.title)
                item.slug = ensure_unique_slug(slug, item.source_url, None, item.media_type)
                item.log_path = 'download.log'
                item.save()

                # Create tmp directory for this item
                tmp_dir = batch_tmp_dir / f'item-{item.guid}'
                tmp_dir.mkdir(exist_ok=True)
                log_path = tmp_dir / 'download.log'

                write_log(log_path, '=== TASK STARTED (BATCH MODE) ===')
                write_log(log_path, f'GUID: {item.guid}')
                write_log(log_path, f'URL: {item.source_url}')
                write_log(log_path, f'Title: {item.title}')
                if is_from_playlist:
                    write_log(log_path, f'From playlist: {video_info.playlist_title}')

                all_items[item.guid] = item
                item_tmp_dirs[item.guid] = tmp_dir
                item_log_paths[item.guid] = log_path
                guid_by_video_url[video_info.url] = item.guid

                write_log(batch_log_path, f'  Created: {item.title}')

            # Mark original playlist URLs as "expanded" (not real items)
            for guid, item in initial_items.items():
                if item.source_url in ytdlp_urls and item.guid not in all_items:
                    # This was a playlist URL - mark it as expanded
                    # Find the playlist title from any video that came from it
                    playlist_title = None
                    for video_info in prefetch_result.videos:
                        if video_info.source_url == item.source_url and video_info.playlist_title:
                            playlist_title = video_info.playlist_title
                            break

                    if playlist_title:
                        item.status = MediaItem.STATUS_READY
                        item.title = f'[Playlist] {playlist_title}'
                        count = sum(
                            1 for v in prefetch_result.videos if v.source_url == item.source_url
                        )
                        item.error_message = f'Expanded to {count} individual items'
                        item.save()
                        write_log(
                            batch_log_path, f'  Playlist expanded: {playlist_title} ({count} items)'
                        )

        # === PHASE 2: HANDLE DIRECT DOWNLOADS ===
        for guid, item in direct_items.items():
            tmp_dir = batch_tmp_dir / f'item-{guid}'
            tmp_dir.mkdir(exist_ok=True)
            log_path = tmp_dir / 'download.log'

            write_log(log_path, '=== TASK STARTED (BATCH MODE - DIRECT) ===')
            write_log(log_path, f'GUID: {guid}')
            write_log(log_path, f'URL: {item.source_url}')

            item_tmp_dirs[guid] = tmp_dir
            item_log_paths[guid] = log_path

            try:
                if Path(item.source_url).exists():
                    prefetch_file(item, tmp_dir, log_path)
                else:
                    prefetch_direct(item, tmp_dir, log_path)

                item.status = MediaItem.STATUS_DOWNLOADING
                item.save()
                download_direct(item, tmp_dir, log_path)
                all_items[guid] = item
                write_log(batch_log_path, f'Direct downloaded: {item.source_url}')

            except Exception as e:
                item.status = MediaItem.STATUS_ERROR
                item.error_message = f'Download failed: {str(e)}'
                item.save()
                write_log(log_path, f'Error: {e}')
                write_log(batch_log_path, f'FAILED direct download: {item.source_url} - {e}')

        # === PHASE 3: SINGLE DOWNLOAD CALL FOR YT-DLP URLS ===
        video_urls = list(guid_by_video_url.keys())
        if video_urls:
            write_log(
                batch_log_path,
                f'=== DOWNLOAD PHASE (single yt-dlp call, {len(video_urls)} videos) ===',
            )

            # Determine resolved type
            first_video_guid = guid_by_video_url[video_urls[0]]
            resolved_type = all_items[first_video_guid].media_type or 'video'

            batch_result = download_ytdlp_batch(
                urls=video_urls,
                resolved_type=resolved_type,
                temp_dir=batch_tmp_dir / 'downloads',
                ytdlp_extra_args=get_ytdlp_args_for_type(resolved_type),
                logger=lambda m: write_log(batch_log_path, m),
            )

            # Move downloaded files to item directories
            for url, download_info in batch_result.downloads.items():
                guid = guid_by_video_url[url]
                item = all_items[guid]
                tmp_dir = item_tmp_dirs[guid]
                log_path = item_log_paths[guid]

                write_log(log_path, f'Downloaded: {download_info.path.name}')

                # Normalize filenames and record content_path / file_size, exactly like
                # the single-item path does. Moving the raw files across without this
                # left every batch item READY but with no content_path, so it had no
                # file size, no MIME type and nothing to play.
                apply_download_info(item, tmp_dir, download_info, log_path)

                # Anything else yt-dlp produced for this video (extra thumbnails,
                # subtitle variants) follows into the item directory
                leftovers = download_info.path.parent
                if leftovers.exists():
                    for src_file in leftovers.iterdir():
                        shutil.move(str(src_file), str(tmp_dir / src_file.name))

                ensure_playable_video(item, tmp_dir, log_path)

            # Handle download errors
            for url, error in batch_result.errors.items():
                guid = guid_by_video_url[url]
                item = all_items[guid]
                log_path = item_log_paths[guid]

                item.status = MediaItem.STATUS_ERROR
                item.error_message = f'Download failed: {error}'
                item.save()
                write_log(log_path, f'Download error: {error}')
                write_log(batch_log_path, f'FAILED download: {url} - {error}')

        # === PHASE 4: PROCESS EACH ITEM ===
        write_log(batch_log_path, '=== PROCESSING PHASE ===')

        for guid, item in all_items.items():
            if item.status == MediaItem.STATUS_ERROR:
                continue

            tmp_dir = item_tmp_dirs.get(guid)
            log_path = item_log_paths.get(guid)

            if not tmp_dir or not tmp_dir.exists():
                continue

            try:
                item.status = MediaItem.STATUS_PROCESSING
                item.save()
                write_log(log_path, '=== PROCESSING ===')
                update_progress(guid, MediaItem.STATUS_PROCESSING, 60)

                process_files(item, tmp_dir, log_path)

                # Move to final directory
                write_log(log_path, '=== MOVING TO FINAL DIRECTORY ===')
                final_dir = item.get_base_dir()
                final_dir.parent.mkdir(parents=True, exist_ok=True)

                if final_dir.exists():
                    shutil.rmtree(final_dir)

                shutil.move(str(tmp_dir), str(final_dir))
                log_path = final_dir / 'download.log'
                write_log(log_path, f'Moved to: {final_dir}')

                record_download_duration(item, log_path)

                item.status = MediaItem.STATUS_READY
                item.downloaded_at = timezone.now()
                item.save()
                write_log(log_path, '=== READY ===')
                update_progress(guid, MediaItem.STATUS_READY, 100)

                from media.progress_tracker import clear_progress

                clear_progress(guid)

                queue_transcription_if_enabled(item, log_path)

                if item.subtitle_path and settings.STASHCAST_SUMMARY_SENTENCES > 0:
                    generate_summary(item.guid)

                write_log(batch_log_path, f'Completed: {item.title}')

            except Exception as e:
                item.status = MediaItem.STATUS_ERROR
                item.error_message = f'Processing failed: {str(e)}'
                item.save()
                write_log(log_path, f'Error during processing: {e}')
                write_log(batch_log_path, f'FAILED processing: {item.title} - {e}')

        write_log(batch_log_path, '=== BATCH COMPLETE ===')

    finally:
        try:
            if batch_tmp_dir.exists():
                shutil.rmtree(batch_tmp_dir)
        except Exception as e:
            write_log(batch_log_path, f'Failed to clean up: {e}')


def queue_items_for_download(guids):
    """Hand a list of existing items to the paced download queue.

    Used for bulk adds: instead of one big batch task, each URL becomes a normal queued
    item processed by process_media - the same well-tested path a single download takes,
    released a few at a time so the workers are never flooded.

    Args:
        guids: Iterable of MediaItem GUIDs

    Returns:
        int: How many items were queued.
    """
    return MediaItem.objects.filter(guid__in=list(guids)).update(
        status=MediaItem.STATUS_QUEUED,
        download_attempts=0,
        next_attempt_at=None,
        error_message='',
    )


def transcribe_item(item, logger=None):
    """Produce a transcript for one item with the configured Wyoming server.

    The text is stored on the record and a WebVTT file is written next to the media so
    podcast clients that support transcripts can fetch it from the feed.

    Args:
        item: MediaItem to transcribe
        logger: Optional callable(str) for progress logging

    Returns:
        int: Number of transcribed segments (0 when the audio held no speech).

    Raises:
        TranscriptionError: When the server or the audio cannot be used.
    """
    from media.service.transcribe import (
        TranscriptionError,
        segments_to_text,
        segments_to_vtt,
        transcribe_file,
    )

    def log(message):
        if logger:
            logger(message)

    if not settings.STASHCAST_WHISPER_ENABLED:
        raise TranscriptionError(
            'Speech to text is switched off - set STASHCAST_WHISPER_ENABLED=true'
        )

    source = item.get_absolute_content_path()
    if not source or not Path(source).exists():
        raise TranscriptionError('No media file on disk to transcribe')

    log(
        f'Transcribing {item.title or item.source_url} '
        f'(language: {settings.STASHCAST_WHISPER_LANGUAGE or "auto-detect"})'
    )
    item.transcript_status = MediaItem.TRANSCRIPT_RUNNING
    item.transcript_error = ''
    item.save(update_fields=['transcript_status', 'transcript_error', 'updated_at'])

    segments = transcribe_file(
        source,
        uri=settings.STASHCAST_WHISPER_URI,
        language=settings.STASHCAST_WHISPER_LANGUAGE or None,
        total_seconds=item.duration_seconds,
        window_seconds=settings.STASHCAST_WHISPER_WINDOW_SECONDS,
        timeout=settings.STASHCAST_WHISPER_TIMEOUT_SECONDS,
        logger=logger,
    )

    text = segments_to_text(segments)
    transcript_file = Path(source).parent / 'transcript.vtt'
    transcript_file.write_text(segments_to_vtt(segments), encoding='utf-8')

    item.transcript = text
    item.transcript_path = transcript_file.name
    item.transcript_created_at = timezone.now()
    item.transcript_status = MediaItem.TRANSCRIPT_DONE
    item.transcript_error = ''
    item.save(
        update_fields=[
            'transcript',
            'transcript_path',
            'transcript_created_at',
            'transcript_status',
            'transcript_error',
            'updated_at',
        ]
    )

    log(f'  {len(segments)} segment(s), {len(text)} characters')
    return len(segments)


def queue_transcription_if_enabled(item, log_path=None):
    """Start a transcript for a freshly downloaded item when its group asks for it.

    Opt-in per group and off by default: transcription costs real time on the Whisper
    machine, and most groups do not need it.

    Never raises. This runs inside the download task, whose error handling marks the
    item as failed and schedules a re-download - so letting a transcription problem
    escape would throw away a file that downloaded perfectly. A transcript is a bonus;
    it must never cost you the episode.

    Args:
        item: MediaItem that has just finished downloading
        log_path: Optional path to the item's download log

    Returns:
        bool: True when transcription was queued.
    """
    if not settings.STASHCAST_WHISPER_ENABLED:
        return False

    group = item.group
    if not group or not group.transcribe_new_downloads:
        return False

    try:
        queue_transcription([item])
    except Exception as e:
        if log_path:
            write_log(log_path, f'Could not queue transcription (download is fine): {e}')
        return False

    if log_path:
        write_log(log_path, f'Queued for transcription (group "{group.name}")')
    return True


def cancel_transcription(items):
    """Take items out of the transcription queue.

    Only items still waiting can be cancelled: one already being transcribed is running
    inside a worker thread and cannot be interrupted, so it is left to finish.

    Args:
        items: Iterable of MediaItem

    Returns:
        tuple[int, int]: (cancelled, left alone because they are already running)
    """
    cancelled = 0
    running = 0
    for item in items:
        if item.transcript_status == MediaItem.TRANSCRIPT_RUNNING:
            running += 1
            continue
        if item.transcript_status != MediaItem.TRANSCRIPT_QUEUED:
            continue
        # Back to "never attempted" - the pending task checks this and does nothing
        item.transcript_status = ''
        item.transcript_error = ''
        item.save(update_fields=['transcript_status', 'transcript_error', 'updated_at'])
        cancelled += 1
    return cancelled, running


def cancel_download(items):
    """Take items out of the paced download queue.

    The record is kept rather than deleted, so nothing is lost: the item lands in ERROR
    saying why, and "Requeue selected items" puts it back whenever you want it.

    Args:
        items: Iterable of MediaItem

    Returns:
        int: How many were taken out.
    """
    cancelled = 0
    for item in items:
        if item.status != MediaItem.STATUS_QUEUED:
            continue
        item.status = MediaItem.STATUS_ERROR
        item.error_message = 'Removed from the download queue'
        item.next_attempt_at = None
        item.save(update_fields=['status', 'error_message', 'next_attempt_at', 'updated_at'])
        cancelled += 1
    return cancelled


def queue_transcription(items):
    """Mark items as waiting for transcription and hand them to the worker.

    Args:
        items: Iterable of MediaItem

    Returns:
        int: How many were queued.
    """
    count = 0
    for item in items:
        item.transcript_status = MediaItem.TRANSCRIPT_QUEUED
        item.transcript_error = ''
        item.save(update_fields=['transcript_status', 'transcript_error', 'updated_at'])
        transcribe_media(item.guid)
        count += 1
    return count


@db_task()
def transcribe_media(guid):
    """Background task: transcribe one item.

    A pending task cannot be pulled back out of the worker queue, so it checks whether
    the item is still waiting before doing any work. That is what makes "remove from the
    transcription queue" in the admin actually take effect rather than just changing a
    label.
    """
    try:
        item = MediaItem.objects.get(guid=guid)
    except MediaItem.DoesNotExist:
        return

    if item.transcript_status != MediaItem.TRANSCRIPT_QUEUED:
        # Taken out of the queue (or already handled) while this task waited its turn
        return

    try:
        transcribe_item(item)
    except Exception as e:
        # Deliberately not error_message: that field describes the download, and a
        # perfectly downloaded episode whose transcription failed must not look like a
        # broken download.
        item.transcript_status = MediaItem.TRANSCRIPT_FAILED
        item.transcript_error = str(e)
        item.save(update_fields=['transcript_status', 'transcript_error', 'updated_at'])


def record_download_duration(item, log_path=None):
    """Measure a freshly downloaded file and note how it compares with the source.

    Called at the end of every download so the stored measurement always describes the
    file that is actually on disk. Without this the value only ever came from
    ./manage.py check_durations, so re-downloading a truncated episode fixed the file
    but left it flagged as incomplete.

    Never raises: a file we cannot probe must not fail an otherwise good download.

    Args:
        item: MediaItem whose file has just been moved into place
        log_path: Optional path to the item's download log
    """
    try:
        gap = check_item_duration(item)
    except Exception as e:
        if log_path:
            write_log(log_path, f'Could not measure duration: {e}')
        return

    if gap is None:
        return

    tolerance = settings.STASHCAST_DURATION_TOLERANCE_SECONDS
    if log_path and gap > tolerance:
        write_log(
            log_path,
            f'WARNING: file plays {item.file_duration_seconds}s but the source reports '
            f'{item.duration_seconds}s (short by {gap}s) - the download is incomplete',
        )
    elif log_path:
        write_log(log_path, f'Duration matches the source ({item.file_duration_seconds}s)')


def measure_file_duration(item):
    """Measure how long an item's downloaded file actually plays for.

    Args:
        item: MediaItem to measure

    Returns:
        int or None: Duration in seconds, or None when there is no readable file.
    """
    from media.service.media_info import extract_ffprobe_metadata

    path = item.get_absolute_content_path()
    if not path or not Path(path).exists():
        return None

    metadata = extract_ffprobe_metadata(path)
    return (metadata or {}).get('duration_seconds')


def check_item_duration(item, logger=None):
    """Store the real duration of an item's file and report how far off it is.

    ``duration_seconds`` comes from the source metadata and is the truth; the measured
    duration comes from the file on disk. A large gap means the download was cut short
    and the item should be fetched again.

    Args:
        item: MediaItem to check
        logger: Optional callable(str) for logging

    Returns:
        int or None: The gap in seconds, or None when the file could not be measured.
    """

    def log(message):
        if logger:
            logger(message)

    measured = measure_file_duration(item)
    if measured is None:
        log(f'  No readable file: {item.title or item.source_url}')
        return None

    item.file_duration_seconds = measured
    item.duration_checked_at = timezone.now()
    item.save(update_fields=['file_duration_seconds', 'duration_checked_at', 'updated_at'])

    gap = item.duration_gap_seconds
    if gap is None:
        log(f'  No source duration to compare against: {item.title or item.source_url}')
    return gap


def check_durations(limit=None, only_unchecked=False, tolerance=None, logger=None):
    """Measure file durations for downloaded items and collect the bad ones.

    Args:
        limit: Maximum number of items to check (None = all)
        only_unchecked: Skip items that already have a measurement
        tolerance: Allowed gap in seconds (default STASHCAST_DURATION_TOLERANCE_SECONDS)
        logger: Optional callable(str) for logging

    Returns:
        tuple[int, list[MediaItem], int]: (checked, mismatched items, skipped)
    """

    def log(message):
        if logger:
            logger(message)

    if tolerance is None:
        tolerance = settings.STASHCAST_DURATION_TOLERANCE_SECONDS

    items = MediaItem.objects.filter(status=MediaItem.STATUS_READY).exclude(content_path='')
    if only_unchecked:
        items = items.filter(file_duration_seconds__isnull=True)
    items = items.order_by('-downloaded_at')
    if limit:
        items = items[: int(limit)]

    checked = 0
    skipped = 0
    mismatched = []
    for item in items:
        gap = check_item_duration(item, logger=logger)
        if gap is None:
            skipped += 1
            continue
        checked += 1
        if gap > tolerance:
            mismatched.append(item)
            log(
                f'  SHORT by {gap}s (expected {item.duration_seconds}s, '
                f'file {item.file_duration_seconds}s): {item.title or item.source_url}'
            )

    log(f'Durations: {checked} checked, {len(mismatched)} off by more than {tolerance}s, '
        f'{skipped} skipped')
    return checked, mismatched, skipped


@db_task()
def check_duration_for_item(guid):
    """Background task: measure one item's file duration."""
    try:
        item = MediaItem.objects.get(guid=guid)
    except MediaItem.DoesNotExist:
        return

    try:
        check_item_duration(item)
    except Exception:
        # A single unreadable file must not take down a batch
        pass


def repair_video_for_item(item, logger=None):
    """Rewrite an item's video file into an MP4 that Apple Podcasts / iOS can play.

    Fixes episodes downloaded while the format selector allowed Opus audio, which made
    yt-dlp fall back to a Matroska container that those clients refuse to play. The
    video stream is copied whenever possible, so this is far cheaper than a re-download.

    Args:
        item: MediaItem to repair
        logger: Optional callable(str) for logging

    Returns:
        bool: True when the file was rewritten, False when nothing needed doing.
    """
    from media.service.media_info import get_mime_type, is_ios_compatible_video
    from media.service.process import remux_to_compatible_mp4

    def log(message):
        if logger:
            logger(message)

    if item.media_type != MediaItem.MEDIA_TYPE_VIDEO:
        return False

    source = item.get_absolute_content_path()
    if not source or not Path(source).exists():
        log(f'  No file on disk: {item.title or item.source_url}')
        return False

    source = Path(source)
    if is_ios_compatible_video(source):
        return False

    target = source.with_name('content-repaired.mp4')
    log(f'Repairing: {item.title or item.source_url}')
    remux_to_compatible_mp4(source, target, logger=logger)

    # Swap the new file in only after ffmpeg succeeded
    final = source.with_suffix('.mp4')
    if source != final:
        source.unlink()
    target.replace(final)

    item.content_path = final.name
    item.mime_type = get_mime_type(final)
    item.file_size = final.stat().st_size
    item.save(update_fields=['content_path', 'mime_type', 'file_size', 'updated_at'])
    log(f'  Now {item.content_path} ({item.file_size} bytes)')
    return True


@db_task()
def repair_video_file(guid):
    """Background task: make one item's video playable on Apple Podcasts / iOS."""
    try:
        item = MediaItem.objects.get(guid=guid)
    except MediaItem.DoesNotExist:
        return

    try:
        repair_video_for_item(item)
    except Exception:
        # One unrepairable file must not take down a batch
        pass


def repair_incompatible_videos(limit=None, logger=None):
    """Repair every ready video that Apple Podcasts / iOS cannot play.

    Args:
        limit: Maximum number of items to process (None = all)
        logger: Optional callable(str) for logging

    Returns:
        tuple[int, int]: (repaired, skipped)
    """

    def log(message):
        if logger:
            logger(message)

    items = MediaItem.objects.filter(
        media_type=MediaItem.MEDIA_TYPE_VIDEO, status=MediaItem.STATUS_READY
    ).order_by('-downloaded_at')
    if limit:
        items = items[: int(limit)]

    repaired = 0
    skipped = 0
    for item in items:
        try:
            if repair_video_for_item(item, logger=logger):
                repaired += 1
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            log(f'  Failed: {item.title or item.source_url} - {e}')

    log(f'Videos: {repaired} repaired, {skipped} left alone')
    return repaired, skipped


def fetch_publish_date_for_item(item, logger=None):
    """Re-query the source platform for an item's publication date and store it.

    Used to fill in items downloaded before the date was being captured. Only touches
    publish_date - nothing else about the item is changed and nothing is re-downloaded.

    Args:
        item: MediaItem to update
        logger: Optional callable(str) for logging

    Returns:
        datetime or None: The stored date, or None when the source offered none.
    """
    from media.service.resolve import prefetch

    def log(message):
        if logger:
            logger(message)

    result = prefetch(item.source_url, 'ytdlp', logger=None)
    if not result.publish_date:
        log(f'  No date available: {item.title or item.source_url}')
        return None

    item.publish_date = result.publish_date
    item.save(update_fields=['publish_date', 'updated_at'])
    log(f'  {result.publish_date:%Y-%m-%d}  {item.title or item.source_url}')
    return item.publish_date


@db_task()
def refresh_publish_date(guid):
    """Background task: fill in one item's publication date from the source platform."""
    try:
        item = MediaItem.objects.get(guid=guid)
    except MediaItem.DoesNotExist:
        return

    try:
        fetch_publish_date_for_item(item)
    except Exception:
        # A missing/private/deleted video should not fail the whole batch
        pass


def backfill_publish_dates(limit=None, only_missing=True, logger=None):
    """Fill in publication dates for items that do not have one yet.

    Args:
        limit: Maximum number of items to process (None = all)
        only_missing: When False, refresh every item instead of just the undated ones
        logger: Optional callable(str) for logging

    Returns:
        tuple[int, int]: (updated, skipped)
    """

    def log(message):
        if logger:
            logger(message)

    items = MediaItem.objects.exclude(status=MediaItem.STATUS_QUEUED)
    if only_missing:
        items = items.filter(publish_date__isnull=True)
    items = items.order_by('-downloaded_at')
    if limit:
        items = items[: int(limit)]

    updated = 0
    skipped = 0
    for item in items:
        try:
            if fetch_publish_date_for_item(item, logger=logger):
                updated += 1
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            log(f'  Failed: {item.title or item.source_url} - {e}')

    log(f'Publication dates: {updated} updated, {skipped} skipped')
    return updated, skipped


@db_task()
def sync_channel_full(group_id):
    """
    Background task: walk a group's entire YouTube channel and queue every upload.

    Listing a large back-catalogue takes a while, so this runs off the request cycle.
    Everything it finds lands in STATUS_QUEUED and is released by the paced queue, so
    a 500-video channel downloads slowly instead of all at once.
    """
    from media.models import MediaGroup
    from media.operations import sync_group_channel

    try:
        group = MediaGroup.objects.get(pk=group_id)
    except MediaGroup.DoesNotExist:
        return

    if not group.youtube_channel_url:
        return

    # max_videos=0 means "no cap" - the whole channel
    sync_group_channel(group, max_videos=0)


def _download_queue_crontab():
    """Build the crontab schedule for the paced download queue.

    Runs every N minutes (STASHCAST_DOWNLOAD_QUEUE_MINUTES). N is clamped to 1..59 so
    the ``*/N`` minute expression stays valid.
    """
    minutes = getattr(settings, 'STASHCAST_DOWNLOAD_QUEUE_MINUTES', 5)
    minutes = max(1, min(59, int(minutes)))
    return crontab(minute=f'*/{minutes}')


@db_periodic_task(crontab(minute='*'))
def worker_heartbeat():
    """Refresh the heartbeat while the worker is idle.

    A busy worker is covered by the signal handler above; this is for the quiet periods
    when no tasks are running at all.
    """
    touch_heartbeat(interval_seconds=0)


@db_periodic_task(_download_queue_crontab())
def process_download_queue():
    """
    Periodic task: release a small batch of queued downloads.

    Items stashed by the YouTube channel sync wait in STATUS_QUEUED; this releases
    STASHCAST_DOWNLOAD_QUEUE_BATCH of them every STASHCAST_DOWNLOAD_QUEUE_MINUTES
    (default: 1 item every 5 minutes) so the workers are never flooded.

    Also recovers items abandoned by a worker that died mid-task, so a failed
    download no longer has to be re-synced by hand.
    """
    recover_stuck_items()
    release_download_queue()


def _youtube_sync_crontab():
    """Build the crontab schedule for the channel-sync task from settings.

    Runs at minute 0, every N hours (STASHCAST_YOUTUBE_SYNC_HOURS). N is clamped
    to 1..23 so the ``*/N`` hour expression stays valid.
    """
    hours = getattr(settings, 'STASHCAST_YOUTUBE_SYNC_HOURS', 3)
    hours = max(1, min(23, int(hours)))
    return crontab(minute='0', hour=f'*/{hours}')


@db_periodic_task(_youtube_sync_crontab())
def sync_youtube_channels():
    """
    Periodic task: check every group's YouTube channel for new uploads.

    New uploads are downloaded as audio and assigned to the group they came from,
    reusing the standard processing pipeline (each new item enqueues process_media).
    Scheduled via STASHCAST_YOUTUBE_SYNC_HOURS (default: every 3 hours).
    """
    from media.operations import sync_all_youtube_channels

    sync_all_youtube_channels(wait=False)
