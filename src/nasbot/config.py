"""Configuration: parsed from environment variables, validated all at once.

Every secret also accepts a ``*_FILE`` variant (Docker/compose secrets) that
points at a file holding the value.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# TrueNAS alert severities, least to most severe (middlewared AlertLevel).
LEVELS: tuple[str, ...] = ("INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")


def level_rank(level: str) -> int:
    """Rank of a TrueNAS alert level; unknown levels rank as WARNING."""
    try:
        return LEVELS.index(level.upper())
    except ValueError:
        return LEVELS.index("WARNING")


Kind = Literal["sonarr", "radarr"]
KINDS: tuple[Kind, ...] = ("sonarr", "radarr")
DISPLAY: dict[str, str] = {"sonarr": "Sonarr", "radarr": "Radarr"}


def normalize_arr_url(raw: str) -> str:
    """``192.168.1.158:8989`` -> ``http://192.168.1.158:8989``; keeps a URL base such as
    ``/sonarr``; drops a trailing slash or a pasted ``/api/v3``."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty URL")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"unsupported scheme {parts.scheme!r} (use http or https)")
    if not parts.netloc:
        raise ValueError("missing host")
    path = parts.path.rstrip("/")
    for suffix in ("/api/v3", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))


@dataclass(frozen=True)
class ArrInstance:
    kind: Kind
    url: str
    api_key: str = field(repr=False)
    link_url: str | None = None  # where humans open the UI, if different from `url`

    @property
    def name(self) -> str:
        return DISPLAY[self.kind]

    @property
    def web_url(self) -> str:
        return self.link_url or self.url


class ConfigError(ValueError):
    """Raised with every configuration problem found, not just the first."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n  - " + "\n  - ".join(problems))


@dataclass(frozen=True)
class Config:
    # Discord
    discord_token: str
    alert_channel_id: int
    guild_id: int | None = None
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_role_ids: frozenset[int] = field(default_factory=frozenset)
    mention_role_id: int | None = None
    mention_user_id: int | None = None
    mention_min_level: str = "CRITICAL"
    ephemeral_replies: bool = False
    announce_startup: bool = True

    # TrueNAS
    truenas_url: str = "wss://truenas.local/api/current"
    truenas_username: str = ""
    truenas_api_key: str = ""
    truenas_auth: Literal["plain", "scram"] = "plain"
    truenas_verify_ssl: bool = True
    truenas_channel_binding: bool = True
    call_timeout: float = 30.0

    # Monitoring
    poll_interval: int = 60
    unreachable_after: int = 3
    min_alert_level: str = "WARNING"
    ignore_alert_classes: frozenset[str] = field(default_factory=frozenset)
    watch_apps: bool = True
    digest_time: time | None = None
    digest_tz: str = "UTC"
    healthcheck_url: str | None = None

    # Media (Sonarr / Radarr) — all optional; the watcher is off unless an instance is configured
    arr_instances: tuple[ArrInstance, ...] = ()
    media_poll_interval: int = 300
    media_grace_minutes: int = 10
    media_watch_stalled: bool = False
    media_notify_imports: bool = False
    media_mention: bool = False
    media_health_ignore: frozenset[str] = field(default_factory=frozenset)
    media_verify_ssl: bool = True
    media_timeout: float = 20.0

    # Runtime
    data_dir: Path = Path("/data")
    log_level: str = "INFO"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.digest_tz)

    @property
    def mention(self) -> str | None:
        """Discord mention markup for urgent notifications, if configured."""
        if self.mention_role_id:
            return f"<@&{self.mention_role_id}>"
        if self.mention_user_id:
            return f"<@{self.mention_user_id}>"
        return None

    @property
    def access_is_open(self) -> bool:
        return not self.allowed_user_ids and not self.allowed_role_ids

    @property
    def media_enabled(self) -> bool:
        return bool(self.arr_instances)


def normalize_truenas_url(raw: str) -> str:
    """Turn whatever the user typed into the JSON-RPC websocket endpoint.

    ``nas.example.ts.net`` / ``https://nas`` -> ``wss://nas/api/current``;
    ``http://10.0.0.5`` -> ``ws://10.0.0.5/api/current``. An explicit path is kept.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("empty URL")
    if "://" not in raw:
        raw = "wss://" + raw
    parts = urlsplit(raw)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(parts.scheme.lower())
    if scheme is None:
        raise ValueError(f"unsupported scheme {parts.scheme!r} (use https/wss or http/ws)")
    if not parts.netloc:
        raise ValueError("missing host")
    path = parts.path.rstrip("/")
    if path in ("", "/api"):
        path = "/api/current"
    elif path == "/websocket":
        raise ValueError("'/websocket' is the legacy pre-25.04 API; this bot needs TrueNAS 25.04+ (/api/current)")
    return urlunsplit((scheme, parts.netloc, path, "", ""))


_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def _bool(value: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"expected true/false, got {value!r}")


def _id_set(value: str) -> frozenset[int]:
    out = set()
    for part in value.replace(";", ",").replace(" ", ",").split(","):
        if part:
            out.add(int(part))
    return frozenset(out)


def _hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def from_env(env: Mapping[str, str] | None = None, *, require_discord: bool = True) -> Config:
    """Build a Config from environment variables (see .env.example).

    ``require_discord=False`` is for ``nasbot check``, which only talks to TrueNAS.
    """
    env = os.environ if env is None else env
    if not require_discord:
        env = {"DISCORD_TOKEN": "unused", "DISCORD_ALERT_CHANNEL_ID": "0", **env}
    problems: list[str] = []
    values: dict = {}

    def secret(name: str) -> str | None:
        file_var = f"{name}_FILE"
        if env.get(file_var):
            try:
                return Path(env[file_var]).read_text().strip()
            except OSError as exc:
                problems.append(f"{file_var}: cannot read {env[file_var]!r} ({exc.strerror})")
                return None
        return env.get(name, "").strip() or None

    def get(name: str, conv, key: str, *, required: bool = False):
        raw = env.get(name)
        if raw is None or raw.strip() == "":
            if required:
                problems.append(f"{name} is required")
            return
        try:
            values[key] = conv(raw.strip())
        except (ValueError, TypeError) as exc:
            problems.append(f"{name}: {exc}")

    token = secret("DISCORD_TOKEN")
    if not token:
        problems.append("DISCORD_TOKEN (or DISCORD_TOKEN_FILE) is required")
    api_key = secret("TRUENAS_API_KEY")
    if not api_key:
        problems.append("TRUENAS_API_KEY (or TRUENAS_API_KEY_FILE) is required")

    get("DISCORD_ALERT_CHANNEL_ID", int, "alert_channel_id", required=True)
    get("DISCORD_GUILD_ID", int, "guild_id")
    get("DISCORD_ALLOWED_USER_IDS", _id_set, "allowed_user_ids")
    get("DISCORD_ALLOWED_ROLE_IDS", _id_set, "allowed_role_ids")
    get("DISCORD_MENTION_ROLE_ID", int, "mention_role_id")
    get("DISCORD_MENTION_USER_ID", int, "mention_user_id")
    get("DISCORD_EPHEMERAL_REPLIES", _bool, "ephemeral_replies")
    get("ANNOUNCE_STARTUP", _bool, "announce_startup")

    get("TRUENAS_URL", normalize_truenas_url, "truenas_url", required=True)
    get("TRUENAS_USERNAME", str, "truenas_username", required=True)
    get("TRUENAS_VERIFY_SSL", _bool, "truenas_verify_ssl")
    get("TRUENAS_CHANNEL_BINDING", _bool, "truenas_channel_binding")
    get("TRUENAS_CALL_TIMEOUT", float, "call_timeout")

    auth = env.get("TRUENAS_AUTH", "plain").strip().lower()
    if auth not in ("plain", "scram"):
        problems.append("TRUENAS_AUTH must be 'plain' (TrueNAS 25.04/25.10) or 'scram' (TrueNAS 26-beta / 27+)")
    else:
        values["truenas_auth"] = auth

    get("POLL_INTERVAL_SECONDS", int, "poll_interval")
    get("UNREACHABLE_AFTER_FAILURES", int, "unreachable_after")
    get("ALERT_IGNORE_CLASSES", lambda v: frozenset(p.strip() for p in v.split(",") if p.strip()),
        "ignore_alert_classes")
    get("WATCH_APPS", _bool, "watch_apps")
    get("DIGEST_TIME", _hhmm, "digest_time")
    get("DIGEST_TZ", str, "digest_tz")
    get("HEALTHCHECK_PING_URL", str, "healthcheck_url")
    get("DATA_DIR", Path, "data_dir")
    get("LOG_LEVEL", lambda v: v.upper(), "log_level")

    # Media watcher: an instance is enabled by giving both its URL and its API key.
    instances: list[ArrInstance] = []
    for kind in KINDS:
        prefix = kind.upper()
        url_raw = env.get(f"{prefix}_URL", "").strip()
        key = secret(f"{prefix}_API_KEY")
        if not url_raw and not key:
            continue
        if not url_raw:
            problems.append(f"{prefix}_API_KEY is set but {prefix}_URL is missing")
            continue
        if not key:
            problems.append(f"{prefix}_URL is set but {prefix}_API_KEY (or {prefix}_API_KEY_FILE) is missing "
                            f"— find it in {DISPLAY[kind]} → Settings → General → Security")
            continue
        try:
            url = normalize_arr_url(url_raw)
            link_raw = env.get(f"{prefix}_LINK_URL", "").strip()
            link = normalize_arr_url(link_raw) if link_raw else None
        except ValueError as exc:
            problems.append(f"{prefix}_URL / {prefix}_LINK_URL: {exc}")
            continue
        instances.append(ArrInstance(kind=kind, url=url, api_key=key, link_url=link))
    values["arr_instances"] = tuple(instances)
    get("MEDIA_POLL_INTERVAL_SECONDS", int, "media_poll_interval")
    get("MEDIA_GRACE_MINUTES", int, "media_grace_minutes")
    get("MEDIA_WATCH_STALLED", _bool, "media_watch_stalled")
    get("MEDIA_NOTIFY_IMPORTS", _bool, "media_notify_imports")
    get("MEDIA_MENTION", _bool, "media_mention")
    get("MEDIA_HEALTH_IGNORE", lambda v: frozenset(p.strip() for p in v.split(",") if p.strip()),
        "media_health_ignore")
    get("MEDIA_VERIFY_SSL", _bool, "media_verify_ssl")
    get("MEDIA_TIMEOUT", float, "media_timeout")

    for var, key in (("ALERT_MIN_LEVEL", "min_alert_level"), ("MENTION_MIN_LEVEL", "mention_min_level")):
        raw = env.get(var, "").strip().upper()
        if raw:
            if raw not in LEVELS:
                problems.append(f"{var} must be one of {', '.join(LEVELS)}")
            else:
                values[key] = raw

    # Range checks
    if values.get("poll_interval", 60) < 15:
        problems.append("POLL_INTERVAL_SECONDS must be >= 15 (TrueNAS rate-limits aggressive clients)")
    if values.get("unreachable_after", 3) < 1:
        problems.append("UNREACHABLE_AFTER_FAILURES must be >= 1")
    if values.get("media_poll_interval", 300) < 60:
        problems.append("MEDIA_POLL_INTERVAL_SECONDS must be >= 60")
    if values.get("media_grace_minutes", 10) < 0:
        problems.append("MEDIA_GRACE_MINUTES must be >= 0")
    if values.get("media_timeout", 20.0) <= 0:
        problems.append("MEDIA_TIMEOUT must be > 0")
    if values.get("call_timeout", 30.0) <= 0:
        problems.append("TRUENAS_CALL_TIMEOUT must be > 0")
    if "digest_tz" in values:
        try:
            ZoneInfo(values["digest_tz"])
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(f"DIGEST_TZ: unknown time zone {values['digest_tz']!r}")
    if values.get("log_level", "INFO") not in logging.getLevelNamesMapping():
        problems.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    insecure_url = values.get("truenas_url", "").startswith("ws://")
    if values.get("truenas_auth") == "plain" and insecure_url:
        try:
            allow = _bool(env.get("TRUENAS_ALLOW_INSECURE", "false"))
        except ValueError:
            allow = False
        if not allow:
            problems.append("TRUENAS_URL is unencrypted (http/ws) with TRUENAS_AUTH=plain: TrueNAS automatically "
                            "REVOKES an API key sent over an insecure connection. Use https/wss (the default for "
                            "a bare hostname) with TRUENAS_VERIFY_SSL=false for the self-signed certificate.")
    if values.get("truenas_auth") == "scram" and insecure_url and values.get("truenas_channel_binding", True):
        problems.append("TRUENAS_AUTH=scram over ws:// needs TRUENAS_CHANNEL_BINDING=false (binding requires TLS)")

    if problems:
        raise ConfigError(problems)
    return Config(discord_token=token, truenas_api_key=api_key, **values)  # type: ignore[arg-type]
