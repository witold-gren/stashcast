"""Context processors for media app."""

from django.conf import settings


def stashcast_settings(request):
    """Make StashCast settings available to all templates."""
    return {
        'user_token': settings.STASHCAST_USER_TOKEN,
        'require_user_token_for_feeds': settings.REQUIRE_USER_TOKEN_FOR_FEEDS,
        # Read by media/_pwa_head.html, which the admin includes too - so these have to
        # be available on every page, not only the ones with a view of their own
        'pwa_short_name': settings.STASHCAST_PWA_SHORT_NAME,
        'pwa_theme_color': settings.STASHCAST_PWA_THEME_COLOR,
    }
