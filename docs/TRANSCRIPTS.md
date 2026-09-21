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
| `STASHCAST_WHISPER_LANGUAGE` | *(auto-detect)* | Language of the **audio**, e.g. `pl` |
| `STASHCAST_WHISPER_WINDOW_SECONDS` | `15` | Length of each audio window (minimum 5) |
| `STASHCAST_WHISPER_TIMEOUT_SECONDS` | `300` | Socket timeout for one window |

## Language

This is the language **people speak in the episodes** — not the language of the admin
interface. Getting it wrong is not harmless: told that a Polish recording is English,
Whisper answers with an English translation of what it heard, and the transcript comes
back half in one language and half in the other.

```bash
STASHCAST_WHISPER_LANGUAGE=pl
```

Left empty, the model works it out for itself. Whisper decides per request, so the
language heard in the **first window is pinned for the whole episode** — otherwise it
can hear Polish at the start and drift into English translation a minute later.

Setting it explicitly is still better whenever you know the language: detection on a
15-second window is not always right, and the whole episode then follows that one guess.

The language actually in use is written to the download log and shown in the message
after the admin action, so it is never a mystery.

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

## Per group

Each group has a **Transcribe new downloads** checkbox, off by default. With it on,
every episode downloaded into that group is sent for transcription once the download
finishes. Existing items are not touched — use the actions for those.

To catch up on what a group already holds, select it in admin → Groups and run:

**Create transcript for all items in group**

A transcription problem can never damage a download: the hook that starts it swallows
its own errors, because the download task treats an exception at that point as a failed
download and would re-fetch a file that was perfectly fine. A failed transcription
leaves the episode `READY` and playable, with the reason recorded in **Transcript
error**.

## Publishing the transcript inside the description

Apple Podcasts will not show a transcript from a private feed (see *Where transcripts
can be read* below). The way around it is to publish the text inside the episode
description, where every app shows it.

That is a checkbox on the group — **Publish transcript in description** — because it
depends entirely on episode length: on a 6-minute episode the description reads nicely,
on a 60-minute one it becomes a wall of text. Off by default.

The result looks like this in the feed:

```
Zwykły opis odcinka.

────────────────────
Transkrypcja
────────────────────

pierwsze zdanie drugie zdanie trzecie zdanie…
```

The `<podcast:transcript>` tag still points at the WebVTT file, so apps that understand
it keep the timed version. Only the description gains a copy, as running text — the
stored transcript keeps one line per audio window, which would read as a column of
fragments.

Nothing is written to the database: description and transcript stay separate fields, and
separate sections in the admin. The merge happens while the feed is generated.

Two settings control the formatting for every group that has the box ticked:

| Variable | Default | Purpose |
|----------|---------|---------|
| `STASHCAST_TRANSCRIPT_HEADING` | `Transcript` | Heading above the text |
| `STASHCAST_TRANSCRIPT_IN_DESCRIPTION_MAX_CHARS` | `0` | Cap in characters, 0 = no limit |

A full transcript is roughly 1 KB per minute of audio. On a library of 85 episodes that
adds about 2 MB to a feed that is downloaded on every refresh, so on a large group the
cap is worth setting.

## Following progress

The **Transcript** column on the item list shows where each episode stands:

| Column | Meaning |
|---|---|
| `⏳ waiting` | queued, not started |
| `● transcribing` | being processed right now |
| `152 words` | finished |
| `failed` | failed — reason in the tooltip and on the item page |
| `—` | never attempted |

The **Transcript status** filter narrows the list to any one of those, which is how you
see the queue and what is running. The text itself, and the failure reason, are on the
item page under **Transcript**.

Transcription state is deliberately separate from the download's `status` and
`error_message`: an episode can download perfectly and still fail to transcribe, and
conflating the two would make a good download look broken.

## Where transcripts can be read

Apple Podcasts will not display these transcripts. Its own documentation states that
transcripts are not shown for episodes outside the Apple Podcasts catalog, and that
private RSS feeds are not processed at all — so there is no way to switch on "Display
transcripts I provide" for a self-hosted feed. Polish is also absent from the languages
Apple accepts for transcripts.

Apps that do read `<podcast:transcript>` from the feed include Player FM, Podcast
Addict, Metacast, Fountain and Goodpods. For Apple Podcasts, use the description option
above instead.

## Not yet

Transcription does not go through the paced download queue — the actions hand work
straight to the worker. If you transcribe a large back-catalogue at once, the Whisper
machine sets the pace.
