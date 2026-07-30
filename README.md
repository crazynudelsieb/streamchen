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
  link; the room hears it when its turn comes. Search goes through YouTube
  Music, so the results are songs.
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
- **Host controls.** Skip, reorder, lock the queue, remove listeners.
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
| Redis | — | Events, presence, rate limits, short-lived caches. Disposable. |
| Worker | yt-dlp + ffmpeg | Playback. One active worker per room. |
| Icecast | — | Audio delivery. Nothing else. |

**No build step and no Node.** Pages are rendered server-side; Bootstrap,
Bootstrap Icons and Inter are vendored under [`app/static/vendor/`](app/static/vendor/)
(~950K, same versions as konsumchen), and the client behaviour is one
dependency-free [`app.js`](app/static/app.js). Nothing is fetched from a CDN,
so the app works under a strict CSP and leaks nothing to third parties.

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

### How the page stays live

The WebSocket carries notifications, never data. When one arrives, the browser
refetches `/r/<token>/live` — a rendered fragment containing the player, the
queue and the history — and swaps the three regions into place. A missed
event, a dropped socket or a flushed Redis all heal on the next update, and
there is exactly one implementation of "what the queue looks like".

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
| [`app/worker/`](app/worker/) | Playback pipeline and the audio cache. |
| [`app/templates/`](app/templates/) | Jinja templates; `_*.html` are fragments. |
| [`app/static/app.js`](app/static/app.js) | All client behaviour. |

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
| `SHADOW_BAN_MINUTES` | `15` | How long a flooder is silently muted. |
| `AUDIO_CACHE_BUDGET_BYTES` | `1 GiB` | Hard cap on the temporary cache. |
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
- Idle rooms and everything in them are deleted after `ROOM_IDLE_DAYS`.
- No CDN, no analytics, no third-party requests of any kind from the browser.

---

## Legal

streamchen is not affiliated with YouTube. It plays what its users link to; the
operator of an instance is responsible for how it is used.

Licensed under the **PolyForm Noncommercial License 1.0.0** — see
[LICENSE](LICENSE). Personal and private use is free; commercial use requires a
separate licence from the author (`appchen@outlook.at`).
