"""Discord embed builders. Pure functions: models in, discord.Embed out.

Timestamps use Discord's ``<t:unix:R>`` markup so each viewer sees them in their
own time zone — handy when the NAS is in California and you are in Seoul.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import discord

from .config import level_rank
from .models import Alert, App, Disk, Pool, Snapshot

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
