"""The media watcher: polls Sonarr / Radarr and emits Notifications, like monitor.py
does for TrueNAS. No Discord I/O here; every rule is testable with fake clients.

What it reports:
  * a download that needs a human — finished but "Unable to Import Automatically",
    or failed — after a grace period, once; and again when it's sorted
  * a download still "Waiting to Import" for over two days (normally temporary:
    e.g. Sonarr holds new episodes whose title is still TBA for up to 48 hours)
  * optionally, stalled downloads (off by default: Cleanuparr handles those)
  * Sonarr/Radarr health problems appearing / clearing (indexers down, download
    client unreachable, disk space…), first check per app is a silent baseline
  * Sonarr/Radarr themselves unreachable (not while the whole NAS is down — the
    NAS alert already covers that), and coming back
  * optionally, "ready to watch" when something is imported
State lives in the shared monitor State, so mute, persistence and restarts behave
exactly like the TrueNAS side.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import formatting
from .arr import (
    ArrAuthError,
    ArrCallError,
    ArrClient,
    ArrUnavailable,
    HealthIssue,
    ImportEvent,
    QueueItem,
    Release,
    group_releases,
)
from .config import ArrInstance, Config, level_rank
from .notify import Notification
from .state import State

log = logging.getLogger(__name__)

BURST_THRESHOLD = 5  # more new items than this in one check are bundled into one message
REASON_RANK = {"stalled": 0, "waiting": 1, "import": 2, "failed": 3}
# "Waiting to Import" is a temporary hold (TBA episode title, which Sonarr waits up to 48 h for; a
# not-yet-extracted archive…). Only worth a message if it outlasts that window.
WAITING_GRACE_SECONDS = 49 * 3600
HEALTH_TYPES = ("warning", "error")


def classify(item: QueueItem, *, watch_stalled: bool) -> str | None:
    """Why a queue row needs a human, or None if it doesn't."""
    state, status = item.tracked_state, item.tracked_status
    if state == "importblocked":  # "Downloaded - Unable to Import Automatically"
        return "import"
    if state == "importpending" and status == "warning":  # "Downloaded - Waiting to Import" (temporary)
        return "waiting"
    if state in ("failedpending", "failed") or status == "error":
        return "failed"
    if watch_stalled and state == "downloading" and status == "warning":
        return "stalled"
    return None


def release_reason(release: Release, *, watch_stalled: bool) -> str | None:
    reasons = [r for r in (classify(i, watch_stalled=watch_stalled) for i in release.items) if r]
    return max(reasons, key=REASON_RANK.__getitem__) if reasons else None


@dataclass
class AppOverview:
    """What `/media queue` and `/media health` show for one app."""

    instance: ArrInstance
    releases: list[tuple[Release, str | None]] = field(default_factory=list)
    health: list[HealthIssue] = field(default_factory=list)
    error: str | None = None


class MediaWatcher:
    def __init__(self, clients: list[ArrClient], cfg: Config,
                 *, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.clients = clients
        self.cfg = cfg
        self.now = now
        self.last_check: datetime | None = None
        self.last_ok: dict[str, bool] = {}
        self.last_error: dict[str, str] = {}

    @classmethod
    def from_config(cls, cfg: Config) -> MediaWatcher | None:
        if not cfg.media_enabled:
            return None
        clients = [ArrClient(i, timeout=cfg.media_timeout, verify_ssl=cfg.media_verify_ssl)
                   for i in cfg.arr_instances]
        return cls(clients, cfg)

    async def close(self) -> None:
        for c in self.clients:
            await c.close()

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.clients]

    def due(self, now: datetime) -> bool:
        return self.last_check is None or (now - self.last_check).total_seconds() >= self.cfg.media_poll_interval

    # ------------------------------------------------------------------ polling

    async def check(self, state: State, now: datetime, *, nas_down: bool = False) -> list[Notification]:
        self.last_check = now
        out: list[Notification] = []
        for client in self.clients:
            try:
                out += await self._check_one(client, state, now, nas_down)
            except Exception:  # noqa: BLE001 - one app's surprise must not stop the other or the NAS monitor
                log.exception("Media check for %s crashed; continuing", client.name)
        return out

    async def _check_one(self, client: ArrClient, state: State, now: datetime, nas_down: bool) -> list[Notification]:
        app = client.kind
        try:
            queue = await client.queue()
            health = await client.health()
        except ArrUnavailable as exc:
            self.last_ok[app] = False
            self.last_error[app] = str(exc)
            return self._on_failure(client, state, now, exc, nas_down)
        except ArrCallError as exc:
            self.last_ok[app] = False
            self.last_error[app] = str(exc)
            log.error("%s answered but the request failed: %s", client.name, exc)
            return []
        self.last_ok[app] = True
        self.last_error.pop(app, None)
        out = self._on_success(client, state, now)
        out += self._diff_attention(client.instance, state, now, queue)
        out += self._diff_health(client.instance, state, health)
        if self.cfg.media_notify_imports:
            out += await self._imports(client, state, now)
        return out

    # ------------------------------------------------------------- reachability

    def _on_failure(self, client: ArrClient, state: State, now: datetime, exc: ArrUnavailable,
                    nas_down: bool) -> list[Notification]:
        app = client.kind
        state.media_failures[app] = state.media_failures.get(app, 0) + 1
        n = state.media_failures[app]
        log.warning("%s check failed (%d/%d): %s", client.name, n, self.cfg.unreachable_after, exc)
        threshold_hit = isinstance(exc, ArrAuthError) or n >= self.cfg.unreachable_after
        if threshold_hit and app not in state.media_down_since and not nas_down:
            state.media_down_since[app] = now.timestamp()
            return [Notification("media_down", formatting.arr_down_embed(client.instance, now, str(exc)),
                                 f"{client.name} unreachable", urgent=self._urgent("ERROR"))]
        return []

    def _on_success(self, client: ArrClient, state: State, now: datetime) -> list[Notification]:
        app = client.kind
        state.media_failures.pop(app, None)
        since = state.media_down_since.pop(app, None)
        if since is None:
            return []
        return [Notification("media_up", formatting.arr_up_embed(client.instance, now.timestamp() - since),
                             f"{client.name} reachable again")]

    # ---------------------------------------------------------------- attention

    def _diff_attention(self, inst: ArrInstance, state: State, now: datetime,
                        queue: list[QueueItem]) -> list[Notification]:
        app = inst.kind
        current: dict[str, tuple[Release, str]] = {}
        for rel in group_releases(queue):
            reason = release_reason(rel, watch_stalled=self.cfg.media_watch_stalled)
            if reason:
                current[rel.key] = (rel, reason)

        known = state.media_attention.get(app, {})
        base_grace = self.cfg.media_grace_minutes * 60
        fresh: dict[str, dict] = {}
        raised: list[tuple[Release, str]] = []
        for key, (rel, reason) in current.items():
            entry = known.get(key)
            if entry is None or entry.get("reason") != reason:
                entry = {"first_seen": now.timestamp(), "notified": False, "reason": reason}
            entry = {**entry, "subject": rel.subject, "release": rel.release}
            grace = max(base_grace, WAITING_GRACE_SECONDS) if reason == "waiting" else base_grace
            if not entry["notified"] and now.timestamp() - entry["first_seen"] >= grace:
                entry["notified"] = True
                raised.append((rel, reason))
            fresh[key] = entry
        resolved = [e for k, e in known.items() if k not in current and e.get("notified")]
        if fresh:
            state.media_attention[app] = fresh
        else:
            state.media_attention.pop(app, None)

        out: list[Notification] = []
        urgent = self.cfg.media_mention
        if len(raised) > BURST_THRESHOLD:
            out.append(Notification("media_attention", formatting.attention_burst_embed(inst, raised),
                                    f"{len(raised)} {inst.name} downloads need attention", urgent=urgent))
        else:
            for rel, reason in sorted(raised, key=lambda rr: (-REASON_RANK[rr[1]], rr[0].subject)):
                out.append(Notification("media_attention", formatting.attention_embed(inst, rel, reason),
                                        f"{inst.name}: {formatting.REASON_TEXT[reason][1]} — {rel.subject}",
                                        urgent=urgent))
        if resolved:
            out.append(Notification("media_resolved", formatting.attention_resolved_embed(inst, resolved),
                                    f"{inst.name}: {len(resolved)} download(s) sorted"))
        return out

    # ------------------------------------------------------------------- health

    def _diff_health(self, inst: ArrInstance, state: State, health: list[HealthIssue]) -> list[Notification]:
        app = inst.kind
        ignore = self.cfg.media_health_ignore
        current = {i.source: i for i in health if i.type in HEALTH_TYPES and i.source not in ignore}
        known = state.media_health.get(app)
        state.media_health[app] = {s: {"type": i.type, "message": i.message} for s, i in current.items()}
        if known is None:
            log.info("%s health baseline recorded: %d issue(s)", inst.name, len(current))
            return []
        out: list[Notification] = []
        for source, issue in current.items():
            if source not in known:
                out.append(Notification("media_health", formatting.arr_health_embed(inst, issue),
                                        f"{inst.name}: {issue.message}", urgent=self._urgent(issue.level)))
        for source, old in known.items():
            if source not in current:
                gone = HealthIssue(app, source, old.get("type", "warning"), old.get("message", source))
                out.append(Notification("media_health_cleared",
                                        formatting.arr_health_embed(inst, gone, cleared=True),
                                        f"{inst.name} resolved: {gone.message}"))
        return out

    # ------------------------------------------------------------------ imports

    async def _imports(self, client: ArrClient, state: State, now: datetime) -> list[Notification]:
        app = client.kind
        raw = state.media_import_cursor.get(app)
        if raw is None:
            state.media_import_cursor[app] = now.isoformat()  # start from now; never replay old history
            return []
        try:
            since = datetime.fromisoformat(raw)
            events = await client.imports_since(since)
        except (ArrUnavailable, ArrCallError, ValueError) as exc:
            log.warning("Could not read %s import history: %s", client.name, exc)
            return []
        if not events:
            return []
        stamps = [e.when for e in events if e.when]
        if stamps:
            state.media_import_cursor[app] = max(stamps).isoformat()
        groups: dict[str, list[ImportEvent]] = {}
        for e in sorted(events, key=lambda ev: ev.when or now):
            groups.setdefault(e.group_key, []).append(e)
        batches = list(groups.values())
        inst = client.instance
        if len(batches) > BURST_THRESHOLD:
            return [Notification("media_imported", formatting.imported_burst_embed(inst, batches),
                                 f"{inst.name}: {len(batches)} downloads ready")]
        return [Notification("media_imported", formatting.imported_embed(inst, b),
                             f"{inst.name}: ready — {b[0].subject}") for b in batches]

    # ---------------------------------------------------------------- commands

    async def overview(self) -> list[AppOverview]:
        async def one(client: ArrClient) -> AppOverview:
            ov = AppOverview(client.instance)
            try:
                queue, health = await asyncio.gather(client.queue(), client.health())
            except (ArrUnavailable, ArrCallError) as exc:
                ov.error = str(exc)
                return ov
            ov.releases = [(r, release_reason(r, watch_stalled=True)) for r in group_releases(queue)]
            ov.health = [h for h in health if h.type in HEALTH_TYPES]
            return ov

        return list(await asyncio.gather(*(one(c) for c in self.clients)))

    def _urgent(self, level: str) -> bool:
        return level_rank(level) >= level_rank(self.cfg.mention_min_level)
