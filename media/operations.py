"""
High-level operations that can be used by views, tasks, and management commands.

This module provides testable functions that encapsulate business logic,
making it easy to test operations without going through Django views or
management commands.
"""

from pathlib import Path


from media.models import MediaItem


def resolve_requested_type(requested_type, group):
    """Apply a group's download convention when no explicit type was asked for.

    A group is either an audio or a video collection, so anything landing in it should
    follow that convention. An explicit 'audio'/'video' from the caller still wins -
    only 'auto' (i.e. "no preference") defers to the group.

    Args:
        requested_type: 'auto', 'audio', 'video', or a falsy value
        group: MediaGroup instance or None

    Returns:
        str: The type to download with.
    """
    if group is None:
        return requested_type or 'auto'
    if requested_type in (None, '', 'auto'):
        return group.download_type
    return requested_type


def stash_url(url, requested_type='auto', wait=False, logger=None, group=None, queued=False):
    """
    Stash a URL for download.

    This is the core operation used by:
    - Web /stash/ endpoint
    - Management command: ./manage.py stash
    - API calls

    Args:
        url: URL to download
        requested_type: 'auto', 'audio', or 'video'
        wait: If True, run synchronously. If False, enqueue background task.
        logger: Optional callable(message) for logging
        group: Optional MediaGroup to assign the item to
        queued: If True, leave the item in STATUS_QUEUED without enqueuing anything.
            The paced queue (process_download_queue) releases it later, one batch per
            interval. Used by the YouTube channel sync so that adding a channel with
            many uploads does not flood the workers. Ignored when ``wait`` is True.

    Returns:
        MediaItem: The created or reused MediaItem instance

    Example:
        >>> item = stash_url('http://example.com/video.mp4', 'auto', wait=True)
        >>> print(item.guid)
    """
    # A group's download convention applies unless an explicit type was requested
    requested_type = resolve_requested_type(requested_type, group)

    # Convert requested_type string to MediaItem constant
    type_map = {
        'auto': MediaItem.REQUESTED_TYPE_AUTO,
        'audio': MediaItem.REQUESTED_TYPE_AUDIO,
        'video': MediaItem.REQUESTED_TYPE_VIDEO,
    }
    requested_type_const = type_map.get(requested_type, MediaItem.REQUESTED_TYPE_AUTO)

    def log(message):
        if logger:
            logger(message)

    # Check for existing item with same URL and requested type
    if requested_type == 'auto':
        # For 'auto', match with other 'auto' requests
        existing_item = MediaItem.objects.filter(
            source_url=url, requested_type=MediaItem.REQUESTED_TYPE_AUTO
        ).first()
    else:
        # For explicit types, match with items that have that media_type
        existing_item = MediaItem.objects.filter(source_url=url, media_type=requested_type).first()

    # A queued item waits for the paced queue instead of starting right away
    use_queue = queued and not wait
    initial_status = MediaItem.STATUS_QUEUED if use_queue else MediaItem.STATUS_PREFETCHING

    if existing_item:
        # Reuse existing item (overwrite behavior)
        item = existing_item
        item.requested_type = requested_type_const
        item.status = initial_status
        item.error_message = ''
        item.download_attempts = 0
        item.next_attempt_at = None
        if group is not None:
            item.group = group
        item.save()
        log(f'Reusing existing item: {item.guid}')
    else:
        # Create new item
        item = MediaItem.objects.create(
            source_url=url,
            requested_type=requested_type_const,
            slug='pending',  # Will be set during processing
            group=group,
            status=initial_status,
        )
        log(f'Created new item: {item.guid}')

    # Process the media item
    from media.tasks import process_media

    if wait:
        # Run synchronously (blocking) - used by CLI
        log('Processing synchronously...')
        process_media.call_local(item.guid)
    elif use_queue:
        # Leave it for process_download_queue to release
        log('Queued for the paced download queue')
    else:
        # Enqueue background task - used by web
        log('Enqueued background task')
        process_media(item.guid)

    return item


def transcode_file(input_path, output_dir=None, requested_type='auto', metadata=None, logger=None):
    """
    Transcode a file without storing in database.

    This is used by the standalone fetch command for batch processing.

    Args:
        input_path: Path to input media file
        output_dir: Directory to write output (default: current directory)
        requested_type: 'auto', 'audio', or 'video'
        metadata: Optional dict with title, author, description
        logger: Optional callable(message) for logging

    Returns:
        Path: Path to output file

    Example:
        >>> output = transcode_file('input.mp4', './output', 'audio')
        >>> print(output)
        ./output/input.m4a
    """
    from media.service.transcode_service import transcode_to_target_format

    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f'Input file not found: {input_path}')

    if output_dir is None:
        output_dir = Path.cwd()
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    def log(message):
        if logger:
            logger(message)

    log(f'Transcoding: {input_path}')
    log(f'Output dir: {output_dir}')
    log(f'Type: {requested_type}')

    # Determine output extension and path
    from media.service.media_info import get_output_extension

    output_ext = get_output_extension(requested_type, input_path.suffix)
    output_path = output_dir / f'{input_path.stem}{output_ext}'

    # Run transcode
    transcode_to_target_format(
        input_path=input_path,
        output_path=output_path,
        resolved_type=requested_type,
        metadata=metadata or {},
        logger=log,
    )

    log(f'Output: {output_path}')
    return output_path


def generate_summary_for_item(guid, logger=None):
    """
    Generate summary for a media item from its subtitles.

    Args:
        guid: MediaItem GUID
        logger: Optional callable(message) for logging

    Returns:
        str: Generated summary text, or None if no subtitles or summary disabled

    Example:
        >>> summary = generate_summary_for_item('abc123xyz')
        >>> print(summary)
        'This video discusses...'
    """

    def log(message):
        if logger:
            logger(message)

    from media.tasks import generate_summary

    item = MediaItem.objects.get(guid=guid)

    log(f'Generating summary for: {item.title or item.guid}')

    # Run summary generation
    generate_summary(guid)

    # Refresh from database to get updated summary
    item.refresh_from_db()

    if item.summary:
        log(f'Summary generated: {len(item.summary)} characters')
    else:
        log('No summary generated (subtitles missing or summary disabled)')

    return item.summary


def sync_group_channel(group, max_videos=None, wait=False, logger=None, immediate=False):
    """
    Check a group's YouTube channel for new uploads and stash them.

    Uploads are downloaded as audio or video according to the group's ``download_type``.

    A video is considered "new" when no MediaItem with its URL is already assigned
    to this group, so re-running only picks up uploads that appeared since last time.
    Downloaded items are attached to ``group`` and reuse the normal processing
    pipeline via ``stash_url``.

    New items are left in STATUS_QUEUED rather than enqueued immediately: the paced
    queue releases them one batch per interval, so adding a channel with many uploads
    does not flood the workers. Pass ``immediate=True`` to enqueue them all at once.

    Args:
        group: MediaGroup instance (must have youtube_channel_url set)
        max_videos: How many of the most recent uploads to consider. ``0`` means no
            cap - the channel's entire back-catalogue. Defaults to
            settings.STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS.
        wait: If True, download synchronously (used by CLI); otherwise enqueue.
        immediate: If True, enqueue downloads right away instead of using the paced
            queue. Ignored when ``wait`` is True.
        logger: Optional callable(message) for logging

    Returns:
        list[MediaItem]: The newly stashed items (empty if nothing new).
    """
    from django.conf import settings
    from django.utils import timezone

    from media.service.youtube_channel import list_channel_videos

    def log(message):
        if logger:
            logger(message)

    if not group.youtube_channel_url:
        log(f'Group "{group.name}" has no YouTube channel configured; skipping')
        return []

    # Note: `is None` not falsiness - 0 is a meaningful value meaning "no cap"
    if max_videos is None:
        max_videos = settings.STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS

    log(f'Syncing group "{group.name}" from {group.youtube_channel_url}')
    videos = list_channel_videos(group.youtube_channel_url, max_videos=max_videos, logger=logger)

    new_items = []
    for video in videos:
        already_present = MediaItem.objects.filter(
            source_url=video.url, group=group
        ).exists()
        if already_present:
            continue
        log(f'New upload: {video.title or video.url}')
        if group.require_approval:
            item = create_awaiting_item(video, group, wait=wait, logger=logger)
        else:
            item = stash_url(
                video.url,
                requested_type=group.download_type,
                wait=wait,
                logger=logger,
                group=group,
                queued=not immediate,
            )
        new_items.append(item)

    group.youtube_last_synced_at = timezone.now()
    group.save(update_fields=['youtube_last_synced_at'])

    log(f'Group "{group.name}": {len(new_items)} new upload(s) stashed')
    return new_items


def find_group_for_channel(channel_id=None, handle=None, channel_url=None):
    """
    The group that follows a given YouTube channel, if any.

    Matches the channel's id, @handle or URL against every group's
    youtube_channel_url, whichever form the group was set up with.

    Returns:
        MediaGroup or None
    """
    from media.models import MediaGroup
    from media.service.youtube_channel import channel_keys

    wanted = channel_keys(channel_url, channel_id=channel_id, handle=handle)
    if not wanted:
        return None
    for group in MediaGroup.objects.exclude(youtube_channel_url='').order_by('pk'):
        if channel_keys(group.youtube_channel_url) & wanted:
            return group
    return None


def create_awaiting_item(video, group, wait=False, logger=None):
    """
    Record a channel upload that needs the user's approval before it is downloaded.

    Only metadata is fetched (prefetch_for_approval), which then sends the push
    notification with Download / Reject buttons. The title from the channel listing is
    stored up front so the item is recognisable even if that prefetch fails.

    Args:
        video: ChannelVideo from list_channel_videos
        group: MediaGroup the upload belongs to
        wait: If True, fetch the metadata synchronously (used by CLI)
        logger: Optional callable(message) for logging

    Returns:
        MediaItem: The new item, in STATUS_AWAITING_APPROVAL
    """
    from media.tasks import prefetch_for_approval

    item = MediaItem.objects.create(
        source_url=video.url,
        requested_type=resolve_requested_type(group.download_type, group),
        slug='pending',
        title=video.title or '',
        external_id=video.external_id or '',
        group=group,
        status=MediaItem.STATUS_AWAITING_APPROVAL,
    )
    if logger:
        logger(f'Awaiting approval: {item.guid}')

    if wait:
        prefetch_for_approval.call_local(item.guid)
    else:
        prefetch_for_approval(item.guid)
    return item


def approve_item(item):
    """
    Download an item that was waiting for approval.

    It skips the paced queue: the user asked for this one item, so it starts right away.

    Returns:
        bool: True when the download was started, False when the item was not
        awaiting approval (already answered, e.g. a second tap on the notification).
    """
    from media.tasks import process_media

    updated = MediaItem.objects.filter(
        guid=item.guid, status=MediaItem.STATUS_AWAITING_APPROVAL
    ).update(status=MediaItem.STATUS_PREFETCHING, error_message='')
    if not updated:
        return False
    item.refresh_from_db()
    process_media(item.guid)
    return True


def reject_item(item):
    """
    Decline an item that was waiting for approval.

    The record stays (without files) as STATUS_REJECTED so the channel sync does not
    offer the same upload again.

    Returns:
        bool: True when the item was rejected, False when it was not awaiting approval.
    """
    updated = MediaItem.objects.filter(
        guid=item.guid, status=MediaItem.STATUS_AWAITING_APPROVAL
    ).update(status=MediaItem.STATUS_REJECTED)
    if updated:
        item.refresh_from_db()
    return bool(updated)


def cleanup_expired_group_items(groups=None, logger=None):
    """
    Delete downloaded items older than their group's auto_delete_after_days.

    Only groups with auto_delete_enabled are touched. Ready and archived items are
    deleted together with their files (see media.signals.cleanup_media_files); rejected
    items are kept because they stop the channel sync from offering them again.

    Args:
        groups: Optional MediaGroup queryset/iterable to limit the cleanup to
        logger: Optional callable(message) for logging

    Returns:
        int: Number of items deleted.
    """
    from datetime import timedelta

    from django.utils import timezone

    from media.models import MediaGroup

    if groups is None:
        groups = MediaGroup.objects.all()

    now = timezone.now()
    total = 0
    for group in groups:
        if not group.auto_delete_enabled:
            continue
        cutoff = now - timedelta(days=group.auto_delete_after_days)
        expired = group.items.filter(
            status__in=(MediaItem.STATUS_READY, MediaItem.STATUS_ARCHIVED),
            downloaded_at__lt=cutoff,
        )
        # The pre_delete signal removes each item's directory, also for bulk deletes
        _, per_model = expired.delete()
        count = per_model.get(MediaItem._meta.label, 0)
        if count and logger:
            logger(
                f'Group "{group.name}": deleted {count} item(s) older than '
                f'{group.auto_delete_after_days} days'
            )
        total += count
    return total


def sync_all_youtube_channels(wait=False, logger=None, immediate=False):
    """
    Sync every group that has a YouTube channel configured.

    Args:
        wait: If True, download synchronously; otherwise enqueue background tasks.
        logger: Optional callable(message) for logging
        immediate: If True, enqueue downloads right away instead of using the paced
            queue. Ignored when ``wait`` is True.

    Returns:
        list[MediaItem]: All newly stashed items across all groups.
    """
    from media.models import MediaGroup

    def log(message):
        if logger:
            logger(message)

    groups = MediaGroup.objects.exclude(youtube_channel_url='')
    log(f'Checking {groups.count()} group(s) with a YouTube channel')

    stashed = []
    for group in groups:
        try:
            stashed.extend(
                sync_group_channel(group, wait=wait, logger=logger, immediate=immediate)
            )
        except Exception as e:
            log(f'Failed to sync group "{group.name}": {e}')
    return stashed
