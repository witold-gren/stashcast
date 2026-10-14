# StashCast YouTube button

*[Polska wersja: README.pl.md](README.pl.md)*

A userscript that puts a StashCast button on YouTube. Click it on any video and StashCast
downloads it - no copying links, no admin form.

- A round **+** button sits on every video thumbnail: home page, search, a channel's
  videos, recommendations, Shorts.
- A large **+** button floats in the bottom-right corner while you watch a video.
- One click sends the video to StashCast. The button turns into **✓** and a short message
  says where the video went.

## Where a video goes

StashCast decides this, not the script:

1. **The video's channel is followed by one of your groups** (the group's *YouTube
   channel URL*): the video goes into that group, with that group's settings - audio or
   video, transcripts, notifications. Matching works whether the group was set up with
   `@name` or `/channel/UC…`.
2. **Any other channel:** the video goes into the script's **default group**.
3. **StashCast already has it** (downloaded, queued or downloading): nothing is
   downloaded again; the button shows **✓** and "Already in StashCast".

Clicking a video that waits for approval, or that you rejected earlier, downloads it -
the click is your approval.

## What you need

- StashCast reachable from the Mac / iPhone over **HTTPS**.
- The free **[Userscripts](https://apps.apple.com/app/userscripts/id1463298887)** app
  (open source, by quoid) - for macOS and iOS. It is a Safari extension that runs scripts
  like this one, so you do not need Xcode, an Apple developer account or any signing.
- Your `STASHCAST_USER_TOKEN` - the page below fills it in for you.

## Get the script

In the StashCast admin open **Tools → YouTube Button**.

1. Pick the **Default group** (and the download type - "Group setting" is usually right).
2. Click **Update script**.
3. Use **Install / download stashcast.user.js**, or **Copy script**.

The script there already contains your server address and token. The copy in this folder
(`stashcast.user.js`) has placeholders instead; if you use it directly, edit the
configuration block at the top:

```js
const STASHCAST_URL = 'https://stashcast.example.com';
const TOKEN = 'paste-your-STASHCAST_USER_TOKEN-here';
const DEFAULT_GROUP = '';   // a group's slug, e.g. 'inbox'; '' = no group
const TYPE = 'auto';        // auto | audio | video
```

## macOS

1. Install **Userscripts** from the Mac App Store and open it once.
2. Safari → **Settings → Extensions**: tick **Userscripts**. Under its website access
   allow **youtube.com** (or "All websites").
3. Install the script, either:
   - open **Install / download stashcast.user.js** from the StashCast admin in Safari,
     click the Userscripts icon in the toolbar and choose **Install**; or
   - click the Userscripts icon → **Open Extension Page** → **+** → **New JS**, paste the
     copied script and save (⌘S).
4. Open or reload youtube.com - the **+** buttons appear on the thumbnails.

## iOS / iPadOS

1. Install **Userscripts** from the App Store and open it.
2. Tap **Set Userscripts Directory** and choose a folder, e.g. *iCloud Drive → Userscripts*.
3. Settings → **Safari → Extensions → Userscripts**: turn it on and allow
   **youtube.com** (or "All Websites").
4. Install the script, either:
   - in Safari, log in to StashCast, open **Tools → YouTube Button → Install / download
     stashcast.user.js**, then tap the extensions icon in the address bar →
     **Userscripts** → **Install**; or
   - save `stashcast.user.js` into the folder from step 2 with the Files app.
5. Open youtube.com **in Safari** - the buttons appear on the thumbnails and on the video.

The script runs in Safari only. For the **YouTube app** use the Shortcut below.

## iOS Shortcut for the YouTube app (optional)

The YouTube app cannot run scripts, but its **Share** menu can start a Shortcut that sends
the link to StashCast - with the same group logic.

1. Open **Shortcuts** → **+** → name it *StashCast*.
2. Tap **ⓘ** → turn on **Show in Share Sheet**; set it to receive **URLs**.
3. Add the action **Get Contents of URL**:
   - URL: `https://your-stashcast-address/stash/`
   - Method: **POST**, Request Body: **Form**, with the fields:

     | Key | Value |
     |---|---|
     | `token` | your `STASHCAST_USER_TOKEN` |
     | `url` | *Shortcut Input* |
     | `type` | `auto` |
     | `group` | your default group's slug (leave out for no group) |
     | `skip_existing` | `true` |

4. Add **Get Dictionary Value** → key `group`, from *Contents of URL*.
5. Add **Show Notification** → "StashCast: *Dictionary Value*".

In the YouTube app: **Share → StashCast**. The notification names the group the video
went into; an empty name means it went in without a group.

## Troubleshooting

| What you see | What to do |
|---|---|
| No buttons on YouTube | Userscripts is not allowed on youtube.com, or the script is switched off in Userscripts. Reload the page after changing it. |
| Buttons on the video page but not on thumbnails | YouTube changed its page layout. Update the script from this folder; the thumbnail finder looks for links to `/watch?v=` that contain an image. |
| `StashCast: Invalid or missing user token` | The token in the script is wrong - get the script again from the admin page. |
| `StashCast: Unknown group: …` | The default group was renamed or deleted - pick it again on the admin page. |
| `Could not reach StashCast` | Check `STASHCAST_URL` (it must be HTTPS and reachable from this device). |

The button waits for StashCast to look the video up on YouTube - a few seconds - before
it turns into ✓.

## Security

The script contains your user token: anyone who has it can add downloads to your
StashCast. Keep it to your own devices. If it leaks, change `STASHCAST_USER_TOKEN` and get
the script again.

## For developers: the endpoint

The script calls `POST /stash/` with form fields:

| Field | Meaning |
|---|---|
| `token` | `STASHCAST_USER_TOKEN` (required) |
| `url` | video URL (required); the script sends `https://www.youtube.com/watch?v=ID` |
| `type` | `auto`, `audio` or `video` (required); `auto` uses the group's download type |
| `group` | default group, by slug or name (optional) |
| `skip_existing` | `true`: report `"already": true` instead of downloading a video StashCast already has |

The JSON reply carries `group`, `matched_channel` (true when the channel's group was
used), `already` and `detail_url`.
