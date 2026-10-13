# Paced Download Queue

Adding a YouTube channel used to hand every new upload to the workers at once. With
two workers, everything past the first couple sat in the queue, and anything that
failed stayed failed until somebody re-synced it by hand.

The paced queue fixes both: channel uploads wait in status `QUEUED` and are released
a small batch at a time, and a download that fails is retried automatically.

## How it works

```
channel sync  ──▶  QUEUED  ──▶  PREFETCHING ──▶ DOWNLOADING ──▶ PROCESSING ──▶ READY
                     ▲   (1 per 5 min)              │
                     │                              │ failure, attempts left
                     └──────────────────────────────┤
                                                    │ attempts exhausted
                                                    └──▶ ERROR
```

- **`sync_youtube_channels`** (every 3 h by default) finds new uploads and creates them
  as `QUEUED`. Nothing is handed to a worker yet.
- **`process_download_queue`** (every 5 min by default) releases
  `STASHCAST_DOWNLOAD_QUEUE_BATCH` items — one by default — and first requeues anything
  a dead worker abandoned.
- **A failed download** goes back to `QUEUED` with an exponential backoff deadline
  (5 min, then 10, then 20, capped at 6 h) until `STASHCAST_DOWNLOAD_MAX_ATTEMPTS` is
  reached, then becomes `ERROR`.

Manual stashes — the web form, the bookmarklet, `./manage.py stash` — are **not** paced.
They still start immediately, so the UI stays responsive.

At one item per five minutes the queue moves about 288 items per day. A channel with a
200-video backlog therefore takes roughly 17 hours to drain, at a load the workers can
actually sustain. Raise `STASHCAST_DOWNLOAD_QUEUE_BATCH` to speed that up.

## Watching the queue

```bash
./manage.py download_queue
```

```
Download queue
  Rate:        1 item(s) every 5 min
  Max tries:   3
  Queued:      8 (7 due now)
  In progress: 1
  Failed:      1
  Drains in:   ~40 min at the current rate

  Next up:
    - Odcinek 0 [now]
    - Zepsuty [at 21:45] (try 2)

  Worker: alive
```

`Queued` counts everything waiting; `due now` excludes items still serving a retry
backoff. `Worker` reads the heartbeat file, which a busy worker refreshes as it works and
an idle one refreshes once a minute.

## Driving it by hand

```bash
# Release one batch right now instead of waiting for the next cycle
./manage.py download_queue --run-now

# Release five items at once (e.g. to backfill a new channel faster)
./manage.py download_queue --run-now -n 5

# Requeue whatever a crashed worker left mid-download
./manage.py download_queue --recover

# Give every ERROR item a fresh set of attempts
./manage.py download_queue --retry-errors
```

In the admin, the item list has two actions:

- **Requeue selected items (paced queue)** — back to `QUEUED` with a clean slate; the
  queue releases them gradually. Use this after a batch of failures.
- **Re-fetch selected items (immediately)** — the old behaviour, starts right away.
  Fine for one or two items, not for fifty.

## Adding a new channel

Set the channel URL on a group (admin → Groups), then either wait for the periodic sync
or trigger it:

```bash
./manage.py sync_youtube --group lekcje
```

The uploads land in the queue and download roughly one per five minutes. To fall back to
the old "everything at once" behaviour:

```bash
./manage.py sync_youtube --group lekcje --immediate
```

## Audio or video per group

Each group carries a **download type** (admin → Groups → *Download type*): `audio` or
`video`. Everything that lands in the group follows it, so a group stays a consistent
audio or video collection.

It applies to:

- the periodic channel sync and full-channel scans for that group,
- anything added to the group **without** an explicit type of its own.

An explicit choice still wins: picking *Video* on the stash form puts a video into an
audio group. Only "auto" — meaning "no preference" — defers to the group's convention.

The default is `audio`, which is what the channel sync always did, so existing groups keep
behaving exactly as before.

> Note on the item-level fields: `MediaItem` has both `requested_type` (`auto`/`audio`/
> `video` — what was asked for) and `media_type` (`audio`/`video` — what was produced).
> A group needs only one field because it just expresses intent. See
> [ARCHITECTURE.md](ARCHITECTURE.md) for why the item keeps both.

## Approving uploads before they download

Some channels publish far more than you want to listen to. Tick **Require approval** on
the group (admin → Media groups → *Approval & notifications*) and the sync stops
downloading on its own:

1. A new upload is recorded as **Awaiting approval**. Only its title, description and
   thumbnail are fetched, nothing is downloaded and nothing appears in any feed.
2. A push notification shows the title and thumbnail with two buttons. It goes only to
   the accounts picked in the group's **Notify users** list:
   - **Download** starts the download right away (it skips the paced queue — you asked
     for this one). Once it finishes it is in the group's feed like any other episode.
   - **Reject** marks it **Rejected**. The record stays, without files, so the next sync
     does not ask about the same upload again.
3. On a phone without notification buttons (iPhone) tapping the notification opens the
   item page, which has the same two buttons. The item list in the admin has *Approve* /
   *Reject* bulk actions too, and the sidebar link **Awaiting Approval** lists what is
   still open.

The buttons in a notification keep working for 14 days and each one only does its own
action on its own item. Push needs to be set up once, see
[push notifications](HOME_SCREEN_APP.md#push-notifications). Without it the items still
wait for approval — answer them from the admin.

### Notify when a download is ready

**Notify on download** sends a notification to the group's **Notify users** whenever a
new item in the group finishes downloading. The group cannot be saved with it ticked and
nobody picked. *Require approval* with an empty list is allowed (answer in the admin), and
saving it shows a warning. It is ignored while *Require approval* is on: there you already chose to
download the item from a notification, and a second one would only repeat it.

### Deleting old episodes automatically

Under *Auto-delete*, tick **Auto delete enabled** and set **Auto delete after days**
(default 90). Every night at 04:30 items in that group which were downloaded longer ago
than that — ready or archived — are deleted together with their files. Groups without
the tick are never touched. Rejected items are kept, they hold no files.

The *Auto-delete expired items now* action on the group list runs the same cleanup on
demand.

A deleted item is gone from the database, so **Download ENTIRE channel** would fetch it
again (or, with approval on, ask about it again). The regular sync only looks at the
newest few uploads and does not reach that far back.

## Catching up on a channel

The periodic sync only looks at the newest `STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS` uploads,
which is what you want for keeping up. When a channel needs a deeper one-off catch-up,
select the group in admin → Groups and pick how far back to look:

| Action | Looks at |
|---|---|
| **Sync YouTube channel now** | the newest `STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS` (default 5) |
| **Sync YouTube channel - newest 10 / 15 / 20 / 25 / 30 videos** | exactly that many |
| **Download ENTIRE channel (paced, background)** | everything |

The depth options are generated from `EXTRA_SYNC_VIDEO_COUNTS` in `media/admin.py`; edit
that tuple to offer different numbers.

All of them skip videos already present in the group, so re-running only picks up what is
missing, and everything found goes into the paced queue rather than starting at once.

The CLI equivalent of a specific depth:

```bash
./manage.py sync_youtube --group lekcje --max 20
```

## Downloading an entire channel

To fetch a channel's whole back-catalogue, select the group in admin → Groups and run:

**Download ENTIRE channel (paced, background)**

Listing a large channel takes a while, so the scan runs as a background task and the
admin page returns immediately. Every upload found is added to the queue and downloaded
at the normal pace — a 500-video channel simply takes a few days to work through, at a
load the workers can sustain.

The CLI equivalent:

```bash
./manage.py sync_youtube --group lekcje --all
```

Watch it drain with `./manage.py download_queue`. To speed up a big backfill, raise the
batch size for a while:

```bash
STASHCAST_DOWNLOAD_QUEUE_BATCH=5   # ~1440 items/day instead of ~288
```

Re-running is safe: a video already present in the group is skipped, so a full scan only
adds what is missing.

## Publication dates

Feeds date each episode by **when it was published on the source platform**, not by when
it was downloaded — so a back-catalogue fetched today still appears in its original
chronological order in your podcast client.

The date is read from the source metadata (`timestamp`, falling back to
`release_timestamp`, `upload_date`, `release_date`) during prefetch and stored in the
item's `publish_date`. It is filled in for every download route: manual stashes, the
periodic channel sync, full-channel scans and batch downloads.

Feeds sort by `publish_date` first and fall back to the download date for items that have
none. That fallback is also why undated items are sorted **last** rather than first — the
ordering pins `nulls_last` explicitly, because databases disagree about where NULLs
belong in a descending sort.

### Filling in dates on older items

Items downloaded before dates were captured have an empty `publish_date`, so they are
still dated by their download time. To fix them without re-downloading anything, select
them in admin → Items and run:

**Fetch publication date from source**

Or from the CLI:

```bash
./manage.py backfill_publish_dates              # every item missing a date
./manage.py backfill_publish_dates -n 20        # only the 20 most recent
./manage.py backfill_publish_dates --dry-run    # show what would change
./manage.py backfill_publish_dates --all        # re-read dates for every item
```

This only reads metadata — no media is downloaded. Items whose source is gone (deleted or
private videos) are counted as skipped and leave the rest of the run untouched.

## Why a download failed, and what the queue does about it

Failures are sorted into categories, because retrying is not always useful:

| Category | Example message | What happens |
|---|---|---|
| **permanent** | `Join this channel to get access to members-only content` | **No retry at all.** Straight to ERROR - it cannot succeed without an account. Also: private, removed, region-blocked. |
| **scheduled** | `Premieres in 3 hours` | Retried **after it airs**. The wait is parsed from the message (+10 min margin), otherwise `STASHCAST_DOWNLOAD_RETRY_SCHEDULED_MINUTES`. Gets its own larger attempt budget. |
| **blocked** | `Sign in to confirm you're not a bot`, `HTTP 429` | Long rest (`STASHCAST_DOWNLOAD_RETRY_BLOCKED_MINUTES`, default 2 h) and a hint pointing at cookies. |
| **transient** | `HTTP Error 403: Forbidden`, network blips | The normal exponential backoff: 5, 10, 20 min, capped at 6 h. |

The category is decided from the error text (YouTube's own wording, passed through by
yt-dlp), so an unrecognised message simply falls back to *transient* - the old
behaviour. The stored error message says which category was chosen and what to do:

```
… Join this channel to get access to members-only content …
(This video cannot be downloaded with the current configuration
 (members-only, private, removed or region-blocked). Not retrying.)
```

A **blocked** item is the one worth acting on: it means YouTube does not trust the IP.
Set `STASHCAST_YTDLP_COOKIES_FILE` (see [YOUTUBE_AUTH.md](YOUTUBE_AUTH.md)), then
requeue the affected items with `./manage.py download_queue --retry-errors`.

## Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `STASHCAST_DOWNLOAD_QUEUE_MINUTES` | `5` | How often the queue releases work (clamped to 1–59) |
| `STASHCAST_DOWNLOAD_QUEUE_BATCH` | `1` | How many items per release |
| `STASHCAST_DOWNLOAD_MAX_ATTEMPTS` | `3` | Attempts before an item becomes `ERROR` (`1` disables retrying) |
| `STASHCAST_STUCK_TIMEOUT_MINUTES` | `30` | Time **without progress** after which an in-progress item counts as abandoned |
| `STASHCAST_WORKER_HEARTBEAT_STALE_SECONDS` | `180` | Heartbeat age at which the worker is reported down |
| `STASHCAST_WORKER_COUNT` | `2` | How many tasks the worker may run at the same time |
| `STASHCAST_YOUTUBE_SYNC_HOURS` | `3` | How often channels are checked for new uploads |
| `STASHCAST_YOUTUBE_SYNC_MAX_VIDEOS` | `5` | How many recent uploads each check considers |

To drain a large backlog over a weekend and then settle down, raise the batch size
temporarily:

```bash
STASHCAST_DOWNLOAD_QUEUE_BATCH=5   # ~1440 items/day
```

## Finding incomplete downloads

An item can finish as READY with a log full of success and still hold a file that is
shorter than the episode. Nothing in the pipeline notices, because yt-dlp reported
success - only playback does.

Every item stores the duration its source reported (`duration_seconds`). This measures
how long the file on disk actually plays and records it, so incomplete downloads can be
found in bulk:

```bash
./manage.py check_durations                  # measure everything, list the bad ones
./manage.py check_durations --only-unchecked # skip items already measured
./manage.py check_durations --tolerance 5    # allow a 5 second gap
./manage.py check_durations -n 50            # only the 50 most recent
./manage.py check_durations --requeue        # also queue the bad ones for re-download
```

```
1 incomplete file(s):
  expected 1282s, got 623s (short by 659s) - Przestań być łatwym celem…
```

A gap of a second or two is normal - containers round and encoders pad - so the default
tolerance is `STASHCAST_DURATION_TOLERANCE_SECONDS` (3 s).

In the admin, the item list has a **Duration** column (`ok`, `short 659s`, or `—` when
not measured yet) and a **Duration** filter with *Incomplete*, *Complete* and *Not
checked yet*. The usual workflow is: run the command once, filter to *Incomplete*,
select all, and apply **Requeue selected items**. The **Check file duration against
source** action measures a hand-picked selection instead.

Items that have never been measured show as *Not checked yet* and never as *Complete*,
so an unmeasured file can't be mistaken for a verified one.

## Long downloads

A running download reports progress back to the database roughly once a minute, so
`STASHCAST_STUCK_TIMEOUT_MINUTES` means "no progress for N minutes", not "running for N
minutes". A four-hour download is therefore never mistaken for an abandoned one.

This matters because the recovery pass used to requeue any download that simply took
longer than the timeout. The queue then released it a second time while the first was
still running, both runs shared the same `tmp-<guid>` directory, and the second deleted
the first's half-written file. The download reported success but the media file was
truncated - and only a manual immediate re-fetch produced a correct one. Slow settings
such as `STASHCAST_YTDLP_SLEEP_INTERVAL` made it far easier to hit.

The progress reports only cover the transfer itself. What comes after it - yt-dlp's
audio conversion, merging and metadata steps, which each rewrite the whole file, the
video repack, and moving the files into place - reported nothing. On a long episode and
a slow CPU (an ARM box, say) those steps outlast the timeout and the same double run
happened anyway. It showed up as `Conversion failed!`, `unable to obtain file audio
codec with ffprobe`, and files that play shorter than the source (the **Duration**
filter). Two things now prevent it:

- **The whole task keeps the item alive.** A background thread touches the item once a
  minute from the first step to the last. The thread dies with the worker, so a crashed
  worker still goes quiet and is recovered as before.
- **One task per item.** Each run takes a lock on the item
  (`STASHCAST_DATA_DIR/locks/<guid>.lock`, via `flock`). A second run that starts anyway
  sees the lock and leaves the item alone. The operating system releases the lock when a
  worker dies, so a crash can never leave an item locked.

To check whether an older download was hit, count the task starts in its log. Two means
it ran twice:

```bash
grep -c "=== TASK STARTED ===" data_docker/media/*/download.log | grep -v ":1$"
```

(Items downloaded in batch mode write `TASK STARTED (BATCH MODE)` instead and show up
as `:0`.) Re-fetch the items it lists.

For the same reason the admin's **Requeue selected items** action skips items that are
downloading right now and says so; genuinely stalled ones are still requeued.

Audio downloads prefer YouTube's AAC stream (`bestaudio[ext=m4a]`), which needs no
conversion to m4a at all - a plain stream copy. Opus is used only when there is no AAC.

## Worker liveness

`process_media` used to fail any item that had been in `PREFETCHING` for more than 30
seconds, on the assumption that the worker was down. Because the item is saved before
being enqueued, that check also fired on a **healthy but busy** queue — which is why
adding a channel produced a burst of "Huey worker may not be running" errors on
downloads that were merely waiting their turn.

Liveness is reported by a heartbeat file (`<STASHCAST_DATA_DIR>/worker-heartbeat`),
refreshed by **a thread of its own** in the worker process, plus a backstop on every task
the worker starts, finishes or fails.

It cannot be a queued task. A heartbeat task queues behind everything else, so it is not
reached exactly when it matters most: on a saturated worker 116 of them piled up unrun
while the file went stale, the application declared a working worker dead, and every new
download failed on sight with *"Worker unavailable ..."*. Task signals are not enough
either — they only fire at task boundaries, so a worker whose every thread sits inside
one long transcription says nothing for tens of minutes.

A thread answers the question actually being asked: *is this process running?*

The status stream also requires the item to have genuinely waited longer than
`STASHCAST_WORKER_HEARTBEAT_STALE_SECONDS` before reporting the worker as down. A
freshly created item has waited zero seconds and says nothing about the worker's health.

Items abandoned by a worker that really did die are picked up by the recovery pass
instead.

If the queue is not moving, check the worker first:

```bash
./manage.py download_queue   # look at the Worker line
python manage.py run_huey    # start it if it is down
```

A worker can also be alive and still move nothing, because every thread is busy with
something long. Transcribing an hour-long episode holds a thread for as long as it
takes, so with the default two threads a pair of transcriptions leaves nothing to
download with. `STASHCAST_WORKER_COUNT` raises the ceiling — bearing in mind that each
extra thread is another concurrent request to the speech-to-text server.

Downloads are enqueued at a higher priority than transcription, so a queue full of
background work does not decide when the next download happens. Priority orders the
queue; it does not interrupt a task already running, so a transcription that has started
keeps its thread until it finishes.

A restart leaves no transcription running. Anything still marked *Transcribing* when the
worker boots is released as failed, so it can be queued again rather than sitting in a
state nobody is working on.
