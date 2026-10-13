"""
URL configuration for stashcast project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView

from media.feeds import ArchiveFeed, AudioFeed, CombinedFeed, GroupFeed, VideoFeed
from media.views import (
    admin_spotify_confirm_view,
    admin_stash_confirm_multiple_view,
    admin_stash_form_view,
    admin_stash_progress_view,
    bookmarklet_view,
    feed_links_view,
    grid_view,
    home_view,
    item_archive_view,
    item_decision_view,
    item_detail_view,
    item_refetch_view,
    item_regenerate_summary_view,
    item_unarchive_view,
    list_view,
    manifest_view,
    notifications_view,
    preferences_view,
    push_decision_view,
    service_worker_view,
    stash_progress_view,
    stash_status_stream,
    stash_view,
)

admin.site.site_header = 'StashCast'  # default: "Django Administration"
admin.site.site_title = 'StashCast'  # default: "Django site admin"


urlpatterns = [
    # Favicon
    path(
        'favicon.ico',
        RedirectView.as_view(url='/static/media/favicon.ico', permanent=True),
        name='favicon',
    ),
    # "Add to Home Screen": the manifest, plus the icon at the well-known path older
    # iPhones look for when a page offers no <link rel="apple-touch-icon">
    path('manifest.webmanifest', manifest_view, name='manifest'),
    # Push notifications: the service worker must live at the root so its scope is the
    # whole site; webpush.urls saves the subscriptions of each device
    path('sw.js', service_worker_view, name='service_worker_root'),
    path('webpush/', include('webpush.urls')),
    path(
        'apple-touch-icon.png',
        RedirectView.as_view(url='/static/media/icons/icon-180.png', permanent=True),
        name='apple_touch_icon',
    ),
    path(
        'apple-touch-icon-precomposed.png',
        RedirectView.as_view(url='/static/media/icons/icon-180.png', permanent=True),
        name='apple_touch_icon_precomposed',
    ),
    # Landing page
    path('', home_view, name='home'),
    # Custom admin tools (must come before admin.site.urls)
    path('admin/tools/bookmarklet/', bookmarklet_view, name='bookmarklet'),
    path('admin/tools/feeds/', feed_links_view, name='feed_links'),
    path('admin/tools/add-url/', admin_stash_form_view, name='admin_stash_form'),
    path(
        'admin/tools/add-url/confirm-multiple/',
        admin_stash_confirm_multiple_view,
        name='admin_stash_confirm_multiple',
    ),
    path(
        'admin/tools/add-url/spotify/',
        admin_spotify_confirm_view,
        name='admin_spotify_confirm',
    ),
    path(
        'admin/tools/add-url/progress/<str:guid>/',
        admin_stash_progress_view,
        name='admin_stash_progress',
    ),
    path('admin/tools/grid/', grid_view, name='grid_view'),
    path('admin/tools/list/', list_view, name='list_view'),
    path('admin/tools/item/<str:guid>/', item_detail_view, name='item_detail'),
    path('admin/tools/item/<str:guid>/archive/', item_archive_view, name='item_archive'),
    path('admin/tools/item/<str:guid>/unarchive/', item_unarchive_view, name='item_unarchive'),
    path('admin/tools/item/<str:guid>/refetch/', item_refetch_view, name='item_refetch'),
    path(
        'admin/tools/item/<str:guid>/regenerate-summary/',
        item_regenerate_summary_view,
        name='item_regenerate_summary',
    ),
    re_path(
        r'^admin/tools/item/(?P<guid>[^/]+)/(?P<action>approve|reject)/$',
        item_decision_view,
        name='item_decision',
    ),
    path('admin/tools/notifications/', notifications_view, name='notifications'),
    path('admin/tools/preferences/', preferences_view, name='preferences'),
    # Redirect admin index to grid view (must come before admin.site.urls)
    re_path(r'^admin/$', RedirectView.as_view(url='/admin/tools/grid/', permanent=False)),
    # Standard admin (includes django-huey-monitor integration)
    path('admin/', admin.site.urls),
    # Public endpoints
    path('stash/', stash_view, name='stash'),
    path('stash/<str:guid>/progress/', stash_progress_view, name='stash_progress'),
    path('stash/<str:guid>/stream/', stash_status_stream, name='stash_status_stream'),
    # Download / Reject buttons of a push notification (signed token, no session)
    re_path(
        r'^push/item/(?P<guid>[^/]+)/(?P<action>approve|reject)/$',
        push_decision_view,
        name='push_decision',
    ),
    path('feeds/audio.xml', AudioFeed(), name='audio_feed'),
    path('feeds/video.xml', VideoFeed(), name='video_feed'),
    path('feeds/combined.xml', CombinedFeed(), name='combined_feed'),
    path('feeds/archive.xml', ArchiveFeed(), name='archive_feed'),
    path('feeds/group/<slug:slug>.xml', GroupFeed(), name='group_feed'),
]

# Serve media files in development
if not settings.STASHCAST_MEDIA_BASE_URL:
    urlpatterns += static('/media/files/', document_root=settings.MEDIA_ROOT)
