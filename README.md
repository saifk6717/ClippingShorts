# ClippingShorts

Self-hosted, multi-stream AI clipping. Watch 2–10 Twitch or YouTube Live streams at once, detect the exciting
moments automatically, keep only those moments as short source clips, edit them in the browser, render vertical
Shorts and publish them to YouTube.

- **Next.js dashboard**: add streams, watch their live status, browse highlights, edit clips, follow jobs.
- **Python backend**: FastAPI plus three workers (stream ingestion, AI, rendering), all bounded so a small server
  cannot run out of memory, CPU or disk.
- **Your own keys**: Google Cloud (Gemini + Speech-to-Text) and your own YouTube channel.

> **Responsible use.** ClippingShorts records nothing but the short windows around highlights, but those clips are
> still other people's content. Only clip and publish streams you own or have permission to use, and follow the
> terms of Twitch, YouTube and the creators you clip. Re-uploading someone else's stream can get your channel
> copyright strikes; the software cannot decide that for you.

---

## How it works

```
 Twitch / YouTube Live ──► stream-worker (one OS process per stream)
                            ├─ FFmpeg: copy stream into 4 s segments  ──► rolling buffer on disk (last 180 s only)
                            │          + 16 kHz mono audio on a pipe
                            ├─ audio analyzer (RMS, dB, rolling baseline, relative jumps, peaks, voice activity)
                            ├─ chat analyzer (messages/s, unique people, emotes, repeats, reaction words, spam)
                            └─ local detector ──► candidate: pin the buffer range, wait for post-event footage
                                     │
            Redis queues (bounded, retries, dead-letter) ◄─┘
                                     │
 ai-worker ─ transcription (Chirp 3, ~10 s chunks with word timestamps)
           ─ two independent Gemini judges: transcript-only and reaction-signals-only
           ─ merge / de-duplicate / hourly cap ──► accepted ──► stream-worker extracts a 2–5 min source clip
           ─ YouTube publishing (resumable upload)
 renderer  ─ FFmpeg: trim, 9:16 / 16:9 / 1:1, smart crop or blurred fit, burned-in captions + hook, loudness
 backend   ─ REST API + Server-Sent Events, resource sampler, admission controller
 frontend  ─ Next.js dashboard (the only public port), proxies /api to the backend
```

**What is kept.** The rolling buffer is temporary: segments older than `ROLLING_BUFFER_SECONDS` are deleted as the
stream runs. A source clip (default 120 s before and 40 s after the moment) is saved only for an accepted
highlight. Source clips are never modified: each edit is rendered as a new version next to it. Source clips expire
after `SOURCE_CLIP_RETENTION_DAYS` unless marked "keep".

**Where state lives.** Videos: the data volume. Metadata (streams, highlights, clips, versions, uploads, AI usage):
SQLite on the data volume by default, or PostgreSQL via `DATABASE_URL`. Live state, queues, locks: Redis (small JSON
only, every key prefixed and expiring). No video or audio ever goes into Redis.

### Repository layout

```
frontend/            Next.js 15 (App Router, TypeScript, Tailwind, shadcn/ui, React Query, SSE)
backend/             FastAPI + the shared Python package `clippingshorts`
  clippingshorts/    stream_manager, twitch_connector, youtube_connector, video_ingestion, rolling_buffer,
                     audio_analyzer, chat_analyzer, transcription, highlight_detector, gemini_judge,
                     clip_extractor, clip_renderer, clip_storage, youtube_uploader, resource_manager, monitoring
  tests/             pytest suite (runs against real FFmpeg and Redis)
workers/             Dockerfiles for stream / ai / renderer (built on the backend image)
scripts/             load test, token check
docker-compose.yml
.env.example
```

---

## Setup, step by step (Windows, macOS, Linux)

Everything runs in Docker, so the steps are the same on every system; only installing Docker differs. You need
about 4 GB of free RAM (6 GB or more for many streams), 2+ CPU cores and 20 GB of free disk.

### Step 1: Install Docker and Git

**Windows 10/11**
1. Install **Docker Desktop** from https://www.docker.com/products/docker-desktop/. Keep "Use WSL 2" selected and
   restart when asked.
2. Open Docker Desktop once and wait until it says *Engine running*. Under *Settings → Resources*, give it at least
   4 GB of memory (6–8 GB is better).
3. Install **Git** from https://git-scm.com/download/win (the defaults are fine).
4. Install **Python 3** from https://www.python.org/downloads/ (tick *Add python.exe to PATH*). It is only needed for
   the one-time YouTube token helper.
5. Use **PowerShell** for the commands below.

**macOS (Apple Silicon or Intel)**
1. Install **Docker Desktop** from https://www.docker.com/products/docker-desktop/, open it, and wait for *Engine
   running*. Under *Settings → Resources*, give it at least 4 GB of memory.
2. Git and Python 3 come with the developer tools: run `xcode-select --install` in Terminal if `git --version`
   fails.

**Linux (Ubuntu / Debian; other distributions are similar)**
```bash
curl -fsSL https://get.docker.com | sudo sh        # Docker Engine + Compose plugin
sudo usermod -aG docker "$USER"                     # then log out and back in
sudo apt-get install -y git python3
```

Check that everything is installed. Compose must be v2.17 or newer:
```bash
docker --version
docker compose version
git --version
```

### Step 2: Download ClippingShorts

Put it in a short folder that stays put: `C:\clippingshorts` on Windows, `~/clippingshorts` on macOS/Linux.

```bash
git clone https://github.com/<your-account>/clippingshorts.git clippingshorts
cd clippingshorts
```

Or, on GitHub, use **Code → Download ZIP**, unzip it into that folder, and open a terminal inside it.

### Step 3: Create your settings file

```bash
cp .env.example .env              # macOS / Linux
Copy-Item .env.example .env       # Windows PowerShell
```

Open `.env` in any text editor. On Windows Notepad works; save it with UTF-8 encoding. Fill in the sign-in values
first:

| Setting | What to put |
|---|---|
| `CLIPPINGSHORTS_SECRET_KEY` | a long random string, see below |
| `CLIPPINGSHORTS_ADMIN_PASSWORD` | the password you will sign in with (10+ characters) |
| `CLIPPINGSHORTS_VIEWER_PASSWORD` | optional read-only password, or leave empty |

To make a random secret key, run one of these:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"                                  # macOS / Linux
py -c "import secrets; print(secrets.token_urlsafe(48))"                                       # Windows
[Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Maximum 256 }))               # Windows, no Python
```

### Step 4: Google Cloud key (Gemini highlight judges + Chirp 3 speech-to-text)

1. Go to https://console.cloud.google.com, create a project (or pick one) and **attach a billing account**. Gemini
   and Speech-to-Text are paid per use.
2. **APIs & Services → Library**: enable **Vertex AI API** and **Cloud Speech-to-Text API**.
3. **IAM & Admin → Service accounts → Create service account**, with any name. Give it the roles **Vertex AI User**
   and **Cloud Speech Client**.
4. Open the service account, go to **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
5. Move that file **outside** the ClippingShorts folder, e.g. `C:\clippingshorts-keys\google.json` or
   `~/clippingshorts-keys/google.json`. Never commit it.
6. In `.env`:
   ```ini
   GOOGLE_SERVICE_ACCOUNT_FILE=/home/you/clippingshorts-keys/google.json   # macOS / Linux
   GOOGLE_SERVICE_ACCOUNT_FILE=C:/clippingshorts-keys/google.json          # Windows: use forward slashes
   GOOGLE_CLOUD_PROJECT=your-project-id
   GEMINI_MODEL=gemini-3.8-flash
   GEMINI_LOCATION=global
   SPEECH_LOCATION=us            # Chirp 3 runs in "us" or "eu"
   SPEECH_LANGUAGE=en-US
   ```
   The file is mounted into the containers as a Docker secret. Model names and regions change over time, so check
   that your project can use `GEMINI_MODEL` in `GEMINI_LOCATION`. ClippingShorts never switches to another model on
   its own. To see cost estimates on the dashboard, set `GEMINI_PRICE_*` and `SPEECH_PRICE_PER_MINUTE` from Google's
   current price list.

Want to try it without Google first? Set `AI_MODE=local` and `TRANSCRIPTION_PROVIDER=none`. Highlights are then
picked from audio and chat signals alone.

### Step 5: YouTube keys (publishing + YouTube Live chat)

Skip this step if you only want to clip and download. You can add it later.

1. In the same Google Cloud console: **APIs & Services → Library → YouTube Data API v3 → Enable**.
2. **APIs & Services → OAuth consent screen**: choose *External*, fill in the app name and your email, and add your
   own Google account under **Test users**.
   - While the app's publishing status is *Testing*, Google expires refresh tokens after about 7 days.
   - For a token that lasts, set the status to *In production*. For your own channel that only shows an
     "unverified app" warning when you sign in.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID → Application type: Desktop app**. Copy
   the **Client ID** and **Client secret**.
4. On your own computer, inside the ClippingShorts folder, run the one-time helper. A browser opens: sign in with
   the account that owns the channel and allow access.
   ```bash
   python3 scripts/get_youtube_token.py "<CLIENT_ID>" "<CLIENT_SECRET>"     # macOS / Linux
   py scripts\get_youtube_token.py "<CLIENT_ID>" "<CLIENT_SECRET>"          # Windows
   ```
   It prints four lines (`YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`, `YOUTUBE_CHANNEL_ID`).
   Paste them into `.env`.
5. Check the token:
   ```bash
   python3 scripts/check_youtube_token.py        # Windows: py scripts\check_youtube_token.py
   ```
   It should list the scopes `youtube.upload` and `youtube.readonly` and your channel's name. The error
   `unauthorized_client` means the refresh token belongs to a different OAuth client than the ID/secret in `.env`.
6. Quota: the YouTube Data API has a daily quota per Cloud project (10,000 units by default). Uploads cost far more
   than chat reads. Check *APIs & Services → YouTube Data API v3 → Quotas*, and keep
   `YOUTUBE_CHAT_DAILY_QUOTA_BUDGET` well below it so uploads still fit.

Keep `AUTO_UPLOAD=off` until you trust the results. Clips then wait for your review in the library.

### Step 6: Redis

Most people on Windows and macOS have no Redis yet. Use the bundled one:
```bash
docker network create clippingshorts_redis
```
and in `.env`:
```ini
COMPOSE_PROFILES=bundled-redis
REDIS_NETWORK=clippingshorts_redis
REDIS_URL=redis://redis:6379/4
```
Already running Redis in Docker? See [Redis](#redis) below to use it instead.

### Step 7: Start it

```bash
docker compose up -d --build
```
The first build downloads and builds everything (5–15 minutes). Then check:
```bash
docker compose ps          # all services should show "healthy" within a minute or two
```
Open **http://localhost:3100** and sign in with your admin password. Change the port with
`CLIPPINGSHORTS_FRONTEND_PORT`.

### Step 8: Try it safely with synthetic streams

Set `CLIPPINGSHORTS_ENABLE_SYNTHETIC=1` in `.env` and run `docker compose up -d`. Then, in **Add streams**, choose
*Synthetic (test)* with a link like `synthetic://test1?bursts=60&chat=3`. The first synthetic stream encodes a
5-minute test video once, which takes a few minutes. After that it plays like a live stream with a loud moment and
a chat explosion every minute. Then add your real Twitch or YouTube Live links.

### Running it on a server

**Reach the dashboard from your computer.** The simplest and safest way is an SSH tunnel; nothing needs to open on
the server's firewall:
```bash
ssh -L 3100:127.0.0.1:3100 you@your-server
```
Then open http://localhost:3100 on your computer while the SSH session is open.

**Put it on the internet.** Use HTTPS behind a reverse proxy and set `CLIPPINGSHORTS_COOKIE_SECURE=1`. For nginx,
turn buffering off so live updates and video previews stream:
```nginx
location / {
    proxy_pass http://127.0.0.1:3100;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```
In that case set `CLIPPINGSHORTS_FRONTEND_BIND=127.0.0.1` so port 3100 is reachable only through the proxy.

### Everyday commands

```bash
docker compose ps                      # status
docker compose logs -f ai-worker       # follow a service's log (backend, stream-worker, ai-worker, renderer, frontend)
docker compose restart stream-worker   # restart one service (running streams resume by themselves)
docker compose down                    # stop everything; clips and settings are kept in the "clippingshorts_data" volume
git pull && docker compose up -d --build   # update
```
`docker compose down -v` also **deletes all saved clips**, so use it only to start from scratch.

### Troubleshooting

| You see | Do this |
|---|---|
| A service is not *healthy* | `docker compose logs <service>`. The backend refuses to start without `CLIPPINGSHORTS_SECRET_KEY` and `CLIPPINGSHORTS_ADMIN_PASSWORD`. |
| `set GOOGLE_SERVICE_ACCOUNT_FILE` | The path in `.env` is empty or wrong. On Windows use forward slashes. |
| `network ... not found` | Create the network from Step 6, or fix `REDIS_NETWORK`. |
| YouTube stream: *bot check* | YouTube refused this machine. Set `YTDLP_COOKIES_FILE` and/or `YTDLP_PROXY` (see [YouTube](#youtube)). |
| Chirp 3 HTTP 403 | Enable *Cloud Speech-to-Text API* and give the service account *Cloud Speech Client*. |
| Gemini HTTP 404 | Your project can't use that model in that location. Check `GEMINI_MODEL` / `GEMINI_LOCATION`. |
| Streams stay *deferred* | The machine is under pressure (CPU/RAM/disk). See the reason on the dashboard; lower `MAX_STREAMS` or add resources. |

## Configuration reference

All settings, with explanations, are in [.env.example](.env.example). The sections below explain the parts that
need a decision.

### Redis

ClippingShorts uses an existing Redis by default. Containers reach it by its **container name on a shared Docker
network**. Inside a container, `localhost:6379` is the container itself, not Redis.

```bash
docker inspect <redis-container> --format '{{json .NetworkSettings.Networks}}'   # find its network and name
```

Set `REDIS_NETWORK` to that network and `REDIS_URL=redis://<container-name>:6379/<db>`. Use a database number
and `REDIS_PREFIX` that no other application uses. ClippingShorts never flushes Redis and never changes its
configuration.

No Redis yet? Run `docker network create clippingshorts_redis`, then set `COMPOSE_PROFILES=bundled-redis`,
`REDIS_NETWORK=clippingshorts_redis` and `REDIS_URL=redis://redis:6379/4` in `.env`.

Redis and the API are never published on a host port; only the dashboard is.

### Google Cloud

- **Gemini.** `GEMINI_MODEL` is used exactly as written, and a different model is never substituted. Check that
  your project can call it in `GEMINI_LOCATION` first (some models are only served from `global`).
- **Speech-to-Text (Chirp 3).** The model `chirp_3` is served from the `us` and `eu` multi-regions
  (`SPEECH_LOCATION`). Chirp 3 returns word-level timestamps only through `Recognize`/`BatchRecognize`, not through
  `StreamingRecognize`, and clipping needs word timing. So live audio is sent in short chunks (`AUDIO_CHUNK_SECONDS`,
  default 10, cut at a quiet moment). Transcripts are therefore a few seconds behind live, and every result is
  final (there are no interim results). After a clip is saved, its own audio is transcribed again so its captions
  line up exactly with the file.
- **Local fallback.** `TRANSCRIPTION_FALLBACK=parakeet` uses NVIDIA Parakeet TDT 0.6B through sherpa-onnx. Build
  with `docker compose build --build-arg INSTALL_PARAKEET=1` and give the AI worker about 1 GB more memory. On a
  small CPU it handles roughly one stream, not ten.

### YouTube

- **Upload.** Resumable upload in 8 MB chunks that resumes after network errors. Private, unlisted or public, or
  scheduled (sent as private with `publishAt`). Each rendered version can be published only once per channel, and
  the YouTube video id is stored.
- **Live chat.** Uses `liveChatMessages.streamList` (a long-lived streaming HTTP connection, 5 quota units per
  connection) and falls back to polling `liveChatMessages.list`. Every call counts against
  `YOUTUBE_CHAT_DAILY_QUOTA_BUDGET`; when the budget is spent, detection continues on audio alone.
- **Video download.** YouTube sometimes refuses downloads from data-centre IPs ("confirm you're not a bot"). The
  stream then shows as *disconnected* with that message. Put a Netscape-format `cookies.txt` on the data volume and
  set `YTDLP_COOKIES_FILE`, and/or set `YTDLP_PROXY`. Twitch has no such check.
- **Check your token.** `docker compose exec backend python /app/scripts/check_youtube_token.py` (or run
  `scripts/check_youtube_token.py` with the same environment) prints the scopes and the channel the token belongs
  to.

Twitch chat is read through Twitch's anonymous read-only IRC gateway, so no Twitch app or account is needed.

---

## Using it

1. **Add streams.** Paste 2–10 links, choose Twitch or YouTube Live for each, and optionally set a creator name and
   per-stream detection settings. Links are checked for the right platform and for duplicates, both in the browser
   and on the server.
2. **Monitoring.** Start/stop each stream or all of them. Each card shows status, monitored time, chat messages/s,
   unique chatters, audio level against baseline, transcription lag, the last spike, highlights, and the worker's
   memory and CPU. *Restart* replaces the worker; *Scissors* clips the last minute by hand.
3. **Highlights library.** Filter by platform, streamer, type, date and state (pending review, rendered, uploaded).
   Select several clips to approve, reject, render a default Short, keep, or delete.
4. **Editor.** The preview streams with HTTP Range requests and is never loaded whole. You get:
   - a timeline with thumbnails and waveform, start/end handles and loop playback;
   - 9:16, 16:9 or 1:1 output, with a crop box you drag on the video, zoom, smart crop or a blurred fit;
   - editable captions in three styles, plus the hook text, title, description and hashtags;
   - volume, loudness normalisation and mute.

   *Export* queues a render. Progress shows live, and every version stays available to watch, download or publish.
5. **Jobs** shows queue lengths against their caps, dead letters (with retry), renders, uploads and every AI
   decision with its scores and reason. **System** shows resources, per-container and per-stream memory, storage per
   folder, and AI/API usage against daily budgets.

### How a highlight is decided

1. **Local detector** (free, every second). It compares loudness with the streamer's own rolling baseline (median
   and spread, so a naturally loud streamer is not "always exciting") and counts unique chatters per second
   against their baseline. Emotes, repeated messages and reaction words add weight. A single user spamming is
   discounted. "Clip it" or your own trigger phrases in the transcript also count. Cooldowns and an hourly cap
   apply.
2. **Pin.** The candidate pins its buffer range, so the footage cannot expire while it is judged. Overlapping
   candidates merge into one window (capped at `SOURCE_CLIP_MAX_SECONDS`).
3. **Two judges.** Both run in parallel on Gemini and never see each other's input:
   - the *transcript judge* reads only the words;
   - the *reaction judge* reads only numbers (loudness and chat people per 2 s).

   A highlight needs both to agree on overlapping times, or one to be very sure while the local signal is strong.
   Gemini is never called per message or per frame. It runs only for candidates and an optional periodic check
   (`AI_EVAL_INTERVAL_SECONDS`).
4. **De-duplication and extraction.** Accepted highlights close to an existing one are marked duplicates. The rest
   are cut from the pinned segments without re-encoding, and the pin is then released.

---

## Resource safety

Docker memory limits are a last resort, not the plan. The app keeps itself well below them:

- **Admission controller.** It samples CPU, RAM, disk and storage every 2 s.
  - At *high* pressure, new streams, renders and periodic AI checks wait; ingestion of running streams, event
    checks and extraction continue.
  - At *critical* pressure, transcription chunks are dropped and only ingestion continues.
  - The dashboard shows the reason.
- **Fixed concurrency.** `MAX_STREAMS`, `MAX_CONCURRENT_TRANSCRIPTIONS`, `MAX_CONCURRENT_AI_JOBS`,
  `MAX_CONCURRENT_RENDER_JOBS`, `MAX_CONCURRENT_EXTRACTIONS`, and `MAX_FFMPEG_PROCESSES` (a cross-container
  semaphore in Redis).
- **Bounded everything.** Queue caps refuse work instead of piling it up (backpressure). Audio and chat history
  live in fixed-size windows, and each stream holds at most 60 s of untranscribed audio. Transcripts are kept in
  Redis only for the buffer window. Pins are limited in number and lifetime.
- **Per-stream isolation.** One OS process per stream, guarded by a Redis lock so there is never a second one. A
  stream worker above `STREAM_WORKER_MEM_LIMIT_MB` (FFmpeg included) is restarted before the kernel's OOM killer
  gets involved.
- **No restart storms.** A crashed stream is restarted with exponential backoff (5 s → 5 min). After 6 crashes in
  30 minutes it is marked *failed* and waits for a manual restart.
- **Priorities.** Renders run at lower CPU priority (`nice`, `cpu_shares`) with capped threads. The renderer has the
  highest OOM score, so it would go before ingestion.
- **Cleanup.** Expired buffers and temp files are removed every few minutes, source clips past retention are
  removed, and `STORAGE_LIMIT_GB` is enforced. Docker logs are size-capped.

The defaults in `.env.example` and `docker-compose.yml` are starting points for a small VM, **not tested
guarantees**. Measure your own machine with the load test before relying on a number of streams.

## Testing

```bash
# unit + integration tests (real FFmpeg, your Redis on a private DB/prefix that is cleaned up afterwards)
docker run --rm --network <redis-network> -e TEST_REDIS_URL=redis://<redis>:6379/15 \
  -v "$PWD/backend/tests:/app/tests:ro" -u root clippingshorts-backend:local \
  sh -c "pip install -q pytest==8.4.2 && python -m pytest -q tests"
```

**Synthetic streams.** Set `CLIPPINGSHORTS_ENABLE_SYNTHETIC=1` and add links like
`synthetic://name?bursts=60&chat=3&spam=1&drop=0`. Each one plays a pre-encoded test video in real time with
`-c copy`, so ingestion cost matches a real stream. It comes with scripted loud bursts, chat explosions from many
users, single-user spam and an optional forced disconnect.

`scripts/loadtest.py` starts N synthetic streams through the API and samples per-stream memory and CPU, disk
growth and pipeline latencies, then writes a report. Results depend entirely on your hardware.

## Development

- Backend: `cd backend && pip install -r requirements-dev.txt`, then run `uvicorn clippingshorts.api.main:app` and
  each `python -m clippingshorts.workers.<stream_supervisor|ai_worker|renderer>` with the same environment.
- Frontend: `cd frontend && npm install && BACKEND_URL=http://127.0.0.1:8000 npm run dev`.
- API docs: `/api/docs` (signed in).

## Security notes

- The admin password grants full control; the viewer password is read-only. Sessions are HMAC-signed HTTP-only
  cookies. State-changing requests need the `X-ClippingShorts` header (CSRF protection). Sign-in is rate limited.
- Put the dashboard behind HTTPS (a reverse proxy) and set `CLIPPINGSHORTS_COOKIE_SECURE=1` when exposing it.
- Secrets come from `.env` or Docker secrets (the Google key is mounted as one). Never commit `.env`, keys or
  cookies: `.gitignore` excludes them.

## License

MIT, see [LICENSE](LICENSE). ClippingShorts adapts parts of [PeakCut](https://github.com/haziqahmedqasmi/PeakCut)
(MIT); its copyright notice is kept in the license file as the MIT License requires. Caption fonts are under the
SIL Open Font License.
