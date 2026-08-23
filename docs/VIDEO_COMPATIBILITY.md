# Video Playback Compatibility

## The symptom

A video downloads without any error, but Apple Podcasts on iPhone/iPad refuses it:

```
Cannot play this episode on this device
```

## The cause

The video format selector used to end in a bare `+ba` — "best audio, whatever it is".
On YouTube the best audio stream is nowadays **Opus** in a WebM container, not AAC.

Opus cannot be muxed into MP4, so `--merge-output-format mp4` cannot be honoured and
yt-dlp quietly falls back to a **Matroska (`.mkv`)** container instead. The download
succeeds and the file plays fine on a desktop, but Apple Podcasts and iOS support
neither Matroska nor Opus, so the episode fails on the device.

This is why it used to work: as long as the selector happened to land on YouTube's AAC
stream (format `140`), the result was a normal MP4.

```
bare "+ba"      →  298 (H.264/mp4) + 251 (Opus/webm)  →  merged into .mkv   ✗ iOS
"+ba[ext=m4a]"  →  298 (H.264/mp4) + 140 (AAC/m4a)    →  merged into .mp4   ✓ iOS
```

## The fix for new downloads

`STASHCAST_DEFAULT_YTDLP_ARGS_VIDEO` now pins AAC audio, with fallbacks that stay as
client-compatible as possible:

```
bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]   # H.264 + AAC: a real MP4
b[height<=720][ext=mp4]                       # ready-made progressive MP4
bv*[height<=720]+ba[ext=m4a]                  # any video + AAC, still MP4
b[height<=720]                                # last resort, rather than failing
```

Nothing to do beyond deploying — the next download of a video is an MP4 with H.264 video
and AAC audio.

## Repairing episodes you already downloaded

Files fetched before the fix are still `.mkv` and still will not play. They do **not**
need re-downloading: the video stream is already H.264, so only the audio has to be
re-encoded.

In the admin, select the items and run:

**Repair video for Apple Podcasts / iOS**

Or from the CLI:

```bash
./manage.py repair_videos --dry-run   # list what is affected, with codecs
./manage.py repair_videos             # repair everything affected
./manage.py repair_videos -n 5        # only the 5 most recent
```

`--dry-run` output tells you exactly what is wrong with each file:

```
  [.mkv h264/opus] Film mkv-a
  [.mkv h264/opus] Film mkv-b
Would repair 2 video(s)
```

What the repair does:

- copies the video stream untouched when it is already H.264 (`-c:v copy`), so this is
  much cheaper than a re-encode,
- re-encodes only the audio to AAC when needed,
- writes `-movflags +faststart` so playback can begin before the whole file is fetched,
- replaces the old file, updates `content_path`, `mime_type` and `file_size`,
- leaves already-compatible files completely alone, so it is safe to re-run.

A file is considered compatible when it is an MP4 container with H.264 video and either
AAC audio or no audio track at all.

## MIME types

RSS enclosures now report the MIME type of the **actual** container
(`video/mp4`, `video/x-matroska`, `video/webm`, …) instead of a generic
`application/octet-stream`. Podcast clients decide whether they can play an episode from
that value, so an honest type means a client that cannot play a file says so up front
rather than downloading it first.

## Checking a file by hand

```bash
ffprobe -v error -show_entries stream=codec_type,codec_name -of csv=p=0 content.mp4
# want: h264,video  and  aac,audio

ffprobe -v error -show_entries format=format_name -of csv=p=0 content.mp4
# want: mov,mp4,m4a,3gp,3g2,mj2
```

## Related

- Downloads failing outright with `HTTP Error 403: Forbidden` — see
  [YOUTUBE_AUTH.md](YOUTUBE_AUTH.md).
- Whether a group downloads audio or video — see
  [DOWNLOAD_QUEUE.md](DOWNLOAD_QUEUE.md).
