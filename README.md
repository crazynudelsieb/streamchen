# streamchen

**Collaborative radio.** One person creates a room and shares a link. Everyone
who opens it hears the same live stream at the same moment, adds songs, and
votes on what plays next. No accounts, no Spotify, no media kept after
playback.

Part of the **appchen** family, alongside
[splittchen](https://github.com/crazynudelsieb/splittchen) and
[konsumchen](https://github.com/crazynudelsieb/konsumchen).

---

## What it does

- **A shared stream, not synchronised players.** Everyone is listening to one
  Icecast mount, so nobody drifts out of sync.
- **A queue anyone can add to.** Search for a song by name or paste a YouTube
  link; the room hears it when its turn comes. Search asks for a page of
  candidates and ranks them, so "artist - song" returns *that recording* rather
  than a live cut and the rest of the artist's catalogue.
- **A radio that keeps going.** When the queue runs dry the room keeps playing:
  from the host's fallback playlist if they set one, otherwise from YouTube's
  mix for whatever played last. Only while somebody is listening, and always
  behind any real request.
- **Fair scheduling.** Round-robin across submitters: everyone's first song
  plays before anyone's second, however many they queue.
- **A name if you want one.** Everyone is given a generated nickname on
  arrival, and anyone can change theirs — or ask for another one. Still no
  account: the name lives on that room only.
- **Voting.** Up and down votes decide which of *your* songs plays in your
  turn — they cannot buy you extra turns.
- **A stream that follows the room.** It starts when somebody arrives and stops
  when the last listener leaves, so nothing is ever encoded for nobody. A host
  can also stop it by hand, which overrules that until they start it again —
  and stopping the stream never touches the room, the queue or the link.
- **News on the hour, if the host wants it.** One short bulletin from a podcast
  feed — Austrian ORF Ö1 Journale out of the box — played *after* a song and
  never over one, then straight back to the queue. Off until a host turns it on.
  Always the station's newest short edition, so it repeats through the day the
  way radio news does, and stops entirely once that edition is too old to be
  news.
- **Chat.** A line beside the player, delivered on the socket the page already
  holds open. Kept in memory, never in the database, gone with the room.
- **A cat.** Everyone gets one, drawn from their session on this server. No
  avatar service, so nobody is told who is in your room.
- **Host controls.** Skip, reorder, lock the queue, rename the room (the link
  never changes), stop the stream, turn chat off, remove listeners.
- **Installable.** A real PWA: manifest, icons, offline page, lock-screen
  controls through the Media Session API, and an install button that appears
  only where the browser can actually install it (with instructions where it
  cannot, i.e. Safari).
- **Spam protection.** Rate limits, per-listener queue caps, and automatic,
  silent shadow bans for flooders.
- **Nothing retained.** Audio is fetched, streamed and deleted. The only
  persistent volume in the deployment is Postgres.

---

## Architecture

```text
                Browser
                   |
              Traefik (TLS)
             /             \
     /stream |               | everything else
            v               v
     +------------+   +------------------+
     | Icecast    |   | streamchen app   |
     | audio only |   | pages + API + WS |
     +-----+------+   +--------+---------+
           ^                   |
           |            Postgres   Redis
           |                   |
           |          +--------+---------+
           +----------+ Playback worker  |
   one source per room | yt-dlp -> ffmpeg|
                       +------------------+
```

| Component | Technology | Owns |
| --- | --- | --- |
| App | FastAPI + Jinja2 | Rooms, queue, voting, moderation, WebSocket, rendered pages. |
| Postgres | — | Rooms, tracks, listeners, votes, bans. |
| Redis | — | Events, presence, chat, rate limits, short-lived caches. Disposable. |
| Worker | yt-dlp + ffmpeg | Playback. One active worker per room. |
| Icecast | — | Audio delivery. Nothing else. |

**No build step and no Node.** Pages are rendered server-side; Bootstrap,
Bootstrap Icons and Inter are vendored under [`app/static/vendor/`](app/static/vendor/)
(~950K, same versions as konsumchen), and the client behaviour is one
dependency-free [`app.js`](app/static/app.js). Nothing is fetched from a CDN,
so the app works under a strict CSP and leaks nothing to third parties. The
PWA icons are raster because an installable app needs them to be; they are
drawn from the same mark by [`tools/make_icons.py`](tools/make_icons.py) with
the standard library and committed, so there is still nothing to build.

The full design document is [`concept.md`](concept.md); the code follows its
section numbering in comments where a decision traces back to it. One
deliberate deviation: §5 proposes a React/Vite/TypeScript SPA, but since §3
requires a frontend that owns no state, server-rendered HTML delivers the same
thing without a build toolchain.

### How the stream stays continuous

Each room owns one long-lived ffmpeg process connected to Icecast, fed raw PCM
on stdin. Tracks are decoded into that pipe one after another, and when the
queue is empty the worker writes silence at the same rate. The encoder reads
with `-re`, so it consumes at exactly real time and everything upstream is
paced by back-pressure. Listeners are never disconnected between songs.

That pacing is also why transitions have to be prepared rather than performed:
because the encoder consumes at real time, anything the worker does *between*
two tracks — a database write, a download, starting a decoder — is a hole in
the broadcast of exactly that length. So none of it happens there. While a
track plays, a lookahead works out what is actually next, fetches it, and
starts its decoder shortly before the handover, leaving the boundary itself
with nothing to do but swap pipes. If the queue runs dry the radio is asked for
a pick early too, so its choice is downloaded rather than discovered at the
moment of silence.

Everything the lookahead does is a guess about a queue that votes and host
promotions can reorder underneath it. The guess is checked against what the
worker actually claims, so being wrong costs a cold start and can never play
the wrong song.

### How the news gets on air without interrupting anything

A bulletin is audio the room did not queue, so it is the one thing that could
plausibly cut a song off — and it never does. Two decisions make that structural
rather than careful ([`app/news.py`](app/news.py)): the clip is *fetched* while
something else is on air, by the same lookahead that prepares the next track,
and it is *played* only from the top of a playback tick, which is a place the
loop can reach only once the previous track has finished. So the worst case is a
bulletin that waits out a long song, never one that lands in the middle of it.

The rest is restraint. Only editions short enough to be a bulletin qualify, and
one that will not say how long it is never plays. What plays is always the
*newest* of those — heard again on the next hour if the station has not published
since, which is what radio news is, rather than reaching further down the feed
for something unheard and calling yesterday's bulletin today's. Once the newest
edition is older than `NEWS_MAX_AGE_H`, nothing plays at all. Nothing is fetched
for a room with nobody in it. And the feed URL belongs to the operator, not the
room: a host chooses whether their room has news, never where the server fetches
from.

### How the page stays live

The WebSocket carries notifications, never data. When one arrives, the browser
refetches `/r/<token>/live` — a rendered fragment containing the player, the
queue and the history — and swaps the three regions into place. A missed
event, a dropped socket or a flushed Redis all heal on the next update, and
there is exactly one implementation of "what the queue looks like".

Chat is the one exception, and it earns it: a message travels whole and is
appended in place. Refetching the room because somebody typed "lol" would cost
a database round trip and three DOM swaps per word, and unlike queue state a
chat line has no authoritative version to disagree with. A client that missed
one is missing a line, not showing a lie, and reconnecting refills from the
history endpoint.

### How search decides

A flat video search returns twenty-five candidates with their titles,
durations, channels and view counts in the listing itself — one request, no
per-hit extraction — and the YouTube Music catalogue is asked at the same time
for nothing but its ids, which is the evidence that a result is a song rather
than a lecture. Everything is then scored ([`app/youtube.py`](app/youtube.py)):
relevance is what a result *is*, and everything else is a tiebreak. A hit
missing half the words of the query cannot climb back on view count; a
different recording of the right song sits below the right one and stays on the
page; the artist's own channel, an "(Official Video)" and the catalogue all
count for something. The properties that matter are pinned in
[`tests/test_search_ranking.py`](tests/test_search_ranking.py).

---

## Quick start

```bash
cp .env.example .env
docker compose -f docker-compose.local.yml up --build
```

Open <http://localhost:8080>, create a room, search for a song, press play.

### Production

```bash
cp .env.example .env      # fill in PUBLIC_HOST, BASE_URL, passwords
docker compose up -d
```

Images are published to GHCR (`streamchen`, `streamchen-worker`,
`streamchen-icecast`) for `linux/amd64` and `linux/arm64`. The compose file
expects an external `traefik` network, which routes `/stream` to Icecast and
everything else to the app.

---

## Development

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1      # bash: source .venv/bin/activate
pip install -r requirements-dev.txt

ruff check .
pytest -q
```

The test suite needs no services: SQLite stands in for Postgres and fakeredis
for the bus.

Running against real services:

```bash
uvicorn app.api.main:app --reload      # pages, API and WebSocket on :8000
python -m app.worker                   # playback
```

Both read the same `.env`. `DATABASE_URL`, `REDIS_URL` and `BASE_URL` are the
only required variables.

### Where things live

| Path | What |
| --- | --- |
| [`app/web.py`](app/web.py) | Rendered pages and the live fragment. |
| [`app/api/routers/`](app/api/routers/) | JSON API and the WebSocket. |
| [`app/service.py`](app/service.py) | Business logic shared by both. |
| [`app/scheduling.py`](app/scheduling.py) | Fair queue ordering (pure functions). |
| [`app/chat.py`](app/chat.py) | Room chat: a capped Redis list, nothing else. |
| [`app/news.py`](app/news.py) | The hourly bulletin: feed, cadence, no repeats. |
| [`app/avatars.py`](app/avatars.py) | The cats. |
| [`app/worker/`](app/worker/) | Playback pipeline and the audio cache. |
| [`app/templates/`](app/templates/) | Jinja templates; `_*.html` are fragments. |
| [`app/static/app.js`](app/static/app.js) | All client behaviour. |
| [`app/static/sw.js`](app/static/sw.js) | Service worker; caches the shell and nothing live. |

---

## Configuration

Everything is environment variables; see [`.env.example`](.env.example) for the
annotated list. The ones worth knowing:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | — | Postgres connection string (required). |
| `REDIS_URL` | — | Redis connection string (required). |
| `BASE_URL` | — | Public URL; room links are built from it (required). |
| `ICECAST_PUBLIC_URL` | — | Where browsers fetch the stream. |
| `MAX_PENDING_PER_LISTENER` | `3` | Songs one listener may have waiting. |
| `MAX_TRACK_DURATION_S` | `900` | Longest track accepted. |
| `ADD_RATE_LIMIT` / `ADD_RATE_WINDOW_S` | `1` / `15` | Song submissions. |
| `VOTE_RATE_LIMIT` / `VOTE_RATE_WINDOW_S` | `5` / `10` | Votes. |
| `RENAME_RATE_LIMIT` / `RENAME_RATE_WINDOW_S` | `6` / `60` | Name changes. |
| `CHAT_RATE_LIMIT` / `CHAT_RATE_WINDOW_S` | `6` / `10` | Chat messages. |
| `SHADOW_BAN_MINUTES` | `15` | How long a flooder is silently muted. |
| `AUDIO_CACHE_BUDGET_BYTES` | `1 GiB` | Hard cap on the temporary cache. |
| `NEWS_FEED_URL` | ORF Ö1 Journale | Feed the hourly bulletin comes from; blank switches news off everywhere. |
| `NEWS_MAX_DURATION_S` | `660` | Longest edition that counts as a bulletin. |
| `NEWS_MAX_AGE_H` | `24` | Past this, the newest edition is no longer news and none plays. |
| `NEWS_INTERVAL_MIN` | `60` | Default cadence for a room that turns news on. |
| `ICECAST_BURST_SIZE` | `65536` | Sent on connect: trades start-up delay against how far behind live a listener begins. |
| `IMPRINT_NAME` | — | Set it and `/imprint` appears in the footer. |

---

## Privacy and retention

- No accounts. Identity is a random session id in a cookie.
- Host rights are proven once with a key and then ride on that session. The
  server stores **only** an Argon2 hash of the key, so it can never be shown
  again — it is displayed once at creation for use on another device.
- Audio files exist in a RAM-backed temporary cache for the current and next
  track, capped, swept, and deleted the moment a track finishes.
- Pages and API responses are `no-store`; the audio stream is
  `no-store, no-cache, must-revalidate`; only versioned static assets are
  cached, and those are immutable.
- Chat lives in a capped Redis list and never reaches Postgres. Avatars are
  generated from a hash of the listener's id, on this server.
- Idle rooms and everything in them are deleted after `ROOM_IDLE_DAYS`.
- No CDN, no analytics, no third-party requests of any kind from the browser.
  The service worker caches the shell and the icons, and is forbidden from
  touching the API, the room page or the stream.

---

## Legal

streamchen is not affiliated with YouTube. It plays what its users link to; the
operator of an instance is responsible for how it is used.

Licensed under the **PolyForm Noncommercial License 1.0.0** — see
[LICENSE](LICENSE). Personal and private use is free; commercial use requires a
separate licence from the author (`appchen@outlook.at`).
