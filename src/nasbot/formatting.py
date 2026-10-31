"""Discord embed builders. Pure functions: models in, discord.Embed out.

Timestamps use Discord's ``<t:unix:R>`` markup so each viewer sees them in their
own time zone — handy when the NAS is in California and you are in Seoul.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from .arr import HealthIssue, ImportEvent, Release
from .config import LEVELS, ArrInstance, level_rank
from .models import Alert, App, Disk, Pool, Snapshot

if TYPE_CHECKING:
    from .media import AppOverview

LEVELS_BY_RANK = dict(enumerate(LEVELS))

COLOR_OK = 0x2ECC71
COLOR_WARN = 0xF1C40F
COLOR_ERROR = 0xE67E22
COLOR_CRIT = 0xE74C3C
COLOR_INFO = 0x5865F2
COLOR_MUTED = 0x95A5A6

HEALTH_STYLE = {
    "OK": ("🟢", "healthy", COLOR_OK),
    "WARN": ("🟠", "needs attention", COLOR_WARN),
    "CRIT": ("🔴", "critical", COLOR_CRIT),
}

# Embed hard limits (https://discord.com/developers/docs/resources/message#embed-object-embed-limits)
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024
MAX_FIELDS = 25


# ----------------------------------------------------------------- primitives

def human_bytes(n: float | None, *, precision: int = 1) -> str:
    if n is None:
        return "?"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(n)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.{precision}f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{s}s"


def bar(fraction: float | None, width: int = 10) -> str:
    if fraction is None:
        return "▱" * width
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "▰" * filled + "▱" * (width - filled)


def ts(dt: datetime | None, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "unknown"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def level_emoji(level: str) -> str:
    rank = level_rank(level)
    if rank >= level_rank("CRITICAL"):
        return "🔴"
    if rank >= level_rank("ERROR"):
        return "🟠"
    if rank >= level_rank("WARNING"):
        return "🟡"
    return "🔵"


def level_color(level: str) -> int:
    rank = level_rank(level)
    if rank >= level_rank("CRITICAL"):
        return COLOR_CRIT
    if rank >= level_rank("ERROR"):
        return COLOR_ERROR
    if rank >= level_rank("WARNING"):
        return COLOR_WARN
    return COLOR_INFO


def pool_emoji(pool: Pool) -> str:
    if pool.ok and not pool.warning:
        return "🟢"
    if pool.status.upper() in ("FAULTED", "UNAVAIL", "SUSPENDED", "REMOVED", "OFFLINE"):
        return "🔴"
    return "🟠"


def _join_lines(lines: list[str], limit: int) -> str:
    """Join lines, dropping whole lines (with a '+N more' note) instead of cutting mid-line."""
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        extra = len(line) + (1 if out else 0)
        remaining = len(lines) - i
        reserve = len(f"\n…and {remaining} more")
        if used + extra > limit - (reserve if remaining > 1 else 0):
            out.append(f"…and {remaining} more")
            break
        out.append(line)
        used += extra
    return "\n".join(out) or "—"


# ---------------------------------------------------------------- line items

def pool_line(pool: Pool) -> str:
    frac = pool.used_fraction
    pct = f"{frac * 100:.0f}%" if frac is not None else "?"
    line = f"{pool_emoji(pool)} **{pool.name}** `{pool.status}` {bar(frac)} {pct} · {human_bytes(pool.free)} free"
    if pool.scan and pool.scan.running:
        pct_done = f"{pool.scan.percentage:.0f}%" if pool.scan.percentage is not None else "…"
        line += f"\n　↳ {pool.scan.function.lower()} in progress {pct_done}"
        if pool.scan.seconds_left:
            line += f", ~{human_duration(pool.scan.seconds_left)} left"
    return line


def scan_summary(pool: Pool) -> str:
    scan = pool.scan
    if scan is None:
        return "never"
    if scan.running:
        pct = f"{scan.percentage:.1f}%" if scan.percentage is not None else "…"
        left = f", ~{human_duration(scan.seconds_left)} left" if scan.seconds_left else ""
        return f"{scan.function.title()} running — {pct}{left}"
    errs = f", {scan.errors} error(s)" if scan.errors else ", 0 errors"
    when = ts(scan.end_time) if scan.end_time else "unknown time"
    return f"Last {(scan.function or 'scan').lower()} {(scan.state or '').lower()} {when}{errs}"


def alert_line(alert: Alert, *, with_time: bool = True) -> str:
    when = f" · {ts(alert.when)}" if with_time and alert.when else ""
    return f"{level_emoji(alert.level)} **{alert.level.title()}**{when} — {alert.title}"


# ------------------------------------------------------------- command embeds

def status_embed(snap: Snapshot, *, title_prefix: str = "") -> discord.Embed:
    sys = snap.system
    icon, word, color = HEALTH_STYLE[snap.health]
    embed = discord.Embed(
        title=f"{title_prefix}{icon} {sys.hostname} — {word}",
        description=(f"TrueNAS **{sys.version}** · up {human_duration(sys.uptime_seconds)}"
                     + (f" (since {ts(sys.boottime, 'f')})" if sys.boottime else "")),
        color=color,
        timestamp=snap.fetched_at,
    )

    rt = snap.realtime
    cpu_bits = []
    if rt and rt.cpu_percent is not None:
        cpu_bits.append(f"{rt.cpu_percent:.0f}%")
    if rt and rt.cpu_temp_c is not None:
        cpu_bits.append(f"{rt.cpu_temp_c:.0f}°C")
    cpu_value = " · ".join(cpu_bits) or "—"
    if sys.loadavg:
        cpu_value += "\nload " + " ".join(f"{x:.2f}" for x in sys.loadavg[:3])
    embed.add_field(name="CPU", value=cpu_value, inline=True)

    if rt and rt.mem_used_fraction is not None:
        mem_value = f"{bar(rt.mem_used_fraction, 8)} {rt.mem_used_fraction * 100:.0f}%\nof {human_bytes(rt.mem_total)}"
        if rt.arc_size:
            mem_value += f" · ARC {human_bytes(rt.arc_size)}"
    else:
        mem_value = human_bytes(sys.physmem) + " installed"
    embed.add_field(name="Memory", value=mem_value, inline=True)

    if snap.apps is not None:
        counts = Counter(a.state for a in snap.apps)
        apps_value = f"{counts.get('RUNNING', 0)} running · {counts.get('STOPPED', 0)} stopped"
        crashed = [a.name for a in snap.apps if a.state == "CRASHED"]
        if crashed:
            apps_value += f"\n🔴 crashed: {', '.join(crashed)}"
        embed.add_field(name=f"Apps ({len(snap.apps)})", value=truncate(apps_value, MAX_FIELD_VALUE), inline=True)

    if snap.pools is not None:
        pool_lines = [pool_line(p) for p in snap.pools] or ["No data pools"]
        if snap.boot_pool:
            bp = snap.boot_pool
            pool_lines.append(f"{pool_emoji(bp)} boot `{bp.status}`")
        embed.add_field(name="Storage", value=_join_lines(pool_lines, MAX_FIELD_VALUE), inline=False)

    if snap.alerts is not None:
        active = snap.active_alerts
        if active:
            by_level = Counter(a.level.title() for a in active)
            head = ", ".join(f"{n} {lvl.lower()}" for lvl, n in by_level.most_common())
            lines = [head] + [alert_line(a, with_time=False) for a in active[:5]]
            if len(active) > 5:
                lines.append(f"…use `/nas alerts` for all {len(active)}")
            embed.add_field(name="Alerts", value=_join_lines(lines, MAX_FIELD_VALUE), inline=False)
        else:
            embed.add_field(name="Alerts", value="✅ None", inline=False)

    _add_errors(embed, snap.errors)
    embed.set_footer(text="Fetched")
    return embed


def pools_embed(snap: Snapshot) -> discord.Embed:
    all_pools = list(snap.pools or []) + ([snap.boot_pool] if snap.boot_pool else [])
    worst = "CRIT" if any(pool_emoji(p) == "🔴" for p in all_pools) else \
        "WARN" if any(pool_emoji(p) == "🟠" for p in all_pools) else "OK"
    embed = discord.Embed(title=f"Storage pools — {snap.system.hostname}", color=HEALTH_STYLE[worst][2],
                          timestamp=snap.fetched_at)
    if snap.pools is None:
        embed.description = f"⚠️ Could not read pools: {snap.errors.get('pools', 'unknown error')}"
    for pool in all_pools[:MAX_FIELDS]:
        frac = pool.used_fraction
        lines = [
            f"Status: `{pool.status}`" + (" ⚠️ warning" if pool.warning else ""),
            f"{bar(frac)} {frac * 100:.1f}% used" if frac is not None else "Usage unknown",
            f"{human_bytes(pool.allocated)} used / {human_bytes(pool.size)} · {human_bytes(pool.free)} free",
        ]
        if pool.fragmentation:
            lines.append(f"Fragmentation: {pool.fragmentation}%")
        lines.append(scan_summary(pool))
        if pool.status_detail and not pool.ok:
            lines.append(f"> {truncate(pool.status_detail, 300)}")
        embed.add_field(name=f"{pool_emoji(pool)} {pool.name}", value=truncate("\n".join(lines), MAX_FIELD_VALUE),
                        inline=False)
    if not all_pools and snap.pools is not None:
        embed.description = "No pools found."
    return embed


def disks_embed(disks: list[Disk], hostname: str, fetched_at: datetime) -> discord.Embed:
    temps = [d.temp_c for d in disks if d.temp_c is not None]
    hottest = max(temps) if temps else None
    color = COLOR_OK if hottest is None or hottest < 45 else COLOR_WARN if hottest < 55 else COLOR_CRIT
    embed = discord.Embed(title=f"Disks — {hostname}", color=color, timestamp=fetched_at)
    if not disks:
        embed.description = "No disks reported."
        return embed

    def temp(d: Disk) -> str:
        if d.temp_c is None:
            return "  —"
        flag = "!" if d.temp_c >= 50 else " "
        return f"{d.temp_c:3.0f}°{flag}"

    rows = [f"{'disk':<9}{'pool':<11}{'temp':>6}  {'size':>9}  model"]
    for d in disks:
        model = truncate((d.model or "?").strip(), 22)
        pool = truncate(d.pool or "—", 10)
        rows.append(f"{d.name:<9}{pool:<11}{temp(d):>6}  {human_bytes(d.size, precision=1):>9}  {model}")
    table = "\n".join(rows)
    embed.description = "```\n" + truncate(table, MAX_DESCRIPTION - 8) + "\n```"
    if temps:
        embed.set_footer(text=f"{len(disks)} disks · avg {sum(temps) / len(temps):.0f}°C · max {hottest:.0f}°C "
                              "· TrueNAS caches temps for 5 min")
    return embed


def alerts_embed(alerts: list[Alert], hostname: str, *, include_dismissed: bool = False) -> discord.Embed:
    shown = [a for a in alerts if include_dismissed or not a.dismissed]
    if not shown:
        return discord.Embed(title=f"Alerts — {hostname}", description="✅ No active alerts.", color=COLOR_OK)
    top = max(shown, key=lambda a: a.rank)
    embed = discord.Embed(title=f"Alerts — {hostname} ({len(shown)})", color=level_color(top.level))
    blocks = []
    for a in shown:
        dismissed = " *(dismissed)*" if a.dismissed else ""
        body = truncate(a.text, 350).replace("\n", "\n> ")
        blocks.append(f"{level_emoji(a.level)} **{a.level.title()}** · {ts(a.when)}{dismissed}\n> {body}")
    embed.description = _join_lines(blocks, MAX_DESCRIPTION)
    return embed


def apps_embed(apps: list[App], hostname: str) -> discord.Embed:
    crashed = any(a.state == "CRASHED" for a in apps)
    embed = discord.Embed(title=f"Apps — {hostname} ({len(apps)})", color=COLOR_WARN if crashed else COLOR_OK)
    if not apps:
        embed.description = "No apps installed."
        return embed
    icon = {"RUNNING": "🟢", "STOPPED": "⚪", "CRASHED": "🔴", "DEPLOYING": "🔵", "STOPPING": "🔵"}
    lines = []
    for a in apps:
        extras = []
        if a.upgrade_available:
            extras.append("⬆️ update")
        elif a.image_updates_available:
            extras.append("⬆️ image")
        version = f" `{a.version}`" if a.version else ""
        lines.append(f"{icon.get(a.state, '❔')} **{a.name}**{version} — {a.state.lower()}"
                     + (f" · {' '.join(extras)}" if extras else ""))
    embed.description = _join_lines(lines, MAX_DESCRIPTION)
    return embed


def _add_errors(embed: discord.Embed, errors: dict[str, str]) -> None:
    if errors and len(embed.fields) < MAX_FIELDS:
        text = "\n".join(f"• {k}: {v}" for k, v in errors.items())
        embed.add_field(name="⚠️ Partial data", value=truncate(text, MAX_FIELD_VALUE), inline=False)


# -------------------------------------------------------- notification embeds

def alert_raised_embed(alert: Alert, hostname: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{level_emoji(alert.level)} {alert.level.title()}: {truncate(alert.title, 230)}",
        description=truncate(alert.text, MAX_DESCRIPTION),
        color=level_color(alert.level),
        timestamp=alert.when,
    )
    embed.set_footer(text=f"{hostname} · {alert.klass}")
    return embed


def alert_cleared_embed(title: str, level: str, klass: str, hostname: str, *, dismissed: bool) -> discord.Embed:
    verb = "Dismissed" if dismissed else "Resolved"
    embed = discord.Embed(title=f"✅ {verb}: {truncate(title, 230)}", color=COLOR_MUTED if dismissed else COLOR_OK)
    embed.set_footer(text=f"{hostname} · {klass} · was {level.title()}")
    return embed


def alert_burst_embed(alerts: list[Alert], hostname: str) -> discord.Embed:
    top = max(alerts, key=lambda a: a.rank)
    embed = discord.Embed(title=f"{level_emoji(top.level)} {len(alerts)} new alerts on {hostname}",
                          color=level_color(top.level))
    embed.description = _join_lines([alert_line(a) for a in alerts], MAX_DESCRIPTION)
    return embed


def nas_down_embed(hostname: str, since: datetime, error: str | None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔴 {hostname} is unreachable",
        description=f"No response since {ts(since)}." + (f"\n```{truncate(error, 500)}```" if error else ""),
        color=COLOR_CRIT,
    )
    embed.set_footer(text="I'll post again when it's back.")
    return embed


def nas_up_embed(hostname: str, downtime_seconds: float, rebooted: bool) -> discord.Embed:
    extra = " The system rebooted while it was away." if rebooted else ""
    return discord.Embed(
        title=f"🟢 {hostname} is reachable again",
        description=f"Unreachable for about {human_duration(downtime_seconds)}.{extra}",
        color=COLOR_OK,
    )


def rebooted_embed(hostname: str, boottime: datetime | None, version: str) -> discord.Embed:
    return discord.Embed(
        title=f"🔁 {hostname} rebooted",
        description=f"Booted {ts(boottime)} · running TrueNAS {version}",
        color=COLOR_INFO,
    )


def app_state_embed(app: App, old_state: str, hostname: str) -> discord.Embed:
    if app.state == "CRASHED":
        title, color = f"🔴 App crashed: {app.name}", COLOR_CRIT
    elif app.state == "RUNNING":
        title, color = f"🟢 App running again: {app.name}", COLOR_OK
    else:
        title, color = f"⚪ App stopped: {app.name}", COLOR_WARN
    embed = discord.Embed(title=title, description=f"`{old_state.lower()}` → `{app.state.lower()}`", color=color)
    embed.set_footer(text=hostname)
    return embed


def silence_summary_embed(suppressed: list[str]) -> discord.Embed:
    if not suppressed:
        return discord.Embed(title="🔔 Notifications resumed", description="Nothing happened while muted.",
                             color=COLOR_INFO)
    return discord.Embed(
        title=f"🔔 Notifications resumed — {len(suppressed)} suppressed while muted",
        description=_join_lines([f"• {s}" for s in suppressed], MAX_DESCRIPTION),
        color=COLOR_INFO,
    )


# ------------------------------------------------------------ media (Sonarr/Radarr)

# reason -> (icon, short headline, what to do — {app} is replaced with Sonarr/Radarr)
REASON_TEXT: dict[str, tuple[str, str, str]] = {
    "import": ("📥", "needs manual import",
               "Open **{app} → Activity → Queue**, click the 👤 *Manual Import* icon on this release, fix any "
               "red rows (pick the right episode/movie), then **Import**."),
    "failed": ("❌", "download failed",
               "{app} normally blocklists a failed release and searches for another one by itself. If nothing new "
               "shows up in **Activity → Queue**, run a search for it manually."),
    "waiting": ("⏳", "still waiting to import",
                "{app} has held this finished download for over two days. Holds are normally temporary (a new "
                "episode whose title is still *TBA*, an archive still being extracted). Hover the icon in "
                "**{app} → Activity → Queue** for the reason; *Manual Import* (👤) imports it now."),
    "stalled": ("⏸️", "download stalled",
                "No data is arriving (no seeders). Cleanuparr replaces stalled downloads automatically if it's set "
                "up; otherwise remove it in **{app} → Activity → Queue** with *Blocklist and Search*."),
}
REASON_COLOR = {"import": COLOR_WARN, "failed": COLOR_ERROR, "stalled": COLOR_MUTED, "waiting": COLOR_MUTED}


def episode_ranges(episodes: list[tuple[int, int]], *, limit: int = 300) -> str:
    """[(1,4),(1,6),(1,7),(1,8),(2,1)] -> 'S01E04, E06–E08 · S02E01' (consecutive runs collapsed)."""
    by_season: dict[int, list[int]] = {}
    for season, ep in sorted(set(episodes)):
        by_season.setdefault(season, []).append(ep)
    parts = []
    for season, eps in by_season.items():
        runs: list[tuple[int, int]] = []
        for e in eps:
            if runs and e == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], e)
            else:
                runs.append((e, e))
        width = max(2, len(str(eps[-1])))
        text = ", ".join(f"E{a:0{width}d}" if a == b else f"E{a:0{width}d}–E{b:0{width}d}" for a, b in runs)
        parts.append(f"S{season:02d}{text}")
    return truncate(" · ".join(parts), limit)


def _episodes_line(episodes: list[tuple[int, int]]) -> str | None:
    if not episodes:
        return None
    n = len(episodes)
    return f"{episode_ranges(episodes)} ({n} episode{'s' if n != 1 else ''})"


def _queue_link(inst: ArrInstance) -> str:
    return f"{inst.web_url}/activity/queue"


def attention_embed(inst: ArrInstance, rel: Release, reason: str) -> discord.Embed:
    icon, headline, fix = REASON_TEXT[reason]
    embed = discord.Embed(
        title=truncate(f"{icon} {headline[0].upper()}{headline[1:]}: {rel.subject}", 256),
        url=_queue_link(inst),
        description=f"`{truncate(rel.release, 400)}`\n\n**What to do:** {fix.format(app=inst.name)}",
        color=REASON_COLOR[reason],
    )
    if (eps := _episodes_line(rel.episodes)) is not None:
        embed.add_field(name="Episodes", value=truncate(eps, MAX_FIELD_VALUE), inline=False)
    msgs = rel.messages
    if msgs:
        lines = [f"• {truncate(m, 200)}" + (f" — {n} files" if n > 1 else "") for m, n in msgs[:4]]
        if len(msgs) > 4:
            lines.append(f"…and {len(msgs) - 4} more")
        embed.add_field(name=f"What {inst.name} says", value=truncate("\n".join(lines), MAX_FIELD_VALUE),
                        inline=False)
    size_bits = []
    if rel.size:
        size_bits.append(human_bytes(rel.size))
    if reason == "stalled" and rel.progress is not None:
        size_bits.append(f"{bar(rel.progress, 8)} {rel.progress * 100:.0f}%")
    if size_bits:
        embed.add_field(name="Size", value=" · ".join(size_bits), inline=True)
    client = rel.first.client
    embed.set_footer(text=f"{inst.name}" + (f" · {client}" if client else ""))
    return embed


def attention_burst_embed(inst: ArrInstance, raised: list[tuple[Release, str]]) -> discord.Embed:
    lines = []
    for rel, reason in raised:
        icon, headline, _ = REASON_TEXT[reason]
        eps = f" · {len(rel.episodes)} ep" if rel.episodes else ""
        lines.append(f"{icon} **{truncate(rel.subject, 80)}** — {headline}{eps}")
    worst = max((r for _, r in raised), key=REASON_RANK_FMT.__getitem__)
    embed = discord.Embed(title=f"⚠️ {len(raised)} {inst.name} downloads need attention", url=_queue_link(inst),
                          description=_join_lines(lines, MAX_DESCRIPTION - 200)
                          + f"\n\nOpen **{inst.name} → Activity → Queue** to sort them out.",
                          color=REASON_COLOR[worst])
    embed.set_footer(text=inst.name)
    return embed


def attention_resolved_embed(inst: ArrInstance, entries: list[dict]) -> discord.Embed:
    if len(entries) == 1:
        e = entries[0]
        embed = discord.Embed(title=truncate(f"✅ Sorted: {e.get('subject', '?')}", 256),
                              description=f"`{truncate(str(e.get('release', '')), 400)}` no longer needs attention.",
                              color=COLOR_OK)
    else:
        embed = discord.Embed(title=f"✅ {len(entries)} {inst.name} downloads sorted", color=COLOR_OK,
                              description=_join_lines([f"• {e.get('subject', '?')}" for e in entries],
                                                      MAX_DESCRIPTION))
    embed.set_footer(text=inst.name)
    return embed


def arr_health_embed(inst: ArrInstance, issue: HealthIssue, *, cleared: bool = False) -> discord.Embed:
    if cleared:
        embed = discord.Embed(title=truncate(f"✅ {inst.name} resolved: {issue.message}", 256), color=COLOR_OK)
    else:
        embed = discord.Embed(
            title=f"{level_emoji(issue.level)} {inst.name} {issue.type}",
            url=issue.wiki or f"{inst.web_url}/system/status",
            description=truncate(issue.message, MAX_DESCRIPTION - 200)
            + f"\n\nDetails: **{inst.name} → System → Status**.",
            color=level_color(issue.level),
        )
    embed.set_footer(text=f"{inst.name} · {issue.source}")
    return embed


def arr_down_embed(inst: ArrInstance, since: datetime, error: str | None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔴 {inst.name} is unreachable",
        description=f"No answer from `{inst.url}` since {ts(since)}."
        + (f"\n```{truncate(error, 500)}```" if error else "")
        + "\nCheck the container is running (Dockge / Apps) and the URL and API key in nasbot's `.env`.",
        color=COLOR_CRIT,
    )
    embed.set_footer(text="I'll post again when it's back.")
    return embed


def arr_up_embed(inst: ArrInstance, downtime_seconds: float) -> discord.Embed:
    return discord.Embed(title=f"🟢 {inst.name} is reachable again",
                         description=f"Unreachable for about {human_duration(downtime_seconds)}.", color=COLOR_OK)


def _import_summary(batch: list[ImportEvent]) -> str:
    eps = [(e.season, e.episode) for e in batch if e.season is not None and e.episode is not None]
    bits = []
    if eps:
        bits.append(_episodes_line(eps) or "")
    qualities = sorted({e.quality for e in batch if e.quality})
    if qualities:
        bits.append(" / ".join(qualities))
    return " · ".join(b for b in bits if b)


def imported_embed(inst: ArrInstance, batch: list[ImportEvent]) -> discord.Embed:
    icon = "📺" if inst.kind == "sonarr" else "🎬"
    embed = discord.Embed(title=truncate(f"{icon} Ready to watch: {batch[0].subject}", 256),
                          description=_import_summary(batch) or None, color=COLOR_INFO,
                          timestamp=max((e.when for e in batch if e.when), default=None))
    embed.set_footer(text=f"Imported by {inst.name}")
    return embed


def imported_burst_embed(inst: ArrInstance, batches: list[list[ImportEvent]]) -> discord.Embed:
    icon = "📺" if inst.kind == "sonarr" else "🎬"
    lines = [f"{icon} **{truncate(b[0].subject, 80)}**" + (f" — {s}" if (s := _import_summary(b)) else "")
             for b in batches]
    embed = discord.Embed(title=f"{icon} {len(batches)} new downloads ready to watch",
                          description=_join_lines(lines, MAX_DESCRIPTION), color=COLOR_INFO)
    embed.set_footer(text=f"Imported by {inst.name}")
    return embed


def media_queue_embed(overviews: list[AppOverview]) -> discord.Embed:
    needs = sum(1 for ov in overviews for _, r in ov.releases if r in ("import", "failed"))  # not holds/stalls
    errors = any(ov.error for ov in overviews)
    color = COLOR_CRIT if errors else COLOR_WARN if needs else COLOR_OK
    embed = discord.Embed(title="Media queue", color=color)
    for ov in overviews[:MAX_FIELDS]:
        name = ov.instance.name
        if ov.error:
            embed.add_field(name=f"🔴 {name}", value=truncate(ov.error, MAX_FIELD_VALUE), inline=False)
            continue
        if not ov.releases:
            embed.add_field(name=f"🟢 {name}", value="Queue is empty.", inline=False)
            continue
        flagged = [(r, why) for r, why in ov.releases if why]
        active = [r for r, why in ov.releases if not why]
        needs_you = sum(1 for _, why in flagged if why in ("import", "failed"))
        head = f"{len(ov.releases)} download{'s' if len(ov.releases) != 1 else ''}"
        if needs_you:
            head += f" · ⚠️ {needs_you} need{'s' if needs_you == 1 else ''} you"
        lines = [head]
        for rel, why in sorted(flagged, key=lambda rw: -REASON_RANK_FMT[rw[1]]):
            lines.append(f"{REASON_TEXT[why][0]} **{truncate(rel.subject, 60)}** — {QUEUE_LABEL[why]}")
        for rel in sorted(active, key=lambda r: -(r.progress or 0))[:8]:
            pct = f"{rel.progress * 100:.0f}%" if rel.progress is not None else "?"
            eta = f" · done {ts(rel.eta)}" if rel.eta and (rel.progress or 0) < 1 else ""
            state = rel.first.status or "queued"
            lines.append(f"{bar(rel.progress, 8)} {pct} **{truncate(rel.subject, 50)}** `{state}`{eta}")
        if len(active) > 8:
            lines.append(f"…and {len(active) - 8} more")
        icon = "🟠" if needs_you else "🟢"
        embed.add_field(name=f"{icon} {name}", value=_join_lines(lines, MAX_FIELD_VALUE), inline=False)
    embed.set_footer(text="Live from Sonarr/Radarr")
    return embed


REASON_RANK_FMT = {"stalled": 0, "waiting": 1, "import": 2, "failed": 3}
QUEUE_LABEL = {"import": "needs manual import", "failed": "download failed",
               "waiting": "waiting to import (usually a temporary hold)", "stalled": "stalled"}


def media_health_embed(overviews: list[AppOverview]) -> discord.Embed:
    issues = [i for ov in overviews for i in ov.health]
    errors = [ov for ov in overviews if ov.error]
    worst = max((level_rank(i.level) for i in issues), default=-1)
    color = COLOR_CRIT if errors else level_color(LEVELS_BY_RANK[worst]) if issues else COLOR_OK
    embed = discord.Embed(title="Media health", color=color)
    for ov in overviews[:MAX_FIELDS]:
        name = ov.instance.name
        if ov.error:
            embed.add_field(name=f"🔴 {name}", value=truncate(ov.error, MAX_FIELD_VALUE), inline=False)
        elif not ov.health:
            embed.add_field(name=f"🟢 {name}", value="No health issues.", inline=False)
        else:
            lines = [f"{level_emoji(i.level)} {truncate(i.message, 300)}" for i in ov.health]
            embed.add_field(name=f"🟠 {name}", value=_join_lines(lines, MAX_FIELD_VALUE), inline=False)
    return embed
