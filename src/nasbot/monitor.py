"""The alert engine: polls TrueNAS, diffs against persisted state, and emits
Notifications for the bot to post. No Discord I/O happens here, which keeps
every rule unit-testable with a fake API and a fake clock.

What it reports:
  * TrueNAS alerts appearing / resolving / being dismissed (level-filtered)
  * the NAS becoming unreachable, and coming back (with downtime)
  * reboots (boot time moved)
  * apps crashing, stopping, or recovering — TrueNAS raises no alert for these
  * an optional daily digest
and it can be muted for a maintenance window, summarizing what it held back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord

from . import collectors, formatting
from .config import Config, level_rank
from .models import APP_STABLE_STATES, Alert, App, SystemInfo
from .state import KnownAlert, State, StateStore
from .truenas import NASCallError, NASUnavailable

log = logging.getLogger(__name__)

BURST_THRESHOLD = 6  # more new/cleared alerts than this in one poll get bundled into one message
REBOOT_TOLERANCE = 120.0  # seconds of boot-time jitter to ignore


@dataclass
class Notification:
    kind: str
    embed: discord.Embed
    summary: str
    urgent: bool = False


class Monitor:
    def __init__(self, api: collectors.API, cfg: Config, store: StateStore,
                 *, now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.api = api
        self.cfg = cfg
        self.store = store
        self.now = now
        self.state: State = store.load()
        self.last_poll_ok: bool | None = None
        self.last_poll_at: datetime | None = None

    # ------------------------------------------------------------------ public

    @property
    def hostname(self) -> str:
        return self.state.hostname or "TrueNAS"

    @property
    def silenced_until(self) -> datetime | None:
        su = self.state.silence_until
        if su is None or su <= self.now().timestamp():
            return None
        return datetime.fromtimestamp(su, tz=UTC)

    def set_silence(self, minutes: int) -> list[Notification]:
        """Mute for `minutes`; 0 unmutes immediately and returns the summary to post."""
        if minutes <= 0:
            out = self._end_silence() if self.state.silence_until is not None else []
            self.store.save(self.state)
            return out
        self.state.silence_until = (self.now() + timedelta(minutes=minutes)).timestamp()
        self.store.save(self.state)
        return []

    async def poll(self) -> list[Notification]:
        now = self.now()
        self.last_poll_at = now
        events: list[Notification] = []
        try:
            info = await collectors.system_info(self.api)
            alerts = await collectors.alerts(self.api)
        except NASUnavailable as exc:
            self.last_poll_ok = False
            events += self._on_failure(now, str(exc))
        except NASCallError as exc:
            # Reachable, but this API user can't read system/alerts: a setup problem, not an outage.
            self.last_poll_ok = True
            log.error("Polling failed with an API error (check the API user's role): %s", exc)
            self._reset_failures()
        else:
            self.last_poll_ok = True
            events += self._on_success(now, info)
            events += self._diff_alerts(alerts)
            if self.cfg.watch_apps:
                events += await self._check_apps()
            events += await self._maybe_digest(now)

        events = self._apply_silence(now, events)
        self.store.save(self.state)
        for n in events:
            log.info("Notification [%s]%s: %s", n.kind, " (urgent)" if n.urgent else "", n.summary)
        return events

    # -------------------------------------------------------- reachability

    def _on_failure(self, now: datetime, error: str) -> list[Notification]:
        s = self.state
        s.consecutive_failures += 1
        if s.first_failure_at is None:
            s.first_failure_at = now.timestamp()
        log.warning("Poll failed (%d/%d): %s", s.consecutive_failures, self.cfg.unreachable_after, error)
        if s.consecutive_failures >= self.cfg.unreachable_after and s.down_since is None:
            s.down_since = s.first_failure_at
            since = datetime.fromtimestamp(s.down_since, tz=UTC)
            return [Notification("nas_down", formatting.nas_down_embed(self.hostname, since, error),
                                 f"{self.hostname} unreachable", urgent=True)]
        return []

    def _reset_failures(self) -> None:
        self.state.consecutive_failures = 0
        self.state.first_failure_at = None

    def _on_success(self, now: datetime, info: SystemInfo) -> list[Notification]:
        s = self.state
        out: list[Notification] = []
        s.hostname = info.hostname
        new_boot = info.boottime.timestamp() if info.boottime else None
        rebooted = (s.boottime is not None and new_boot is not None
                    and abs(new_boot - s.boottime) > REBOOT_TOLERANCE)

        if s.down_since is not None:
            downtime = now.timestamp() - s.down_since
            out.append(Notification("nas_up", formatting.nas_up_embed(info.hostname, downtime, rebooted),
                                    f"{info.hostname} reachable again", urgent=False))
        elif rebooted:
            out.append(Notification("rebooted", formatting.rebooted_embed(info.hostname, info.boottime, info.version),
                                    f"{info.hostname} rebooted"))
        s.down_since = None
        self._reset_failures()
        if new_boot is not None:
            s.boottime = new_boot
        return out

    # --------------------------------------------------------------- alerts

    def _eligible(self, alert: Alert) -> bool:
        return (not alert.dismissed
                and alert.rank >= level_rank(self.cfg.min_alert_level)
                and alert.klass not in self.cfg.ignore_alert_classes)

    def _urgent(self, level: str) -> bool:
        return level_rank(level) >= level_rank(self.cfg.mention_min_level)

    def _diff_alerts(self, alerts: list[Alert]) -> list[Notification]:
        current = {a.uuid: a for a in alerts if self._eligible(a)}
        by_uuid = {a.uuid: a for a in alerts}
        known = self.state.known_alerts
        self.state.known_alerts = {u: KnownAlert(a.title, a.level, a.klass) for u, a in current.items()}

        if known is None:
            log.info("Alert baseline recorded: %d active alert(s)", len(current))
            return []

        host = self.hostname
        new = sorted((a for u, a in current.items() if u not in known), key=lambda a: -a.rank)
        gone = [(u, k) for u, k in known.items() if u not in current]
        out: list[Notification] = []

        if len(new) > BURST_THRESHOLD:
            out.append(Notification("alert_burst", formatting.alert_burst_embed(new, host),
                                    f"{len(new)} new alerts", urgent=any(self._urgent(a.level) for a in new)))
        else:
            for a in new:
                out.append(Notification("alert_raised", formatting.alert_raised_embed(a, host),
                                        f"{a.level.title()}: {a.title}", urgent=self._urgent(a.level)))

        if len(gone) > BURST_THRESHOLD:
            embed = discord.Embed(title=f"✅ {len(gone)} alerts cleared on {host}", color=formatting.COLOR_OK,
                                  description=formatting.truncate("\n".join(f"• {k.title}" for _, k in gone), 4000))
            out.append(Notification("alert_cleared", embed, f"{len(gone)} alerts cleared"))
        else:
            for u, k in gone:
                dismissed = u in by_uuid and by_uuid[u].dismissed
                out.append(Notification(
                    "alert_cleared",
                    formatting.alert_cleared_embed(k.title, k.level, k.klass, host, dismissed=dismissed),
                    f"{'Dismissed' if dismissed else 'Resolved'}: {k.title}",
                ))
        return out

    # ----------------------------------------------------------------- apps

    async def _check_apps(self) -> list[Notification]:
        try:
            apps = await collectors.apps(self.api)
        except (NASCallError, NASUnavailable) as exc:
            log.debug("Skipping app check: %s", exc)
            return []
        return self._diff_apps(apps)

    def _diff_apps(self, apps: list[App]) -> list[Notification]:
        prev = self.state.app_states
        present = {a.name for a in apps}
        stable = {a.name: a.state for a in apps if a.state in APP_STABLE_STATES}
        # Keep the last stable state of apps that are mid-transition (DEPLOYING/STOPPING),
        # forget apps that were deleted.
        merged = {n: s for n, s in (prev or {}).items() if n in present}
        merged.update(stable)
        self.state.app_states = merged
        if prev is None:
            return []

        out = []
        for app in apps:
            old = prev.get(app.name)
            if old is None or app.state not in APP_STABLE_STATES or old == app.state:
                continue
            urgent = app.state == "CRASHED" and self._urgent("ERROR")
            out.append(Notification("app_state", formatting.app_state_embed(app, old, self.hostname),
                                    f"App {app.name}: {old.lower()} → {app.state.lower()}", urgent=urgent))
        return out

    # --------------------------------------------------------------- digest

    async def _maybe_digest(self, now: datetime) -> list[Notification]:
        at = self.cfg.digest_time
        if at is None:
            return []
        local = now.astimezone(self.cfg.tz)
        today = local.date().isoformat()
        if self.state.last_digest_date is None:
            # First run: don't fire immediately just because we started after the digest time.
            due_today = local.time() < at
            self.state.last_digest_date = (local.date() - timedelta(days=1)).isoformat() if due_today else today
            return []
        if local.time() < at or self.state.last_digest_date == today:
            return []
        try:
            snap = await collectors.snapshot(self.api)
        except (NASUnavailable, NASCallError) as exc:
            log.warning("Digest skipped, will retry next poll: %s", exc)
            return []
        self.state.last_digest_date = today
        return [Notification("digest", formatting.status_embed(snap, title_prefix="Daily digest · "),
                             "daily digest")]

    # -------------------------------------------------------------- silence

    def _end_silence(self) -> list[Notification]:
        suppressed = list(self.state.suppressed)
        self.state.silence_until = None
        self.state.suppressed = []
        return [Notification("silence_ended", formatting.silence_summary_embed(suppressed),
                             f"unmuted ({len(suppressed)} suppressed)")]

    def _apply_silence(self, now: datetime, events: list[Notification]) -> list[Notification]:
        until = self.state.silence_until
        if until is None:
            return events
        if now.timestamp() < until:
            self.state.suppressed.extend(n.summary for n in events)
            del self.state.suppressed[:-100]  # keep the newest 100 only
            return []
        return self._end_silence() + events
