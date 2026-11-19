"""Config schema and loading.

Precedence (highest wins): CLI overrides > env vars > config file > model defaults.
An empty or missing config file is valid — defaults cover everything.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator


# --- Schema ---------------------------------------------------------------


class PathsConfig(BaseModel):
    config_dir: str = "./config"
    data_dir: str = "./data"
    temp_dir: str = "./config/temp"
    cookies_file: str = "cookies.txt"  # resolved relative to config_dir
    db_file: str = "ytpt.db"           # resolved relative to config_dir
    log_dir: str = "logs"              # resolved relative to config_dir


class DownloadConfig(BaseModel):
    max_height: int | None = None
    limit_rate: str = "5M"
    concurrent_fragments: int = 1
    max_retries: int = 5
    subtitle_languages: list[str] = Field(default_factory=lambda: ["en"])
    download_thumbnail: bool = True
    embed_chapters: bool = True
    write_info_json: bool = True
    download_comments: bool = False
    max_comments: int = Field(default=500, ge=1)  # replies count toward this cap


class PostprocessConfig(BaseModel):
    # Both off by default. remux is lossless container swap,
    # recode is a full transcode.
    remux_to: str | None = None
    recode_to: str | None = None


class SchedulerConfig(BaseModel):
    # Gap between video completions, jittered.
    gap_min_seconds: int = 8 * 60
    gap_max_seconds: int = 12 * 60
    playlist_check_interval_seconds: int = 24 * 60 * 60


class ServerConfig(BaseModel):
    # API opt-in, daemon still runs from DB.
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    default_sort: str = "newest_archived"
    # Browser origins allowed to call the API (e.g. the Vite dev server).
    cors_origins: list[str] = Field(default_factory=list)
    # Built frontend served at "/" if this directory exists.
    # Relative paths resolve against the entrypoint directory.
    frontend_dir: str = "frontend/dist"


class YtdlpConfig(BaseModel):
    auto_update: bool = True


class NamingConfig(BaseModel):
    # {category} -> one of the [folders] names below. The "[{id}].{ext}" tail is
    # mandatory, as this prevents name collisions by using the YouTube id and lets lost files
    # be re-located later.
    template: str = "{category}/{title} [{id}].{ext}"

    @field_validator("template")
    @classmethod
    def _must_end_with_id(cls, v: str) -> str:
        if not v.endswith("[{id}].{ext}"):
            raise ValueError('naming.template must end with "[{id}].{ext}"')
        return v


class FoldersConfig(BaseModel):
    """Downloads are filed by what they are, not by which playlist found them.

    Rules, first match wins: audio-only -> audio; YouTube Short -> shorts;
    duration >= movie_min_minutes -> movies; everything else -> video.
    """

    audio: str = "audio"
    video: str = "video"
    shorts: str = "shorts"
    movies: str = "movies"
    # Fallback rule when YouTube doesn't flag a Short itself: at most this long
    # AND vertical/square.
    shorts_max_seconds: int = 180
    movie_min_minutes: int = 55  # 0 or negative (e.g. -1) disables "movies"

    @field_validator("audio", "video", "shorts", "movies")
    @classmethod
    def _plain_folder_name(cls, v: str) -> str:
        if not v or v in {".", ".."} or any(c in v for c in '/\\:*?"<>|'):
            raise ValueError(f"invalid folder name: {v!r}")
        return v


class Config(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    postprocess: PostprocessConfig = Field(default_factory=PostprocessConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    ytdlp: YtdlpConfig = Field(default_factory=YtdlpConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    folders: FoldersConfig = Field(default_factory=FoldersConfig)
    verbose: bool = False


# --- Loading --------------------------------------------------------------


def load_config(
    config_path: Path | None,
    cli_overrides: dict[str, Any],
    env: Mapping[str, str],
) -> Config:
    """Build a Config by merging file, env, and CLI, then validating."""
    file_data = _read_toml(config_path) if config_path else {}
    env_data = _env_overrides(env)
    cli_data = _nested_from_cli(cli_overrides)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, file_data)
    merged = _deep_merge(merged, env_data)
    merged = _deep_merge(merged, cli_data)

    return Config.model_validate(merged)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Pull recognized YTPT_* vars into a nested dict shape matching Config."""
    out: dict[str, Any] = {}
    mapping = {
        "YTPT_CONFIG_DIR": ("paths", "config_dir"),
        "YTPT_DATA_DIR": ("paths", "data_dir"),
        "YTPT_TEMP_DIR": ("paths", "temp_dir"),
    }
    for var, (section, key) in mapping.items():
        if var in env and env[var]:
            out.setdefault(section, {})[key] = env[var]
    # Server settings, Docker needs these to bind 0.0.0.0 without editing the
    # config file. Pydantic coerces the strings ("true", "8000") on validation.
    for var, key in (
        ("YTPT_SERVER_ENABLED", "enabled"),
        ("YTPT_SERVER_HOST", "host"),
        ("YTPT_SERVER_PORT", "port"),
    ):
        if env.get(var):
            out.setdefault("server", {})[key] = env[var]
    return out


def _nested_from_cli(cli: dict[str, Any]) -> dict[str, Any]:
    """CLI overrides arrive flat; place them under the right sections."""
    out: dict[str, Any] = {}
    if cli.get("config_dir"):
        out.setdefault("paths", {})["config_dir"] = cli["config_dir"]
    if cli.get("data_dir"):
        out.setdefault("paths", {})["data_dir"] = cli["data_dir"]
    if cli.get("temp_dir"):
        out.setdefault("paths", {})["temp_dir"] = cli["temp_dir"]
    if cli.get("verbose"):
        out["verbose"] = True
    return out


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge; overlay wins. Returns a new dict, doesn't mutate."""
    result = dict(base)
    for key, val in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(val, dict)
        ):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
