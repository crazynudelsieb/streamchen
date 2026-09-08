# Collaborative Radio
### Design Document (MVP)

Version: 0.1  
Status: Draft  
Principles: KISS, DRY, Stateless Where Possible, Stream-First, Container-First

---

# 1. Vision

A private, shareable online radio.

One user creates a room and shares a link.

Everyone joining the room:

- listens to the same audio stream
- can submit songs
- can vote songs up/down
- sees the current queue in real time

No accounts required.

No Spotify dependency.

Self-hostable via Docker.

---

# 2. Goals

## Functional Goals

- Shared live audio stream
- Collaborative queue
- Voting system
- Anonymous participation
- Host moderation controls
- Mobile-friendly web UI
- Minimal setup

## Non-Goals

- Music library management
- User profiles
- Social network features
- Multi-room synchronization
- Distributed streaming

---

# 3. Design Principles

## KISS

Prefer boring and battle-tested technologies.

Examples:

- Icecast over custom streaming
- Postgres over exotic databases
- Docker Compose over Kubernetes

---

## DRY

Single source of truth.

Examples:

- Current queue only stored in database
- Playback state only maintained by Playback Service
- Room permissions only maintained by API

---

## Stateless Frontend

Frontend never owns state.

It only renders:

- queue
- room data
- playback status

received from API/WebSocket.

---

## Stream First

The stream is the product.

Everything else exists to support playback.

---

## Failure Friendly

Any individual service may restart without losing room state.

---

# 4. System Architecture

```text
                Browser Clients
                        |
                        v
               +----------------+
               | Frontend       |
               +----------------+
                        |
                        v
               +----------------+
               | API Service    |
               +--------+-------+
                        |
        +---------------+---------------+
        |                               |
        v                               v
   PostgreSQL                        Redis

                        |
                        v
               +----------------+
               | Playback       |
               | Worker         |
               +--------+-------+
                        |
                        v
               +----------------+
               | Icecast        |
               +----------------+
                        |
                        v
                 Audio Stream
```

---

# 5. Components

## Frontend

Technology:

- React
- Vite
- Typescript

Responsibilities:

- Room UI
- Queue UI
- Voting
- Audio Player
- Realtime updates

No business logic.

---

## API Service

Technology:

- FastAPI

Responsibilities:

- Room management
- Queue management
- Voting
- Moderation
- WebSocket gateway

API owns all business logic.

---

## PostgreSQL

Persistent storage.

Stores:

- rooms
- queue entries
- listener sessions
- votes
- bans

Nothing audio-related stored.

---

## Redis

Temporary state.

Stores:

- websocket events
- playback events
- rate limit counters
- presence tracking

Redis may be cleared at any time.

---

## Playback Worker

Responsibilities:

- Resolve YouTube URLs
- Obtain audio stream URLs
- Feed ffmpeg
- Feed Icecast
- Track playback position

Exactly one active worker per room.

---

## Icecast

Responsibilities:

- Audio delivery only

No application logic.

---

# 6. Rooms

## Create Room

Host receives:

```text
/r/X7mT8fQ29A
```

Room token generated using:

```python
secrets.token_urlsafe(24)
```

---

## Room Settings

Configurable:

- room name
- queue limits
- voting enabled
- fallback playlist
- queue lock
- maximum listeners

---

# 7. Permissions

## Host

Can:

- skip songs
- remove songs
- reorder queue
- ban listeners
- lock room

---

## Listener

Can:

- vote
- add songs
- view queue

---

# 8. Queue Design

## Rule 1

No user may dominate the queue.

---

## Rule 2

Fairness over popularity.

---

## Queue Entry

```json
{
  "id": "123",
  "youtube_id": "abc123",
  "title": "Track",
  "added_by": "guest-id",
  "score": 7
}
```

---

## Fair Scheduling

Round-robin scheduling.

Example:

```text
Alice -> Song A1
Bob   -> Song B1
Alice -> Song A2
Carl  -> Song C1
```

instead of:

```text
Alice -> 20 songs
```

---

## Duplicate Prevention

Reject:

- identical YouTube ID already queued

Optional:

- fuzzy title matching

---

# 9. Voting

## Upvote

```text
+1
```

## Downvote

```text
-1
```

---

## Score

```text
score = upvotes - downvotes
```

Higher score pushes track upward.

---

## Limits

One vote per listener.

Changes overwrite previous vote.

---

# 10. Spam Protection

## Rate Limiting

Add song:

```text
1 request / 15 seconds
```

Vote:

```text
5 requests / 10 seconds
```

---

## Queue Limits

Max pending songs per listener:

```text
3
```

---

## Temporary Shadow Ban

Triggered automatically for:

- repeated spam
- request flooding

Duration:

```text
15 minutes
```

---

# 11. Playback Pipeline

## Song Submission

```text
YouTube URL
    |
    v
Metadata Extraction
    |
    v
Queue
```

---

## Playback

```text
Queue
  |
  v
yt-dlp
  |
  v
ffmpeg
  |
  v
Icecast
```

---

## Preloading

Current song:

```text
Playing
```

Next song:

```text
Prepared
```

Worker always prepares:

```text
current + next
```

Result:

- nearly gapless playback
- fast transitions

---

# 12. Cache Strategy

Critical requirement:

No long-term media storage.

---

## Design Goal

Never retain copyrighted content.

Cache only what improves latency.

---

## Audio Cache

Allowed:

```text
Current track
Next track
```

Only in temporary storage.

---

## TTL

```text
Current track:
until playback completed

Next track:
maximum 10 minutes
```

Automatic deletion immediately after usage.

---

## Disk Budget

Configurable.

Default:

```text
1 GB
```

Hard limit enforced.

---

## Metadata Cache

Cache:

- title
- duration
- thumbnail
- channel

TTL:

```text
24 hours
```

Stored in Redis.

---

## Stream URL Cache

Cached only because YouTube extraction is expensive.

TTL:

```text
5 minutes
```

Stored in Redis.

---

## Browser Cache

API Responses:

```http
Cache-Control: no-store
```

Room State:

```http
Cache-Control: no-store
```

Queue State:

```http
Cache-Control: no-store
```

---

## Static Assets

Frontend assets:

```http
Cache-Control:
public,max-age=31536000,immutable
```

Versioned filenames.

Example:

```text
app.7af2c1.js
```

---

## Audio Stream

```http
Cache-Control:
no-store,no-cache,must-revalidate
```

Ensures listeners stay live.

---

# 13. Realtime Updates

Protocol:

```text
WebSocket
```

Events:

```text
QUEUE_CHANGED
SONG_ADDED
SONG_REMOVED
SONG_STARTED
SONG_SKIPPED
VOTE_CHANGED
LISTENER_JOINED
LISTENER_LEFT
```

---

# 14. Security

## No Accounts

Anonymous guest sessions.

---

## Session ID

Stored as:

```text
Secure Cookie
```

Random UUID.

---

## CSRF

Enabled.

---

## HTTPS

Mandatory.

---

## Admin Secrets

Never exposed to frontend.

Stored as:

```text
Argon2 hashes
```

---

# 15. Docker Deployment

```yaml
services:
  frontend:
  api:
  worker:
  postgres:
  redis:
  icecast:
```

Persistent volumes:

```text
postgres
```

Only.

---

## No Persistent Audio Storage

Explicit requirement:

```text
No media files are retained after playback.
```

---

# 16. Observability

Metrics:

- active listeners
- room count
- queue size
- playback errors
- stream bitrate
- latency

Stack:

```text
Prometheus
Grafana
```

Optional.

---

# 17. MVP Scope

Must Have:

- room creation
- shared stream
- queue
- voting
- host controls
- rate limiting
- Docker deployment

Nice To Have:

- fallback playlists
- room themes
- moderation dashboard
- mobile PWA

Not Needed:

- accounts
- friends
- playlists
- recommendations
- AI features

---

# 18. Success Criteria

A user can:

1. Start a room in under 10 seconds.
2. Share a single URL.
3. Have 10-100 listeners join.
4. Allow listeners to add songs safely.
5. Deliver continuous synchronized audio.
6. Retain zero media files after playback.
7. Run the entire system with Docker Compose on one VPS.
