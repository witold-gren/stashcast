# Transcripts

Episodes can be transcribed by a **Wyoming protocol** speech-to-text server that you run
yourself — for example `wyoming-whisper`. The audio never leaves your network, and there
is no quota: unlike YouTube's own transcripts, nothing here depends on a Premium account.

Podcast clients that support transcripts — [Apple Podcasts](https://support.apple.com/pl-pl/guide/iphone/iph9426049e9/ios)
among them — pick the result up from the `<podcast:transcript>` tag in the RSS feed.

## Setting it up

Point the app at your server and switch the feature on:

```bash
STASHCAST_WHISPER_ENABLED=true
STASHCAST_WHISPER_URI=tcp://whisper:10300
```

If the server runs in another container, make sure the app can reach it — either on a
shared Docker network (then `whisper` is the container name) or by host and port.

| Variable | Default | Purpose |
|----------|---------|---------|
| `STASHCAST_WHISPER_ENABLED` | `false` | Turns the feature on |
| `STASHCAST_WHISPER_URI` | `tcp://whisper:10300` | `tcp://host:port`, `host:port` or `host` |
| `STASHCAST_WHISPER_LANGUAGE` | site language | Language hint, e.g. `pl`. Empty = let the model detect it |
| `STASHCAST_WHISPER_WINDOW_SECONDS` | `15` | Length of each audio window (minimum 5) |
| `STASHCAST_WHISPER_TIMEOUT_SECONDS` | `300` | Socket timeout for one window |

## Creating a transcript

Select the items in admin → Items and run:

**Create transcript**

The work happens in the background; the **Transcript** column fills in as episodes
finish. A long recording takes a while — the audio is sent window by window, so progress
is steady rather than one long silence.

With the feature switched off the action says so instead of quietly doing nothing.

## How it works

Whisper returns one block of text per request with no timings, so the audio is sent in
fixed windows and the window boundaries become the cue timings of the resulting WebVTT:

```
WEBVTT

00:00:00.000 --> 00:00:15.000
<the first fifteen seconds>

00:00:15.000 --> 00:00:30.000
<the next fifteen seconds>
```

Smaller windows give finer timings and shorter individual requests; larger ones give the
model more surrounding context, which can help it at sentence boundaries. Change it with
`STASHCAST_WHISPER_WINDOW_SECONDS`; values below 5 seconds are raised to 5. The text is also stored on the item itself, so it can be
read and searched in the admin.

The file is written as `transcript.vtt` next to the media and served through the feed.
It is deliberately kept apart from `subtitle_path`, so generating a transcript never
overwrites subtitles that came with the video — and when both exist, the generated
transcript is the one announced, since it always covers the whole episode.

## Checking the result

```bash
# What the feed tells clients
curl -s http://localhost:8000/feeds/audio.xml | grep podcast:transcript
```

The tag carries the real MIME type of the file and the configured language, so a Polish
transcript is announced as Polish rather than as English.

## Not yet

Choosing transcription per group, and running it for a whole group through the paced
queue, are not built yet — for now transcripts are created from the admin action on
hand-picked items.
