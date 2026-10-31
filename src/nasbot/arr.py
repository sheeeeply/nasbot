"""Sonarr / Radarr (v3 API) client and the normalized views the media watcher uses.

Both apps share the same API shape for everything used here (queue, health,
history, system status); only the media object differs (``series``/``episode``
vs ``movie``). Parsing is defensive like ``models.py``: a field that moved
between releases shows up as "?" instead of crashing the bot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from .config import KINDS, ArrInstance, Kind, normalize_arr_url
from .models import _dt, _num

__all__ = ["KINDS", "ArrInstance", "Kind", "normalize_arr_url"]  # re-exported for callers of this module

log = logging.getLogger(__name__)

PAGE_SIZE = 200
MAX_PAGES = 25  # 5,000 queue rows is far beyond any home setup; stop rather than loop forever


class ArrUnavailable(Exception):
    """Sonarr/Radarr could not be reached (network, timeout, HTTP 5xx)."""


class ArrAuthError(ArrUnavailable):
    """Reached, but the API key was rejected (HTTP 401/403)."""


class ArrCallError(Exception):
    """Reached, but the request failed or the answer wasn't the expected JSON."""


# ----------------------------------------------------------------------- models

def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


@dataclass(frozen=True)
class QueueItem:
    """One row of the Sonarr/Radarr queue. A season pack is one row per episode,
    all sharing the same ``download_id``."""

    app: Kind
    id: int
    download_id: str | None
    release: str
    subject: str
    season: int | None = None
    episode: int | None = None
    absolute: int | None = None
    status: str = ""
    tracked_status: str = ""  # ok / warning / error
    tracked_state: str = ""  # downloading / importBlocked / importPending / importing / imported / failedPending…
    messages: tuple[tuple[str, tuple[str, ...]], ...] = ()
    error: str | None = None
    size: float | None = None
    sizeleft: float | None = None
    eta: datetime | None = None
    protocol: str = ""
    client: str = ""

    @property
    def key(self) -> str:
        return self.download_id or f"item-{self.id}"

    @property
    def progress(self) -> float | None:
        if not self.size:
            return None
        left = self.sizeleft if self.sizeleft is not None else self.size
        return max(0.0, min(1.0, 1 - left / self.size))

    @classmethod
    def parse(cls, app: Kind, rec: dict[str, Any]) -> QueueItem:
        season = episode = absolute = None
        if app == "sonarr":
            series = rec.get("series") or {}
            subject = series.get("title") or rec.get("title") or "Unknown series"
            ep = rec.get("episode") or {}
            season = _int(ep.get("seasonNumber", rec.get("seasonNumber")))
            episode = _int(ep.get("episodeNumber"))
            absolute = _int(ep.get("absoluteEpisodeNumber"))
        else:
            movie = rec.get("movie") or {}
            title = movie.get("title")
            year = movie.get("year")
            subject = (f"{title} ({year})" if title and year else title) or rec.get("title") or "Unknown movie"
        messages = []
        for m in rec.get("statusMessages") or []:
            if isinstance(m, dict):
                msgs = tuple(str(x) for x in (m.get("messages") or []) if x)
                messages.append((str(m.get("title") or ""), msgs))
        return cls(
            app=app,
            id=_int(rec.get("id")) or 0,
            download_id=rec.get("downloadId") or None,
            release=str(rec.get("title") or "?"),
            subject=str(subject),
            season=season,
            episode=episode,
            absolute=absolute,
            status=_lower(rec.get("status")),
            tracked_status=_lower(rec.get("trackedDownloadStatus")),
            tracked_state=_lower(rec.get("trackedDownloadState")),
            messages=tuple(messages),
            error=rec.get("errorMessage") or None,
            size=_num(rec.get("size")),
            sizeleft=_num(rec.get("sizeleft")),
            eta=_dt(rec.get("estimatedCompletionTime")),
            protocol=_lower(rec.get("protocol")),
            client=str(rec.get("downloadClient") or ""),
        )


@dataclass(frozen=True)
class Release:
    """Queue rows grouped by download: what a human thinks of as "one download"."""

    app: Kind
    key: str
    items: tuple[QueueItem, ...]

    @property
    def first(self) -> QueueItem:
        return self.items[0]

    @property
    def release(self) -> str:
        return self.first.release

    @property
    def subject(self) -> str:
        return self.first.subject

    @property
    def episodes(self) -> list[tuple[int, int]]:
        return sorted({(i.season, i.episode) for i in self.items if i.season is not None and i.episode is not None})

    @property
    def size(self) -> float | None:
        return self.first.size  # every row of a pack carries the size of the whole download

    @property
    def progress(self) -> float | None:
        return self.first.progress

    @property
    def eta(self) -> datetime | None:
        return self.first.eta

    @property
    def messages(self) -> list[tuple[str, int]]:
        """Distinct problem messages with the number of files each applies to, most common first.

        Every row of a pack repeats the same per-file messages, so files are counted, not rows.
        """
        per_msg: dict[str, set[str]] = {}
        for item in self.items:
            for title, msgs in item.messages:
                for msg in msgs:
                    per_msg.setdefault(msg, set()).add(title or item.release)
            if item.error:
                per_msg.setdefault(item.error, set()).add(item.release)
        return sorted(((m, len(files)) for m, files in per_msg.items()), key=lambda kv: (-kv[1], kv[0]))


def group_releases(items: list[QueueItem]) -> list[Release]:
    groups: dict[str, list[QueueItem]] = {}
    for item in items:
        groups.setdefault(item.key, []).append(item)
    return [Release(items[0].app, key, tuple(rows)) for key, rows in groups.items()]


@dataclass(frozen=True)
class HealthIssue:
    app: Kind
    source: str
    type: str  # notice / warning / error
    message: str
    wiki: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}|{self.message}"

    @property
    def level(self) -> str:
        return {"error": "ERROR", "warning": "WARNING"}.get(self.type, "NOTICE")

    @classmethod
    def parse(cls, app: Kind, rec: dict[str, Any]) -> HealthIssue:
        return cls(app=app, source=str(rec.get("source") or "?"), type=_lower(rec.get("type")) or "warning",
                   message=str(rec.get("message") or "").strip(), wiki=rec.get("wikiUrl") or None)


@dataclass(frozen=True)
class ImportEvent:
    app: Kind
    download_id: str | None
    subject: str
    when: datetime | None
    season: int | None = None
    episode: int | None = None
    quality: str | None = None
    source_title: str = ""

    @property
    def group_key(self) -> str:
        return self.download_id or f"{self.subject}|{self.source_title}"

    @classmethod
    def parse(cls, app: Kind, rec: dict[str, Any]) -> ImportEvent:
        season = episode = None
        if app == "sonarr":
            subject = (rec.get("series") or {}).get("title") or "Unknown series"
            ep = rec.get("episode") or {}
            season, episode = _int(ep.get("seasonNumber")), _int(ep.get("episodeNumber"))
        else:
            movie = rec.get("movie") or {}
            title, year = movie.get("title"), movie.get("year")
            subject = (f"{title} ({year})" if title and year else title) or "Unknown movie"
        quality = ((rec.get("quality") or {}).get("quality") or {}).get("name")
        return cls(app=app, download_id=rec.get("downloadId") or None, subject=str(subject),
                   when=_dt(rec.get("date")), season=season, episode=episode, quality=quality,
                   source_title=str(rec.get("sourceTitle") or ""))


# ----------------------------------------------------------------------- client

class ArrClient:
    """Read-only client for one Sonarr or Radarr instance."""

    def __init__(self, instance: ArrInstance, *, timeout: float = 20.0, verify_ssl: bool = True,
                 session: aiohttp.ClientSession | None = None):
        self.instance = instance
        self.timeout = timeout
        self._ssl: bool = bool(verify_ssl)  # False = accept self-signed certs (aiohttp convention)
        self._session = session
        self._owns_session = session is None

    @property
    def kind(self) -> Kind:
        return self.instance.kind

    @property
    def name(self) -> str:
        return self.instance.name

    def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.instance.url}/api/v3/{path}"
        clean = {k: ("true" if v is True else "false" if v is False else v) for k, v in (params or {}).items()}
        headers = {"X-Api-Key": self.instance.api_key, "Accept": "application/json"}
        try:
            async with self._sess().get(url, params=clean, headers=headers, ssl=self._ssl,
                                        timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                if resp.status in (401, 403):
                    raise ArrAuthError(f"{self.name} rejected the API key (HTTP {resp.status})")
                if resp.status >= 500:
                    raise ArrUnavailable(f"{self.name} returned HTTP {resp.status}")
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise ArrCallError(f"{self.name} /{path} failed: HTTP {resp.status} {body}".strip())
                try:
                    return await resp.json(content_type=None)
                except ValueError as exc:
                    raise ArrCallError(f"{self.name} /{path} didn't return JSON — is the URL "
                                       f"({self.instance.url}) pointing at {self.name}?") from exc
        except (ArrUnavailable, ArrCallError):
            raise
        except aiohttp.InvalidURL as exc:
            raise ArrCallError(f"{self.name}: invalid URL {self.instance.url!r}") from exc
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            detail = str(exc) or type(exc).__name__
            raise ArrUnavailable(f"{self.name} unreachable at {self.instance.url}: {detail}") from exc

    async def status(self) -> dict[str, Any]:
        data = await self._get("system/status")
        if not isinstance(data, dict):
            raise ArrCallError(f"{self.name} /system/status: unexpected answer")
        return data

    async def queue(self) -> list[QueueItem]:
        # Unknown items (torrents in the category that Sonarr/Radarr didn't grab) stay hidden, matching the
        # apps' own default view; otherwise every hand-added torrent would look like a stuck import.
        if self.kind == "sonarr":
            extra = {"includeUnknownSeriesItems": False, "includeSeries": True, "includeEpisode": True}
        else:
            extra = {"includeUnknownMovieItems": False, "includeMovie": True}
        items: list[QueueItem] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._get("queue", {"page": page, "pageSize": PAGE_SIZE, **extra})
            if not isinstance(data, dict):
                raise ArrCallError(f"{self.name} /queue: unexpected answer")
            records = data.get("records") or []
            items += [QueueItem.parse(self.kind, r) for r in records if isinstance(r, dict)]
            total = _int(data.get("totalRecords")) or 0
            if not records or len(items) >= total:
                break
        return items

    async def health(self) -> list[HealthIssue]:
        data = await self._get("health")
        if not isinstance(data, list):
            raise ArrCallError(f"{self.name} /health: unexpected answer")
        return [HealthIssue.parse(self.kind, r) for r in data if isinstance(r, dict)]

    async def imports_since(self, since: datetime) -> list[ImportEvent]:
        params: dict[str, Any] = {"date": since.isoformat().replace("+00:00", "Z"),
                                  "eventType": "downloadFolderImported"}
        params.update({"includeSeries": True, "includeEpisode": True} if self.kind == "sonarr"
                      else {"includeMovie": True})
        data = await self._get("history/since", params)
        if not isinstance(data, list):
            raise ArrCallError(f"{self.name} /history/since: unexpected answer")
        events = [ImportEvent.parse(self.kind, r) for r in data if isinstance(r, dict)
                  and _lower(r.get("eventType")) in ("downloadfolderimported", "3")]
        return [e for e in events if e.when is None or e.when > since]
