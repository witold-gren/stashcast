from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, mark_safe
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from media.models import MediaGroup, MediaItem
from media.tasks import generate_summary, process_media
from media.utils import build_group_image_url

DEMO_GROUP = 'DemoReadOnly'


def is_demo_readonly(user):
    return user.is_authenticated and user.groups.filter(name=DEMO_GROUP).exists()


class DemoReadOnlyAdminMixin:
    """
    Allows viewing change pages with normal UI, but blocks any POST that would write.
    """

    def has_module_permission(self, request):
        if is_demo_readonly(request.user):
            return True
        return super().has_module_permission(request)

    def has_add_permission(self, request):
        # Returning True keeps "Add" buttons visible in the app index/list pages.
        # They'll still be blocked on POST by add_view.
        return True

    def has_change_permission(self, request, obj=None):
        # True so they can open the change form and see Save buttons.
        return True

    def has_view_permission(self, request, obj=None):
        if is_demo_readonly(request.user):
            return True
        return super().has_view_permission(request, obj=obj)

    def has_delete_permission(self, request, obj=None):
        # True so delete UI can show, but delete_view will block POST.
        return True

    def add_view(self, request, form_url='', extra_context=None):
        if is_demo_readonly(request.user) and request.method == 'POST':
            raise PermissionDenied('Demo users are not allowed to add objects.')
        return super().add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        if is_demo_readonly(request.user) and request.method == 'POST':
            raise PermissionDenied('Demo users are not allowed to change objects.')
        return super().change_view(request, object_id, form_url, extra_context)

    def delete_view(self, request, object_id, extra_context=None):
        if is_demo_readonly(request.user) and request.method == 'POST':
            raise PermissionDenied('Demo users are not allowed to delete objects.')
        return super().delete_view(request, object_id, extra_context)

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Optional: keep actions visible if you want; but most actions write.
        # If you want them visible-but-fail, leave them. If you want to hide:
        if is_demo_readonly(request.user):
            # Prevent bulk delete and other write actions
            actions.pop('delete_selected', None)
            actions.pop('refetch_items', None)
            actions.pop('regenerate_summaries', None)
            actions.pop('archive_items', None)
            actions.pop('unarchive_items', None)
            actions.pop('sync_youtube_now', None)
        return actions


@admin.register(MediaGroup)
class MediaGroupAdmin(UnfoldModelAdmin, DemoReadOnlyAdminMixin):
    list_display = [
        'image_thumbnail',
        'name',
        'slug',
        'download_type',
        'item_count_display',
        'youtube_sync_display',
        'created_at',
    ]
    list_display_links = ['name']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ['created_at', 'image_preview', 'youtube_last_synced_at']
    fields = [
        'name',
        'slug',
        'image',
        'image_preview',
        'download_type',
        'youtube_channel_url',
        'youtube_last_synced_at',
        'created_at',
    ]
    actions = ['sync_youtube_now', 'download_entire_channel']

    def item_count_display(self, obj):
        return obj.items.count()

    item_count_display.short_description = 'Items'

    def youtube_sync_display(self, obj):
        if not obj.youtube_channel_url:
            return '—'
        last = obj.youtube_last_synced_at
        return format_html(
            '<span title="{}">▶ {}</span>',
            obj.youtube_channel_url,
            last.strftime('%Y-%m-%d %H:%M') if last else 'never synced',
        )

    youtube_sync_display.short_description = 'YouTube sync'

    def sync_youtube_now(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to sync channels.')
        from media.operations import sync_group_channel

        total = 0
        for group in queryset.exclude(youtube_channel_url=''):
            total += len(sync_group_channel(group))
        self.message_user(
            request,
            f'Queued {total} new YouTube upload(s). They are released a few at a time '
            f'by the paced download queue.',
        )

    sync_youtube_now.short_description = (
        f'Sync YouTube channel now (newest {settings.STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS} only)'
    )

    def download_entire_channel(self, request, queryset):
        """Queue every upload on the channel, not just the newest few.

        Listing a large back-catalogue is slow, so the walk itself runs as a background
        task. Everything it finds goes into the paced queue and downloads gradually.
        """
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to sync channels.')
        from media.tasks import sync_channel_full

        groups = list(queryset.exclude(youtube_channel_url=''))
        for group in groups:
            sync_channel_full(group.pk)

        if not groups:
            self.message_user(
                request, 'None of the selected groups has a YouTube channel configured.'
            )
            return

        interval = settings.STASHCAST_DOWNLOAD_QUEUE_MINUTES
        batch = settings.STASHCAST_DOWNLOAD_QUEUE_BATCH
        self.message_user(
            request,
            f'Scanning {len(groups)} channel(s) in the background. Every upload found is '
            f'added to the paced queue and downloaded {batch} at a time every '
            f'{interval} min - watch it with "./manage.py download_queue".',
        )

    download_entire_channel.short_description = 'Download ENTIRE channel (paced, background)'

    def image_thumbnail(self, obj):
        url = build_group_image_url(obj)
        if url:
            return format_html(
                '<img src="{}" style="height: 40px; width: 40px; '
                'object-fit: cover; border-radius: 4px;">',
                url,
            )
        return '—'

    image_thumbnail.short_description = 'Image'

    def image_preview(self, obj):
        url = build_group_image_url(obj)
        if url:
            return format_html(
                '<img src="{}" style="max-height: 200px; max-width: 100%; '
                'border-radius: 6px;">',
                url,
            )
        return 'No image uploaded'

    image_preview.short_description = 'Image preview'


@admin.register(MediaItem)
class MediaItemAdmin(UnfoldModelAdmin, DemoReadOnlyAdminMixin):
    list_display = [
        'title',
        'action_links',
        'media_type',
        'group',
        'status',
        # 'author',
        'publish_date_display',
        'file_size_display',
        'updated_at',
    ]

    list_filter = [
        'status',
        'media_type',
        'group',
        'requested_type',
        # Lets you narrow down to items with no publication date, select them all and
        # run the "Fetch publication date from source" action
        ('publish_date', admin.EmptyFieldListFilter),
        'publish_date',
        'created_at',
        'downloaded_at',
    ]

    list_select_related = ['group']

    autocomplete_fields = ['group']

    search_fields = [
        'title',
        'author',
        'source_url',
        'guid',
        'slug',
    ]

    readonly_fields = [
        'guid',
        'created_at',
        'updated_at',
        'downloaded_at',
        'archived_at',
        'preview_display',
        'log_display',
    ]

    fieldsets = [
        ('Identification', {'fields': ['guid', 'slug', 'source_url']}),
        ('Status', {'fields': ['status', 'group', 'error_message']}),
        (
            'Media Info',
            {
                'fields': [
                    'requested_type',
                    'media_type',
                    'title',
                    'author',
                    'description',
                    'publish_date',
                    'duration_seconds',
                ]
            },
        ),
        (
            'Files',
            {
                'fields': [
                    'content_path',
                    'thumbnail_path',
                    'subtitle_path',
                    'file_size',
                    'mime_type',
                ]
            },
        ),
        ('Processing', {'fields': ['ytdlp_args', 'ffmpeg_args']}),
        (
            'Metadata',
            {
                'fields': [
                    'extractor',
                    'external_id',
                    'webpage_url',
                ]
            },
        ),
        ('Summary', {'fields': ['summary']}),
        ('Logs & Preview', {'fields': ['log_display', 'preview_display']}),
        (
            'Timestamps',
            {'fields': ['created_at', 'updated_at', 'downloaded_at', 'archived_at']},
        ),
    ]

    actions = [
        'refetch_items',
        'requeue_items',
        'refresh_publish_dates',
        'regenerate_summaries',
        'archive_items',
        'unarchive_items',
    ]

    def publish_date_display(self, obj):
        """Publication date on the source platform, or a clear marker when unknown.

        Rendered rather than shown raw so a missing date reads as missing instead of as
        an empty cell - those are the items worth running the fetch action on.
        """
        if not obj.publish_date:
            # mark_safe, not format_html: a constant string with no interpolated data
            return mark_safe(
                '<span style="opacity: .5" title="No publication date &ndash; feeds fall '
                'back to the download date. Use the &quot;Fetch publication date from '
                'source&quot; action.">&mdash; not set</span>'
            )
        local = timezone.localtime(obj.publish_date)
        return format_html(
            '<span title="{}">{}</span>',
            local.strftime('%Y-%m-%d %H:%M %Z'),
            local.strftime('%Y-%m-%d'),
        )

    publish_date_display.short_description = 'Published'
    publish_date_display.admin_order_field = 'publish_date'

    def file_size_display(self, obj):
        if obj.file_size:
            size_mb = obj.file_size / (1024 * 1024)
            return f'{size_mb:.2f} MB'
        return '-'

    file_size_display.short_description = 'File Size'

    def action_links(self, obj):
        edit_url = reverse('item_detail', args=[obj.guid])
        links = [
            f'<a href="{edit_url}" alt="View" title="View">👁️</a>',
            f'<a href="{edit_url}" alt="Edit" title="Edit">✏️</a>',
        ]
        if obj.log_path:
            # Link to the admin change page with log anchor
            admin_url = reverse('admin:media_mediaitem_change', args=[obj.guid])
            links.append(f'<a href="{admin_url}#log" alt="Logs" title="Logs">📜</a>')
        return mark_safe(' '.join(links))

    action_links.short_description = 'Actions'

    def preview_display(self, obj):
        if obj.status != MediaItem.STATUS_READY:
            return '-'

        # Build thumbnail URL
        if obj.thumbnail_path:
            thumb_url = reverse('admin:media_mediaitem_change', args=[obj.guid])
            thumb_url = f'{thumb_url}#thumbnail'
            return format_html(
                '<img src="{}" width="100%" height="300" alt="Thumbnail" '
                'style="object-fit: contain; border-radius: 4px;">',
                thumb_url,
            )
        else:
            return '-'

    def log_display(self, obj):
        if not obj.log_path:
            return 'No log file'

        log_path = obj.get_absolute_log_path()
        if not log_path:
            return 'No log file'

        try:
            with open(log_path, 'r') as f:
                log_content = f.read()
            return format_html(
                '<a name="log"></a><pre style="background: #f5f5f5; padding: 10px; '
                'border-radius: 4px; max-height: 400px; overflow: auto;">{}</pre>',
                log_content,
            )
        except Exception as e:
            return f'Error reading log: {e}'

    log_display.short_description = 'Logs'

    def refetch_items(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to refetch items.')
        count = 0
        for item in queryset:
            item.status = MediaItem.STATUS_PREFETCHING
            item.error_message = ''
            item.save()
            process_media(item.guid)
            count += 1
        self.message_user(request, f'Re-fetching {count} items.')

    refetch_items.short_description = 'Re-fetch selected items (immediately)'

    def requeue_items(self, request, queryset):
        """Put items back into the paced queue with a fresh set of attempts.

        Unlike re-fetch, this does not enqueue anything right away: the queue releases
        the items a few at a time, which is what you want after a batch of failures.
        """
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to requeue items.')
        count = 0
        for item in queryset:
            item.status = MediaItem.STATUS_QUEUED
            item.download_attempts = 0
            item.next_attempt_at = None
            item.error_message = ''
            item.save()
            count += 1
        self.message_user(request, f'Requeued {count} item(s) for the paced download queue.')

    requeue_items.short_description = 'Requeue selected items (paced queue)'

    def refresh_publish_dates(self, request, queryset):
        """Fetch the original publication date from the source for selected items.

        For items downloaded before the date was captured, whose feed entries would
        otherwise be dated by when they were downloaded. Metadata only - nothing is
        re-downloaded.
        """
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to refresh metadata.')
        from media.tasks import refresh_publish_date

        count = 0
        for item in queryset:
            refresh_publish_date(item.guid)
            count += 1
        self.message_user(
            request,
            f'Fetching publication dates for {count} item(s) in the background. '
            f'Reload in a moment to see them.',
        )

    refresh_publish_dates.short_description = 'Fetch publication date from source'

    def regenerate_summaries(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to regenerate summaries.')
        count = 0
        for item in queryset:
            if item.subtitle_path:
                generate_summary(item.guid)
                count += 1
        self.message_user(request, f'Enqueued summary generation for {count} items.')

    regenerate_summaries.short_description = 'Regenerate summaries'

    def archive_items(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to archive items.')
        count = queryset.filter(status=MediaItem.STATUS_READY).update(
            status=MediaItem.STATUS_ARCHIVED, archived_at=timezone.now()
        )
        self.message_user(request, f'Archived {count} items.')

    archive_items.short_description = 'Archive selected items'

    def unarchive_items(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to unarchive items.')
        count = queryset.filter(status=MediaItem.STATUS_ARCHIVED).update(
            status=MediaItem.STATUS_READY, archived_at=None
        )
        self.message_user(request, f'Unarchived {count} items.')

    unarchive_items.short_description = 'Unarchive selected items'
