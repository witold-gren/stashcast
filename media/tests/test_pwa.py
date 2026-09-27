"""
Tests for installing StashCast to a phone's home screen.

The failure modes here are quiet ones: an icon with an alpha channel turns black behind
iOS's rounded mask, a manifest served as text/plain is ignored without a word, and a page
missing the apple-* meta tags produces a Safari bookmark instead of something that opens
as an app of its own. None of that reports an error anywhere, so it is pinned down here.
"""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

User = get_user_model()
ICON_DIR = Path(settings.BASE_DIR) / 'media' / 'static' / 'media' / 'icons'


class ManifestTest(TestCase):
    """What Android and iOS 16.4+ read to decide the app is installable"""

    def get(self):
        response = self.client.get('/manifest.webmanifest')
        return response, json.loads(response.content)

    def test_it_is_public(self):
        """The manifest is fetched before anyone has logged in"""
        response, _ = self.get()

        self.assertEqual(response.status_code, 200)

    def test_the_content_type_is_the_one_browsers_require(self):
        """Served as text/plain the manifest is silently ignored"""
        response, _ = self.get()

        self.assertEqual(response['Content-Type'], 'application/manifest+json')

    def test_it_asks_to_open_without_browser_chrome(self):
        _, manifest = self.get()

        self.assertEqual(manifest['display'], 'standalone')

    def test_it_carries_the_names_and_colours(self):
        _, manifest = self.get()

        self.assertEqual(manifest['name'], settings.STASHCAST_PWA_NAME)
        self.assertEqual(manifest['short_name'], settings.STASHCAST_PWA_SHORT_NAME)
        self.assertEqual(manifest['theme_color'], settings.STASHCAST_PWA_THEME_COLOR)
        self.assertEqual(manifest['background_color'], settings.STASHCAST_PWA_BACKGROUND_COLOR)

    @override_settings(STASHCAST_PWA_START_URL='/admin/tools/grid/')
    def test_the_start_page_is_configurable(self):
        """So the icon can open straight into the grid rather than the landing page"""
        _, manifest = self.get()

        self.assertEqual(manifest['start_url'], '/admin/tools/grid/')

    def test_icon_urls_are_absolute(self):
        """A manifest may be read from a different origin than the one it describes"""
        _, manifest = self.get()

        for icon in manifest['icons']:
            self.assertTrue(icon['src'].startswith('http'), icon['src'])

    def test_it_offers_the_sizes_android_asks_for(self):
        _, manifest = self.get()

        sizes = {icon['sizes'] for icon in manifest['icons']}
        self.assertIn('192x192', sizes)
        self.assertIn('512x512', sizes)

    def test_a_maskable_icon_is_offered(self):
        """Without one, Android launchers letterbox the icon inside a white circle"""
        _, manifest = self.get()

        purposes = {icon['purpose'] for icon in manifest['icons']}
        self.assertIn('maskable', purposes)


class HeadTagsTest(TestCase):
    """The tags iOS reads at the moment "Add to Home Screen" is tapped"""

    def landing(self):
        return self.client.get('/').content.decode()

    def test_the_landing_page_offers_the_touch_icon(self):
        self.assertIn('rel="apple-touch-icon"', self.landing())

    def test_the_landing_page_asks_for_standalone_mode(self):
        """Without this iOS produces a bookmark that opens in Safari"""
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', self.landing())

    def test_the_landing_page_links_the_manifest(self):
        self.assertIn('rel="manifest"', self.landing())

    def test_the_home_screen_label_comes_from_settings(self):
        with override_settings(STASHCAST_PWA_SHORT_NAME='Stash'):
            self.assertIn('name="apple-mobile-web-app-title" content="Stash"', self.landing())

    def test_the_status_bar_is_not_translucent(self):
        """black-translucent draws the page under the notch, which every page would then
        have to pad around"""
        self.assertIn(
            'name="apple-mobile-web-app-status-bar-style" content="black"', self.landing()
        )


class AdminHeadTagsTest(TestCase):
    """The admin is where the app is actually used, so it must be addable too"""

    def setUp(self):
        User.objects.create_superuser('pwa', 'pwa@test.com', 'password')
        self.client = Client()
        self.client.login(username='pwa', password='password')

    def page(self):
        # /admin/ redirects to the grid, which is the admin page actually looked at
        return self.client.get('/admin/tools/grid/').content.decode()

    def test_admin_pages_offer_the_touch_icon(self):
        self.assertIn('rel="apple-touch-icon"', self.page())

    def test_admin_pages_ask_for_standalone_mode(self):
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', self.page())

    def test_admin_pages_link_the_manifest(self):
        self.assertIn('rel="manifest"', self.page())

    def test_each_tag_appears_once(self):
        """The icons come from unfold's own config and the meta tags from the template
        override; including both halves in both places emitted every link twice"""
        page = self.page()

        for tag in ('rel="manifest"', 'rel="apple-touch-icon"', 'name="theme-color"'):
            self.assertEqual(page.count(tag), 1, tag)

    def test_overriding_base_site_kept_unfolds_branding(self):
        """The override replaces unfold's file wholesale; its blocks were copied along.
        If this fails after an unfold upgrade, re-compare the two templates."""
        self.assertIn('StashCast', self.page())
        self.assertNotIn('Django administration', self.page())


class WellKnownIconPathTest(TestCase):
    """Older iPhones fetch /apple-touch-icon.png without being told to"""

    def test_the_root_path_serves_the_icon(self):
        response = self.client.get('/apple-touch-icon.png')

        self.assertIn(response.status_code, (301, 302))
        self.assertEqual(response['Location'], '/static/media/icons/icon-180.png')

    def test_the_precomposed_path_works_too(self):
        response = self.client.get('/apple-touch-icon-precomposed.png')

        self.assertEqual(response['Location'], '/static/media/icons/icon-180.png')


class IconFileTest(TestCase):
    """The icons themselves, which is where the iOS-specific traps are"""

    def open(self, name):
        from PIL import Image

        return Image.open(ICON_DIR / name)

    def test_every_icon_the_manifest_promises_exists(self):
        response = self.client.get('/manifest.webmanifest')
        for icon in json.loads(response.content)['icons']:
            name = icon['src'].rsplit('/', 1)[-1]
            self.assertTrue((ICON_DIR / name).exists(), name)

    def test_the_touch_icon_is_the_size_ios_wants(self):
        self.assertEqual(self.open('icon-180.png').size, (180, 180))

    def test_icons_are_opaque(self):
        """A transparent background is drawn black behind iOS's rounded mask"""
        for name in ('icon-180.png', 'icon-192.png', 'icon-512.png'):
            image = self.open(name)
            self.assertNotIn('A', image.getbands(), name)

    def test_icons_are_square(self):
        for name in ('icon-32.png', 'icon-180.png', 'icon-512.png'):
            width, height = self.open(name).size
            self.assertEqual(width, height, name)

    def test_the_maskable_icon_leaves_room_to_be_cropped(self):
        """Android crops it to a circle of 80% of the width; artwork outside that is lost"""
        from scripts.generate_app_icons import MASKABLE_SCALE, PLAIN_SCALE

        self.assertLess(MASKABLE_SCALE, 0.6)
        self.assertLess(MASKABLE_SCALE, PLAIN_SCALE)

    def test_the_committed_icons_match_the_logo(self):
        """They are generated, so they can drift once the logo is redrawn"""
        from scripts.generate_app_icons import build

        for name, data in build().items():
            self.assertEqual((ICON_DIR / name).read_bytes(), data, name)


class LoginPageTest(TestCase):
    """The first page an uninstalled phone sees, and a likely place to add from"""

    def page(self):
        return self.client.get('/admin/login/').content.decode()

    def test_it_offers_the_touch_icon(self):
        """It does not go through admin/base_site.html, so this comes from SITE_FAVICONS"""
        self.assertIn('rel="apple-touch-icon"', self.page())

    def test_it_links_the_manifest(self):
        """Which is what lets iOS 16.4+ install it without the apple-* meta tags"""
        self.assertIn('rel="manifest"', self.page())
