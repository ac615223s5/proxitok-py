# ProxiTok (Python)

A privacy-friendly alternative frontend for TikTok. This is a rewrite of
[ProxiTok](https://github.com/pablouser1/ProxiTok) (PHP, no longer working) using **FastAPI**,
Jinja2 and our own scraper layer. Templates, CSS and JavaScript keep the original markup, so the
default and card themes look the same as before.

TikTok's own servers are only contacted by the instance. Video, image and audio files are proxied
through `/stream`, so the user's browser never talks to TikTok.

## How it works

- **Profiles and videos**: plain HTTP. TikTok's HTML pages embed their data in a
  `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON blob, which is parsed with `httpx`. A profile's posts come
  from `/api/creator/item_list/`, which still answers unsigned requests.
- **Other feeds** (music, tags, For You, suggested accounts, comments): TikTok's web API only
  answers signed requests. One headless Chromium page (Playwright) is kept open on tiktok.com, and API
  calls run `window.fetch` inside it, so TikTok's own JavaScript adds the signature parameters.
  The browser launches lazily on the first request that needs it (the launch keeps going even if
  that request gives up) and relaunches if it crashes. Calls share the one page: a request waits at most
  `BROWSER_TIMEOUT` seconds for it and then gets a 503 "busy" page, and a call that TikTok never
  answers fails with a 504 after `BROWSER_TIMEOUT` seconds.
- **Media**: `/stream` and `/download` proxy TikTok's CDN with the same cookies as the scraper. Range
  requests pass through, so seeking works. Only TikTok's media hosts are allowed (`*.tiktokcdn.com`,
  `*.tiktokcdn-us.com`, `*.ibyteimg.com`, `*.muscdn.com` and video hosts such as
  `v16-webapp-prime.us.tiktok.com`; never `www.tiktok.com` or API hosts), and every redirect hop is
  checked again. Only video, audio, raster images and `application/octet-stream` are passed on.
  Every response carries `X-Content-Type-Options: nosniff` and a sandboxing CSP, and upstream error
  bodies are not relayed.
- Every page also answers `HEAD`, like the PHP version did.

## Setup (local)

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                              # add --extra redis for the Redis cache
uv run playwright install chromium   # add --with-deps on a fresh Linux machine
cp .env.example .env                 # optional
uv run uvicorn proxitok.main:app --port 8080
```

Then open http://localhost:8080. Run the tests with `uv run pytest`. They run offline against
saved fixtures and a fake Playwright, so Chromium isn't needed for them.

Development helpers in `scripts_dev/` (not part of the app or the Docker image):

- `capture_hydration.py name=URL ...` saves a page's hydration JSON to `tests/fixtures/name.json`.
- `shots_check.py [base_url]` takes screenshots of `/@tiktok` in both themes with the app running
  and checks that the proxied `<video>` really plays. Screenshots go to `scripts_dev/shots/`.

## Setup (Docker)

```sh
docker compose up -d --build
```

This starts the app on port 8080 and a Redis cache. The image is `python:3.12-slim` with uv and
Playwright Chromium, and it runs as a non-root user. Put overrides in `.env` (optional).
Behind a reverse proxy, set `APP_URL` to the public URL. The compose file uses `env_file` with
`required: false`, which needs Docker Compose 2.24 or newer. **The Docker setup has not been built or
run yet**, because no Docker was available while writing it.

## Configuration

Settings come from environment variables or `.env`. The names follow the PHP version where they
still apply.

| Variable | Default | Description |
|---|---|---|
| `APP_URL` | *(empty)* | Public URL of the instance, no trailing slash. When empty, links are root-relative and RSS feeds use the request's host. |
| `APP_DEBUG` | `false` | Show TikTok's raw error response on error pages. |
| `API_CACHE` | `memory` | Cache backend: `memory`, `json`, `redis` or `none`. Falls back to `memory` with a warning if the chosen backend can't be used. |
| `CACHE_TTL` | `3600` | Seconds a TikTok response stays cached. CDN URLs inside it expire, so keep this to a few hours at most. Cache entries holding video URLs are kept per process (see Known limitations). |
| `API_CACHE_JSON` | `./cache/api` | Directory for the `json` cache. |
| `REDIS_URL` | — | For example `redis://:pass@host:6379/0`. Takes precedence over the three variables below. |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | — / `6379` / — | Redis connection, if `REDIS_URL` isn't set. Requires `uv sync --extra redis`. |
| `PROXY` | — | Outbound proxy for all TikTok traffic, both httpx and Chromium: `http://`, `https://`, `socks4://` or `socks5://`. |
| `USER_AGENT` | desktop Chrome UA | User agent for the HTTP client. |
| `BROWSER_HEADLESS` | `true` | Run Chromium headless. |
| `BROWSER_TIMEOUT` | `30` | Seconds per browser navigation or in-page API call, and the longest a request waits for the shared browser before getting a 503. |

Dropped from the PHP version: `API_CHROMEDRIVER`, `API_VERIFYFP`, `API_DEVICE_ID` and `LATTE_CACHE`,
because signing now happens in the built-in browser. The "Downloader" setting is also gone, since the
third-party no-watermark services no longer exist. Both download buttons proxy TikTok's own files.

## Routes

`/`, `/about`, `/verify`, `/manifest`, `/@{user}` (`?cursor=`), `/@{user}/rss`, `/@{user}/video/{id}`,
`/t/{code}`, `/tag/{name}`, `/tag/{name}/rss`, `/music/{id}`, `/foryou`, `/foryou/rss`, `/following`,
`/settings` (+ `POST /settings/general|api|misc`), `/redirect/search`, `/redirect/download`,
`/embed/v2/{id}`, `/stream?url=`, `/download?url=&id=&user=[&watermark=1]`.

`/redirect/search?type=url&term=…` accepts full TikTok URLs (profile, video, photo, tag, music) and
short links (`vm.tiktok.com/…`, `vt.tiktok.com/…`, `tiktok.com/t/…`). Short links are resolved by
following TikTok's redirect.

## Known limitations

TikTok actively blocks automated browsers. This instance does not try to get around that, so some
pages depend on whether TikTok lets the request through:

- **Comments never load.** TikTok refuses `comment/list` for anonymous automated sessions. Video
  pages render without comments.
- **Tag feeds are always empty.** The tag header (title, view count) loads only when TikTok accepts
  the request. Otherwise the tag page returns an error (502).
- **Music, For You and Following are unreliable.** About half of signed calls get an empty HTTP 403.
  Each call is retried up to 3 times, but a page can still show an empty feed or a 502 error page.
  Reloading usually helps. A sound only shows a 404 when TikTok says it doesn't exist; a blocked
  request shows a 502. Music headers usually lack the video count, because TikTok blocks
  `music/detail`. The header is then built from the first post that uses the sound, and the
  "Videos" line is hidden. For You falls back to the single video TikTok embeds in its `/foryou` HTML.
  `/following` shows TikTok's suggested accounts, because anonymous sessions have no real following
  list, and it only has one page.
- **User profiles and single videos are reliable.** They use plain HTTP, not the browser.
- **Video URLs expire and are tied to one process.** TikTok CDN URLs only stream with the cookies of
  the process that fetched them, and they expire after some hours. So cache entries that hold video URLs
  (videos and feeds) are namespaced per process: after a restart, or in another uvicorn worker, they are
  fetched again rather than served from the JSON/Redis cache. Profile, tag and music headers and
  short-link lookups are still shared. Video links in RSS items you already downloaded stop playing after a
  restart or once they expire.
- **Slow first video page.** Each video page tries to load comments (which TikTok refuses) for up to
  12 s. The failure is remembered for 2 minutes. In practice the page takes 3–5 s, or longer while the
  browser is busy or launching.
- **No captcha handling.** If TikTok starts serving captchas to the instance's IP, feeds stop working.
  A residential `PROXY` may help.
- **Photo posts in the card theme** open the modal without a video (same as the PHP version).

## License

AGPL-3.0-or-later. This is a derivative of ProxiTok by Pablo Ferreiro.
