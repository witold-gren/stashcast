from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q
from django.db.models.functions import Abs
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.utils.translation import ngettext
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


# Extra "sync newest N" bulk actions offered on the group list. The plain sync action
# already covers STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS and download_entire_channel covers
# "everything"; these fill the gap when a channel needs a one-off deeper catch-up.
EXTRA_SYNC_VIDEO_COUNTS = (10, 15, 20, 25, 30)


def _make_sync_action(max_videos):
    """Build a MediaGroupAdmin action that syncs the newest ``max_videos`` uploads.

    Generated rather than written out five times over: the actions differ only in how
    deep into the channel they look.
    """

    def sync_action(modeladmin, request, queryset):
        modeladmin.run_channel_sync(request, queryset, max_videos=max_videos)

    sync_action.__name__ = f'sync_youtube_last_{max_videos}'
    sync_action.short_description = gettext_lazy(
        'Sync YouTube channel - newest %(count)s videos'
    ) % {'count': max_videos}
    return sync_action


class MediaGroupAdminForm(forms.ModelForm):
    class Meta:
        model = MediaGroup
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        # "Notify on download" with nobody to notify would look switched on and do nothing
        if cleaned.get('notify_on_download') and not cleaned.get('notify_users'):
            self.add_error(
                'notify_users',
                _('Pick at least one user to notify, or untick "notify on download".'),
            )
        return cleaned


@admin.register(MediaGroup)
class MediaGroupAdmin(UnfoldModelAdmin, DemoReadOnlyAdminMixin):
    form = MediaGroupAdminForm
    filter_horizontal = ['notify_users']
    list_display = [
        'image_thumbnail',
        'name',
        'slug',
        'download_type',
        'transcribe_new_downloads',
        'transcript_in_description',
        'require_approval',
        'notify_on_download',
        'auto_delete_display',
        'item_count_display',
        'youtube_sync_display',
        'created_at',
    ]
    list_display_links = ['name']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ['created_at', 'image_preview', 'youtube_last_synced_at']
    fieldsets = [
        (
            None,
            {
                'fields': [
                    'name',
                    'slug',
                    'image',
                    'image_preview',
                    'download_type',
                    'transcribe_new_downloads',
                    'transcript_in_description',
                    'youtube_channel_url',
                    'youtube_last_synced_at',
                    'created_at',
                ]
            },
        ),
        (
            gettext_lazy('Approval & notifications'),
            {'fields': ['require_approval', 'notify_on_download', 'notify_users']},
        ),
        (
            gettext_lazy('Auto-delete'),
            {'fields': ['auto_delete_enabled', 'auto_delete_after_days']},
        ),
    ]
    actions = [
        'sync_youtube_now',
        *(f'sync_youtube_last_{count}' for count in EXTRA_SYNC_VIDEO_COUNTS),
        'download_entire_channel',
        'transcribe_group_items',
        'delete_expired_items',
    ]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        group = form.instance
        # Allowed - uploads can be answered in the admin - but easy to miss
        if group.require_approval and not group.notify_users.exists():
            self.message_user(
                request,
                _(
                    '"%(group)s" asks before downloading but notifies nobody. New uploads '
                    'will wait under "Awaiting Approval" in the admin.'
                )
                % {'group': group.name},
                level=messages.WARNING,
            )

    def auto_delete_display(self, obj):
        if not obj.auto_delete_enabled:
            return '—'
        days = obj.auto_delete_after_days
        return ngettext('%(count)s day', '%(count)s days', days) % {'count': days}

    auto_delete_display.short_description = gettext_lazy('Auto-delete')

    def delete_expired_items(self, request, queryset):
        """Run the daily auto-delete right now for the selected groups."""
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to delete items.')
        from media.operations import cleanup_expired_group_items

        enabled = queryset.filter(auto_delete_enabled=True)
        if not enabled.exists():
            self.message_user(
                request, _('None of the selected groups has auto-delete switched on.')
            )
            return

        deleted = cleanup_expired_group_items(enabled)
        self.message_user(
            request,
            ngettext(
                "Deleted %(count)s item past its group's retention period.",
                "Deleted %(count)s items past their group's retention period.",
                deleted,
            )
            % {'count': deleted},
        )

    delete_expired_items.short_description = gettext_lazy('Auto-delete expired items now')

    def item_count_display(self, obj):
        return obj.items.count()

    item_count_display.short_description = gettext_lazy('Items')

    def youtube_sync_display(self, obj):
        if not obj.youtube_channel_url:
            return '—'
        last = obj.youtube_last_synced_at
        return format_html(
            '<span title="{}">▶ {}</span>',
            obj.youtube_channel_url,
            last.strftime('%Y-%m-%d %H:%M') if last else _('never synced'),
        )

    youtube_sync_display.short_description = gettext_lazy('YouTube sync')

    def run_channel_sync(self, request, queryset, max_videos=None):
        """Queue the newest uploads of every selected channel.

        Shared by all the "sync newest N" actions.

        Args:
            request: Admin request
            queryset: Selected MediaGroup rows
            max_videos: How deep to look into the channel. None uses
                STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS.
        """
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to sync channels.')
        from media.operations import sync_group_channel

        groups = list(queryset.exclude(youtube_channel_url=''))
        if not groups:
            self.message_user(
                request, _('None of the selected groups has a YouTube channel configured.')
            )
            return

        queued = awaiting = 0
        for group in groups:
            items = sync_group_channel(group, max_videos=max_videos)
            if group.require_approval:
                awaiting += len(items)
            else:
                queued += len(items)

        depth = max_videos or settings.STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS
        message = _(
            'Checked the newest %(depth)s upload(s) of %(channels)s channel(s): '
            'queued %(queued)s new item(s). They are released a few at a time by the '
            'paced download queue.'
        ) % {'depth': depth, 'channels': len(groups), 'queued': queued}
        if awaiting:
            message += ' ' + ngettext(
                '%(count)s new item is awaiting your approval.',
                '%(count)s new items are awaiting your approval.',
                awaiting,
            ) % {'count': awaiting}
        self.message_user(request, message)

    def sync_youtube_now(self, request, queryset):
        self.run_channel_sync(request, queryset)

    sync_youtube_now.short_description = gettext_lazy(
        'Sync YouTube channel now (newest %(count)s only)'
    ) % {'count': settings.STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS}

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
                request, _('None of the selected groups has a YouTube channel configured.')
            )
            return

        interval = settings.STASHCAST_DOWNLOAD_QUEUE_MINUTES
        batch = settings.STASHCAST_DOWNLOAD_QUEUE_BATCH
        self.message_user(
            request,
            _(
                'Scanning %(channels)s channel(s) in the background. Every upload found is '
                'added to the paced queue and downloaded %(batch)s at a time every '
                '%(interval)s min - watch it with "./manage.py download_queue".'
            )
            % {'channels': len(groups), 'batch': batch, 'interval': interval},
        )

    download_entire_channel.short_description = gettext_lazy(
        'Download ENTIRE channel (paced, background)'
    )

    def transcribe_group_items(self, request, queryset):
        """Transcribe every downloaded item in the selected groups.

        For catching up on what a group already holds; the per-group checkbox only
        covers items downloaded from now on.
        """
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to create transcripts.')
        if not settings.STASHCAST_WHISPER_ENABLED:
            self.message_user(
                request,
                _(
                    'Speech to text is switched off. Set STASHCAST_WHISPER_ENABLED=true and '
                    'point STASHCAST_WHISPER_URI at your Wyoming server.'
                ),
                level=messages.WARNING,
            )
            return
        from media.tasks import queue_transcription

        items = MediaItem.objects.filter(group__in=queryset, status=MediaItem.STATUS_READY).exclude(
            content_path=''
        )
        count = queue_transcription(items)

        if not count:
            self.message_user(request, _('The selected groups hold no downloaded items.'))
            return

        language = settings.STASHCAST_WHISPER_LANGUAGE or _('auto-detect')
        self.message_user(
            request,
            _(
                'Transcribing %(count)s item(s) from %(groups)s group(s), '
                'language: %(language)s. Watch the Transcript column on the item list.'
            )
            % {'count': count, 'groups': queryset.count(), 'language': language},
        )

    transcribe_group_items.short_description = gettext_lazy(
        'Create transcript for all items in group'
    )

    def image_thumbnail(self, obj):
        url = build_group_image_url(obj)
        if url:
            return format_html(
                '<img src="{}" style="height: 40px; width: 40px; '
                'object-fit: cover; border-radius: 4px;">',
                url,
            )
        return '—'

    image_thumbnail.short_description = gettext_lazy('Image')

    def image_preview(self, obj):
        url = build_group_image_url(obj)
        if url:
            return format_html(
                '<img src="{}" style="max-height: 200px; max-width: 100%; border-radius: 6px;">',
                url,
            )
        return _('No image uploaded')

    image_preview.short_description = gettext_lazy('Image preview')


# Attach the generated "sync newest N" actions. They are set on the class rather than
# listed as callables so they behave exactly like the hand-written actions above.
for _count in EXTRA_SYNC_VIDEO_COUNTS:
    setattr(MediaGroupAdmin, f'sync_youtube_last_{_count}', _make_sync_action(_count))
del _count


class DurationCheckFilter(admin.SimpleListFilter):
    """Filter items by how well the downloaded file matches the source duration.

    Lets you pull up every incomplete download at once instead of opening them one by
    one. Requires ./manage.py check_durations (or the admin action) to have measured
    the files first - anything unmeasured lands in "not checked yet", never in "ok".
    """

    title = gettext_lazy('Duration')
    parameter_name = 'duration_check'

    def lookups(self, request, model_admin):
        tolerance = settings.STASHCAST_DURATION_TOLERANCE_SECONDS
        return [
            (
                'incomplete',
                _('Incomplete (off by more than %(seconds)ss)') % {'seconds': tolerance},
            ),
            ('ok', _('Complete')),
            ('unchecked', _('Not checked yet')),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value == 'unchecked':
            return queryset.filter(file_duration_seconds__isnull=True)
        if value not in ('incomplete', 'ok'):
            return queryset

        measured = queryset.filter(
            file_duration_seconds__isnull=False, duration_seconds__isnull=False
        ).annotate(gap=Abs(F('duration_seconds') - F('file_duration_seconds')))

        tolerance = settings.STASHCAST_DURATION_TOLERANCE_SECONDS
        if value == 'incomplete':
            return measured.filter(gap__gt=tolerance)
        return measured.filter(gap__lte=tolerance)


@admin.register(MediaItem)
class MediaItemAdmin(UnfoldModelAdmin, DemoReadOnlyAdminMixin):
    list_display = [
        'title',
        'action_links',
        'media_type',
        'group',
        'status',
        'queue_position_display',
        # 'author',
        'duration_check_display',
        'transcript_display',
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
        DurationCheckFilter,
        'transcript_status',
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
        'transcript_text_display',
        'transcript_created_at',
    ]

    fieldsets = [
        (gettext_lazy('Identification'), {'fields': ['guid', 'slug', 'source_url']}),
        (gettext_lazy('Status'), {'fields': ['status', 'group', 'error_message']}),
        (
            gettext_lazy('Media Info'),
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
            gettext_lazy('Files'),
            {
                'fields': [
                    'content_path',
                    'thumbnail_path',
                    'thumbnail_url',
                    'subtitle_path',
                    'file_size',
                    'mime_type',
                ]
            },
        ),
        (gettext_lazy('Processing'), {'fields': ['ytdlp_args', 'ffmpeg_args']}),
        (
            gettext_lazy('Metadata'),
            {
                'fields': [
                    'extractor',
                    'external_id',
                    'webpage_url',
                ]
            },
        ),
        (gettext_lazy('Summary'), {'fields': ['summary']}),
        (
            gettext_lazy('Transcript'),
            {
                'fields': [
                    'transcript_status',
                    'transcript_created_at',
                    'transcript_path',
                    'transcript_text_display',
                ]
            },
        ),
        (gettext_lazy('Logs & Preview'), {'fields': ['log_display', 'preview_display']}),
        (
            gettext_lazy('Timestamps'),
            {'fields': ['created_at', 'updated_at', 'downloaded_at', 'archived_at']},
        ),
    ]

    actions = [
        'approve_items',
        'reject_items',
        'refetch_items',
        'requeue_items',
        'refresh_publish_dates',
        'check_file_durations',
        'create_transcripts',
        'remove_from_transcription_queue',
        'remove_from_download_queue',
        'repair_videos',
        'regenerate_summaries',
        'archive_items',
        'unarchive_items',
    ]

    def queue_position_display(self, obj):
        """Position in the paced download queue, or why the item is not waiting in it.

        The queue releases due items oldest-first by created_at, so the position is the
        number of due queued items created before this one. Items serving a retry
        backoff are skipped until their deadline, so they show that time instead of a
        number - otherwise the order shown here would not match what actually happens.
        """
        if obj.status != MediaItem.STATUS_QUEUED:
            return mark_safe('<span style="opacity: .4">&mdash;</span>')

        now = timezone.now()
        if obj.next_attempt_at and obj.next_attempt_at > now:
            waiting_until = timezone.localtime(obj.next_attempt_at)
            attempt = obj.download_attempts + 1
            return format_html(
                '<span title="{}">&#9203; {} ({})</span>',
                _('Waiting out the retry backoff; not released until then.'),
                waiting_until.strftime('%H:%M'),
                _('try %(attempt)s') % {'attempt': attempt},
            )

        ahead = (
            MediaItem.objects.filter(status=MediaItem.STATUS_QUEUED, created_at__lt=obj.created_at)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .count()
        )
        return format_html(
            '<span title="{}">{}</span>', _('Release order: oldest queued item first.'), ahead + 1
        )

    queue_position_display.short_description = gettext_lazy('Queue')
    queue_position_display.admin_order_field = 'created_at'

    def duration_check_display(self, obj):
        """How the file's real duration compares with the source's.

        Shows the gap rather than the raw numbers: that is the part that tells you
        whether the download is complete.
        """
        gap = obj.duration_gap_seconds
        if gap is None:
            return format_html(
                '<span style="opacity: .4" title="{}">&mdash;</span>',
                _(
                    'Not measured yet - run ./manage.py check_durations or the '
                    '"Check file duration" action.'
                ),
            )

        tolerance = settings.STASHCAST_DURATION_TOLERANCE_SECONDS
        values = {'expected': obj.duration_seconds, 'actual': obj.file_duration_seconds}
        if gap <= tolerance:
            return format_html(
                '<span title="{}">{}</span>',
                _('Source says %(expected)ss, file plays %(actual)ss.') % values,
                _('ok'),
            )
        return format_html(
            '<span style="color: #dc3545" title="{}">{}</span>',
            _(
                'Source says %(expected)ss but the file plays only %(actual)ss - the '
                'download is incomplete, fetch it again.'
            )
            % values,
            _('short %(gap)ss') % {'gap': gap},
        )

    duration_check_display.short_description = gettext_lazy('Duration')
    duration_check_display.admin_order_field = 'file_duration_seconds'

    def check_file_durations(self, request, queryset):
        """Measure the selected files and record how far they are from the source."""
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to run checks.')
        from media.tasks import check_duration_for_item

        count = 0
        for item in queryset.filter(status=MediaItem.STATUS_READY).exclude(content_path=''):
            check_duration_for_item(item.guid)
            count += 1

        if not count:
            self.message_user(request, _('No downloaded items selected.'))
            return
        self.message_user(
            request,
            _(
                'Measuring %(count)s file(s) in the background. Reload in a moment, then '
                'use the "Duration" filter to list the incomplete ones.'
            )
            % {'count': count},
        )

    check_file_durations.short_description = gettext_lazy('Check file duration against source')

    def create_transcripts(self, request, queryset):
        """Send the selected media to the speech-to-text server."""
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to create transcripts.')
        if not settings.STASHCAST_WHISPER_ENABLED:
            self.message_user(
                request,
                _(
                    'Speech to text is switched off. Set STASHCAST_WHISPER_ENABLED=true and '
                    'point STASHCAST_WHISPER_URI at your Wyoming server.'
                ),
                level=messages.WARNING,
            )
            return
        from media.tasks import queue_transcription

        count = queue_transcription(
            queryset.filter(status=MediaItem.STATUS_READY).exclude(content_path='')
        )

        if not count:
            self.message_user(request, _('No downloaded items selected.'))
            return
        language = settings.STASHCAST_WHISPER_LANGUAGE or _('auto-detect')
        self.message_user(
            request,
            _(
                'Transcribing %(count)s item(s) in the background against %(server)s, '
                'language: %(language)s. Long recordings take a while - the Transcript '
                'column fills in as they finish.'
            )
            % {
                'count': count,
                'server': settings.STASHCAST_WHISPER_URI,
                'language': language,
            },
        )

    create_transcripts.short_description = gettext_lazy('Create transcript')

    def remove_from_transcription_queue(self, request, queryset):
        """Free the transcription queue by dropping the selected waiting items."""
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to change the queue.')
        from media.tasks import cancel_transcription

        cancelled, running = cancel_transcription(queryset)

        if not cancelled and not running:
            self.message_user(
                request, _('None of the selected items was waiting to be transcribed.')
            )
            return

        message = _('Removed %(count)s item(s) from the transcription queue.') % {
            'count': cancelled
        }
        if running:
            message += ' ' + _(
                '%(count)s item(s) are being transcribed right now and were left to '
                'finish - work already under way cannot be interrupted.'
            ) % {'count': running}
        self.message_user(request, message)

    remove_from_transcription_queue.short_description = gettext_lazy(
        'Remove from transcription queue'
    )

    def remove_from_download_queue(self, request, queryset):
        """Free the download queue by dropping the selected waiting items."""
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to change the queue.')
        from media.tasks import cancel_download

        cancelled = cancel_download(queryset)

        if not cancelled:
            self.message_user(request, _('None of the selected items was waiting to download.'))
            return

        self.message_user(
            request,
            _(
                'Removed %(count)s item(s) from the download queue. They are kept as '
                'errors saying why - use "Requeue selected items" to put them back.'
            )
            % {'count': cancelled},
        )

    remove_from_download_queue.short_description = gettext_lazy('Remove from download queue')

    def approve_items(self, request, queryset):
        """Download the selected items that are awaiting approval."""
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to approve items.')
        from media.operations import approve_item

        approved = sum(
            approve_item(item)
            for item in queryset.filter(status=MediaItem.STATUS_AWAITING_APPROVAL)
        )
        if not approved:
            self.message_user(request, _('None of the selected items was awaiting approval.'))
            return
        self.message_user(
            request,
            ngettext(
                'Downloading %(count)s approved item.',
                'Downloading %(count)s approved items.',
                approved,
            )
            % {'count': approved},
        )

    approve_items.short_description = gettext_lazy('Approve: download selected items')

    def reject_items(self, request, queryset):
        """Reject the selected items that are awaiting approval."""
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to reject items.')
        from media.operations import reject_item

        rejected = sum(
            reject_item(item) for item in queryset.filter(status=MediaItem.STATUS_AWAITING_APPROVAL)
        )
        if not rejected:
            self.message_user(request, _('None of the selected items was awaiting approval.'))
            return
        self.message_user(
            request,
            _(
                'Rejected %(count)s item(s). They are kept without files so the channel '
                'sync does not offer them again.'
            )
            % {'count': rejected},
        )

    reject_items.short_description = gettext_lazy('Reject selected items')

    def transcript_display(self, obj):
        """Where this item is in the transcription process, at a glance."""
        if obj.transcript_status == MediaItem.TRANSCRIPT_QUEUED:
            return format_html(
                '<span title="{}">&#9203; {}</span>', _('Waiting for the worker'), _('waiting')
            )
        if obj.transcript_status == MediaItem.TRANSCRIPT_RUNNING:
            return format_html(
                '<span style="color: #17a2b8" title="{}">&#9679; {}</span>',
                _('Being transcribed right now'),
                _('transcribing'),
            )
        if obj.transcript_status == MediaItem.TRANSCRIPT_FAILED:
            return format_html(
                '<span style="color: #dc3545" title="{}">{}</span>',
                obj.transcript_error or _('Unknown error'),
                _('failed'),
            )
        if obj.transcript:
            characters = len(obj.transcript)
            words = len(obj.transcript.split())
            return format_html(
                '<span title="{}">{}</span>',
                ngettext('%(count)s character', '%(count)s characters', characters)
                % {'count': characters},
                ngettext('%(count)s word', '%(count)s words', words) % {'count': words},
            )
        return mark_safe('<span style="opacity: .4">&mdash;</span>')

    transcript_display.short_description = gettext_lazy('Transcript')
    transcript_display.admin_order_field = 'transcript_status'

    def transcript_text_display(self, obj):
        """The transcript itself, readable on the item page."""
        if obj.transcript_error:
            return format_html('<p style="color: #dc3545">{}</p>', obj.transcript_error)
        if not obj.transcript:
            return _('No transcript yet')
        return format_html(
            '<pre style="background: #f5f5f5; padding: 10px; border-radius: 4px; '
            'max-height: 400px; overflow: auto; white-space: pre-wrap;">{}</pre>',
            obj.transcript,
        )

    transcript_text_display.short_description = gettext_lazy('Transcript text')

    def publish_date_display(self, obj):
        """Publication date on the source platform, or a clear marker when unknown.

        Rendered rather than shown raw so a missing date reads as missing instead of as
        an empty cell - those are the items worth running the fetch action on.
        """
        if not obj.publish_date:
            # mark_safe, not format_html: a constant string with no interpolated data
            return format_html(
                '<span style="opacity: .5" title="{}">&mdash; {}</span>',
                _(
                    'No publication date - feeds fall back to the download date. Use the '
                    '"Fetch publication date from source" action.'
                ),
                _('not set'),
            )
        local = timezone.localtime(obj.publish_date)
        return format_html(
            '<span title="{}">{}</span>',
            local.strftime('%Y-%m-%d %H:%M %Z'),
            local.strftime('%Y-%m-%d'),
        )

    publish_date_display.short_description = gettext_lazy('Published')
    publish_date_display.admin_order_field = 'publish_date'

    def file_size_display(self, obj):
        if obj.file_size:
            size_mb = obj.file_size / (1024 * 1024)
            return f'{size_mb:.2f} MB'
        return '-'

    file_size_display.short_description = gettext_lazy('File Size')

    def action_links(self, obj):
        edit_url = reverse('item_detail', args=[obj.guid])
        links = [
            format_html('<a href="{}" title="{}">👁️</a>', edit_url, _('View')),
            format_html('<a href="{}" title="{}">✏️</a>', edit_url, _('Edit')),
        ]
        if obj.log_path:
            # Link to the admin change page with log anchor
            admin_url = reverse('admin:media_mediaitem_change', args=[obj.guid])
            links.append(format_html('<a href="{}#log" title="{}">📜</a>', admin_url, _('Logs')))
        return mark_safe(' '.join(links))

    action_links.short_description = gettext_lazy('Actions')

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
            return _('No log file')

        log_path = obj.get_absolute_log_path()
        if not log_path:
            return _('No log file')

        try:
            with open(log_path, 'r') as f:
                log_content = f.read()
            return format_html(
                '<a name="log"></a><pre style="background: #f5f5f5; padding: 10px; '
                'border-radius: 4px; max-height: 400px; overflow: auto;">{}</pre>',
                log_content,
            )
        except Exception as e:
            return _('Error reading log: %(error)s') % {'error': e}

    log_display.short_description = gettext_lazy('Logs')

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
        self.message_user(
            request,
            ngettext('Re-fetching %(count)s item.', 'Re-fetching %(count)s items.', count)
            % {'count': count},
        )

    refetch_items.short_description = gettext_lazy('Re-fetch selected items (immediately)')

    def requeue_items(self, request, queryset):
        """Put items back into the paced queue with a fresh set of attempts.

        Unlike re-fetch, this does not enqueue anything right away: the queue releases
        the items a few at a time, which is what you want after a batch of failures.
        """
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to requeue items.')

        # Requeuing something a worker is downloading right now would start a second run
        # against the same tmp directory and corrupt the file. Items whose download has
        # genuinely stalled are picked up by recover_stuck_items instead.
        stale_before = timezone.now() - timedelta(minutes=settings.STASHCAST_STUCK_TIMEOUT_MINUTES)
        count = 0
        skipped = 0
        for item in queryset:
            if item.status in MediaItem.IN_PROGRESS_STATUSES and item.updated_at > stale_before:
                skipped += 1
                continue
            item.status = MediaItem.STATUS_QUEUED
            item.download_attempts = 0
            item.next_attempt_at = None
            item.error_message = ''
            item.save()
            count += 1

        message = _('Requeued %(count)s item(s) for the paced download queue.') % {'count': count}
        if skipped:
            message += ' ' + _('Skipped %(count)s item(s) that are downloading right now.') % {
                'count': skipped
            }
        self.message_user(request, message)

    requeue_items.short_description = gettext_lazy('Requeue selected items (paced queue)')

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
            _(
                'Fetching publication dates for %(count)s item(s) in the background. '
                'Reload in a moment to see them.'
            )
            % {'count': count},
        )

    refresh_publish_dates.short_description = gettext_lazy('Fetch publication date from source')

    def repair_videos(self, request, queryset):
        """Rewrite selected videos into an MP4 that Apple Podcasts / iOS can play.

        Files already in the right shape are left untouched. The video stream is copied
        where possible, so this is much cheaper than downloading the episode again.
        """
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to repair files.')
        from media.tasks import repair_video_file

        count = 0
        for item in queryset.filter(media_type=MediaItem.MEDIA_TYPE_VIDEO):
            repair_video_file(item.guid)
            count += 1

        if not count:
            self.message_user(request, _('No video items selected.'))
            return
        self.message_user(
            request,
            _(
                'Checking %(count)s video(s) in the background; anything that cannot play '
                'on Apple Podcasts is repacked as MP4/H.264/AAC.'
            )
            % {'count': count},
        )

    repair_videos.short_description = gettext_lazy('Repair video for Apple Podcasts / iOS')

    def regenerate_summaries(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to regenerate summaries.')
        count = 0
        for item in queryset:
            if item.subtitle_path:
                generate_summary(item.guid)
                count += 1
        self.message_user(
            request,
            ngettext(
                'Enqueued summary generation for %(count)s item.',
                'Enqueued summary generation for %(count)s items.',
                count,
            )
            % {'count': count},
        )

    regenerate_summaries.short_description = gettext_lazy('Regenerate summaries')

    def archive_items(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to archive items.')
        count = queryset.filter(status=MediaItem.STATUS_READY).update(
            status=MediaItem.STATUS_ARCHIVED, archived_at=timezone.now()
        )
        self.message_user(
            request,
            ngettext('Archived %(count)s item.', 'Archived %(count)s items.', count)
            % {'count': count},
        )

    archive_items.short_description = gettext_lazy('Archive selected items')

    def unarchive_items(self, request, queryset):
        if is_demo_readonly(request.user):
            raise PermissionDenied('Demo users are not allowed to unarchive items.')
        count = queryset.filter(status=MediaItem.STATUS_ARCHIVED).update(
            status=MediaItem.STATUS_READY, archived_at=None
        )
        self.message_user(
            request,
            ngettext('Unarchived %(count)s item.', 'Unarchived %(count)s items.', count)
            % {'count': count},
        )

    unarchive_items.short_description = gettext_lazy('Unarchive selected items')
