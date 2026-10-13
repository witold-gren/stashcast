# Adding StashCast to a phone's home screen

StashCast is installable: tapping **Share → Add to Home Screen** in Safari gives an icon
that opens the app full screen, with no address bar and no Safari toolbar.

Nothing needs to be enabled for this — it is on by default.

## Installing it on an iPhone

1. Open StashCast in **Safari** (not Chrome — on iOS only Safari can install to the home
   screen).
2. **Share** → **Add to Home Screen**.
3. The name and icon are filled in already; tap **Add**.

The result behaves as its own app: it has its own window, its own back/forward history
and its own place in the app switcher.

## What is set up, and why each part is needed

| Piece | Where | What happens without it |
|---|---|---|
| `apple-touch-icon` (180×180) | page head, `SITE_FAVICONS` | iOS renders a blurry screenshot of the page as the icon |
| `apple-mobile-web-app-capable` | page head | The icon opens Safari with its address bar, not an app |
| `apple-mobile-web-app-title` | page head | The label under the icon is the `<title>`, usually far too long |
| `apple-mobile-web-app-status-bar-style` | page head | The status bar stays white over a dark app |
| `theme-color` | page head | Safari tints its UI with the page background it guesses |
| `/manifest.webmanifest` | view | Android cannot install it at all; iOS 16.4+ loses the name and icon |
| Maskable icon | manifest | Android launchers shrink the icon inside a white circle |
| `/apple-touch-icon.png` | URL | Older iPhones, which look for that exact path, find nothing |

The icons are **opaque on purpose**. iOS draws whatever it is given under its own rounded
mask and does not composite transparency, so an icon with an alpha channel comes out as a
black square.

## Configuration

All optional, all in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `STASHCAST_PWA_NAME` | `StashCast` | Full name, used by the install prompt |
| `STASHCAST_PWA_SHORT_NAME` | `StashCast` | Label under the icon (iOS cuts it at ~12 characters) |
| `STASHCAST_PWA_START_URL` | `/` | The page the icon opens |
| `STASHCAST_PWA_THEME_COLOR` | `#0f172a` | Status bar and browser UI tint |
| `STASHCAST_PWA_BACKGROUND_COLOR` | `#0a1120` | Painted before the first frame is drawn |

To have the icon open straight into the library rather than the landing page:

```bash
STASHCAST_PWA_START_URL=/admin/tools/grid/
```

Re-install the icon after changing this — iOS reads `start_url` once, when the icon is
created.

## The icons

They are generated from the logo, not drawn by hand:

```bash
./scripts/generate_app_icons.py           # rebuild media/static/media/icons/
./scripts/generate_app_icons.py --check   # report whether they match the logo
```

Run it after changing `media/static/media/header-transparent.png`, then
`./manage.py collectstatic`. A test fails if the committed icons and the logo disagree.

## Things worth knowing

**Logging in again.** An installed web app has its own cookie store, separate from
Safari's. Being logged in inside Safari does not carry over — expect to log in once more
the first time the icon is opened. Tick *remember me* if the login form offers it.

**It needs HTTPS.** Safari will not install a page served over plain HTTP. StashCast sits
behind Caddy, so this is already the case.

**Updating the icon.** iOS caches it against the home-screen entry. After changing the
artwork, delete the icon and add it again — a reload will not update it.

**No offline mode.** The service worker (`/sw.js`) only handles push notifications, it
caches nothing: the app streams media from the server, so there is nothing useful to do
without a connection. Opening it with no network shows Safari's error page.

**Splash screens** are not configured. iOS needs one image per device resolution, with an
exact media query for each, and shows a blank screen tinted with the background colour
when none matches — which is what happens here, briefly.

## Push notifications

Groups can ask before downloading a new upload, or announce a finished download (see
[group options](DOWNLOAD_QUEUE.md#approving-uploads-before-they-download)). Both arrive as
push notifications through `django-webpush`.

### 1. Generate the keys (once)

```bash
pip install py-vapid   # already installed with the requirements
vapid --gen                    # writes private_key.pem and public_key.pem
vapid --applicationServerKey   # prints the public key to use below
```

### 2. Configure `.env`

```bash
STASHCAST_VAPID_PUBLIC_KEY=BAvS...        # the "Application Server Key" printed above
STASHCAST_VAPID_PRIVATE_KEY=/data/private_key.pem
STASHCAST_VAPID_ADMIN_EMAIL=you@example.com
```

`STASHCAST_VAPID_PRIVATE_KEY` is either the path to `private_key.pem` or its content on a
single line, without the `-----BEGIN/END-----` lines:

```bash
grep -v '^-----' private_key.pem | tr -d '\n'
```

Restart StashCast (web and worker). Keep the keys: new ones invalidate every device that
subscribed with the old ones.

### 3. Turn them on per device

Open **Tools → Notifications** on the phone, tap **Enable on this device** and allow
notifications. **Send test notification** checks the whole chain. Every staff account
receives notifications on all of its subscribed devices.

| | Android (Chrome) | iPhone |
|---|---|---|
| Where | browser or installed app | only the app added to the Home Screen (iOS 16.4+) |
| Download / Reject buttons | in the notification | not supported by iOS — tapping opens the item page with the buttons |

Everything needs HTTPS. Without the keys the notification options on a group still save,
but nothing is sent.
