# ytpt: YouTube Playlist Tracker

A self-hosted archiver and change tracker for YouTube playlists, channels and videos. Give it URLs; it downloads everything with [yt-dlp](https://github.com/yt-dlp/yt-dlp), re-checks on a schedule, and records **how each source changes over time** (videos added, removed, reordered) so lost media isn't lost.

- **Core-first.** `core/` is a plain Python library. `cli/` gives quick and simple control from the terminal. `api/` gives control over HTTP and is also the hook for a web GUI. Both are thin layers on top of `core/`, designed to be easily swapped or pulled out as you see fit. ytpt runs fully headless from the CLI and a config file.
- **Polite by default.** Designed to be soft on Google's servers, with just one global download worker, a jittered 8-12 minute pause between downloads, a bandwidth cap, and daily source checks.
- **Never deletes.** Videos stay on disk even if the video disappears from YouTube or you stop tracking its source (such videos show up under *Orphaned*). A file is only deleted when you give explicit permission.
- **Change tracking.** Every check is snapshotted, and the API exposes a per-source timeline of adds/removals/reorders.

A simple web GUI is also available and is recommended to get the full use out of ytpt. It can be found here: [YouTube-Playlist-Tracker-GUI](https://github.com/GamerBossHarmon/YouTube-Playlist-Tracker-GUI).

## Quick start (local)

Requires Python 3.12, [ffmpeg](https://ffmpeg.org/), and Node.js or Deno (yt-dlp uses a JS runtime for YouTube).

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
# source .venv/bin/activate       # Linux/macOS
pip install -r requirements.txt

python ytpt.py add "https://www.youtube.com/playlist?list=PL..."   # track and download a source. A channel or video URL also works.
python ytpt.py list sources                                        # shows the source above as tracked
python ytpt.py run                                                 # starts the daemon (checks + downloads), Ctrl+C to stop
```

Commands: `add <url>`, `list sources|videos|orphaned`, `check [--source ID]`, `retry [ID | --source ID | --all]`,
`run`. Global flags: `--config FILE`, `--config-dir`, `--data-dir`, `--temp-dir`, `-v`.

## Change tracking

Playlists change, videos get added, deleted, made private, or moved around. Every time ytpt checks a source it saves a **snapshot** of the playlist and compares it with the previous one, recording exactly what changed:

- **Added:** new videos that appeared in the playlist.
- **Removed:** videos that left the playlist (deleted, privated, or taken out by the owner). The video stays in your archive, and its download is kept if you already have it.
- **Reordered:** videos that kept their place in the playlist but changed position.

If a removed video comes back, it's restored to the playlist's current members.

Sources are checked daily by default (`playlist_check_interval_seconds` under `[scheduler]` in `ytpt.conf`). You can see the results in a few places:

- **CLI:** `python ytpt.py check` checks now and prints a summary per source, for example `+2 -1, 3 reordered`.
- **API / GUI:** `GET /api/sources/{id}/timeline` returns the per-check history, newest first, with the titles of the videos that were added, removed or moved. The GUI shows this as a timeline for each source.

## Configuration

Copy `ytpt.conf.example` to `ytpt.conf` (next to `ytpt.py`). Every option is shown commented out with its default value and a description; uncomment what you want to change. The file is optional.

Precedence: **CLI args > environment > config file > defaults**.

| Environment variable | Meaning |
|---|---|
| `YTPT_CONFIG` | path to the config file |
| `YTPT_CONFIG_DIR`, `YTPT_DATA_DIR`, `YTPT_TEMP_DIR` | where the database/cookies, downloads and in-progress files go |
| `YTPT_SERVER_ENABLED`, `YTPT_SERVER_HOST`, `YTPT_SERVER_PORT` | the web API (below) |

Environment variables are not required. They are a convenience, mainly for Docker.

State lives in `config/` (SQLite database, `cookies.txt`) and `data/` (the default download directory).

**Cookies** (private/members-only videos, Watch Later, age gates): put a Netscape-format `cookies.txt` in `config/`. That is the **primary account**, and it does all the downloading. A throwaway Google account is recommended, as heavy downloading with your main account risks it being flagged.

**Extra accounts** (read-only): from the web GUI's Accounts page you can upload more cookies files, or use `POST /api/accounts`. They are stored in `config/accounts/`. A source can be set to be *read* by an extra account, which allows tracking a private playlist that belongs to a side account. Its videos still download with the primary account. If only a side account can open a video (e.g. members-only), use "download with this account" in the GUI to download that single video. The Accounts page also lists each account's playlists so you can track them with one click.

## Web API and GUI

The API is off by default. To use it (and the web GUI), set `[server] enabled = true` in `ytpt.conf` (or `YTPT_SERVER_ENABLED=true`). `ytpt run` will then also serve a REST + SSE API on `127.0.0.1:8000` (interactive docs at `/api/docs`). There is **no built-in authentication**: it binds to loopback by default, and if you expose it, do so behind Tailscale or a reverse proxy.

The GUI ([YouTube-Playlist-Tracker-GUI](https://github.com/GamerBossHarmon/YouTube-Playlist-Tracker-GUI)) is a standalone app that proxies to this API.

Alternatively, ytpt can serve a built GUI itself via `server.frontend_dir`.

## Docker

```bash
docker compose up -d --build       # runs the daemon; state persists in ./config and ./data
```

The image's default config is `ytpt.conf`. See `docker-compose.yml` for enabling the API (it stays on
`127.0.0.1` of the host), mounting your own `ytpt.conf`, and running as your own user on Linux. To run the API *and*
the GUI together, see `docker-compose.full.yml` in the GUI project.

## Development

```bash
pip install -r requirements-dev.txt
pytest                             # unit + integration; network tests are excluded by default
pytest -m network                  # the ones that hit real YouTube
```

## Contributing

Feedback and contributions are welcome. This is a small personal project, so replies may not be instant, but everything gets read.

- **Questions, ideas and comments:** open an [issue](https://github.com/GamerBossHarmon/YouTube-Playlist-Tracker/issues) and say what's on your mind.
- **Bug reports:** open an issue with what you did, what you expected, what happened, and the output of `ytpt -v` if you can. Remove any cookies or personal info from logs first.
- **Pull requests:** welcome. For anything big, open an issue first so we can agree on the approach. Please run `pytest` before submitting and add tests for new behavior.
- **GUI issues and PRs** go to the [GUI repo](https://github.com/GamerBossHarmon/YouTube-Playlist-Tracker-GUI).

## Disclaimer

ytpt is intended for personal archival of media you have the right to access. You are responsible for complying with [YouTube's Terms of Service](https://www.youtube.com/t/terms) and with the copyright and licensing rights of the creators whose content you download. Downloading content may violate YouTube's Terms of Service, and automated access can result in rate limiting or account restrictions. Use a throwaway account and the default polite settings. Do not redistribute content you do not have permission to share. The authors are not affiliated with YouTube or Google, and the software is provided as is, without warranty (see [LICENSE](LICENSE)).

## Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp): does all the downloading and metadata extraction.
- [FFmpeg](https://ffmpeg.org/): merges and processes the downloaded streams.
- [Node.js](https://nodejs.org/) / [Deno](https://deno.com/): the JS runtime yt-dlp uses for YouTube.
- [FastAPI](https://fastapi.tiangolo.com/) and [Uvicorn](https://www.uvicorn.org/): the REST + SSE web API.
- [SQLModel](https://sqlmodel.tiangolo.com/) and [Alembic](https://alembic.sqlalchemy.org/): the SQLite database layer and its migrations.
- [Pydantic](https://docs.pydantic.dev/): data validation and settings models.
- [APScheduler](https://apscheduler.readthedocs.io/): the scheduled source checks.
- [pytest](https://pytest.org/) and [HTTPX](https://www.python-httpx.org/): the test suite.

## License

[MIT](LICENSE)
