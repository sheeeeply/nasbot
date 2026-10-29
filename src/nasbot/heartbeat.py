"""Liveness signals.

* A heartbeat file touched after every poll — Docker's HEALTHCHECK reads it, so a
  wedged bot gets restarted.
* An optional dead-man's-switch ping (healthchecks.io, Uptime Kuma push, …). This
  is what catches the case the bot itself can't report: the whole box (and the bot
  with it) losing power or network. The external service notices the pings stopped.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)


def touch(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())))
    except OSError as exc:
        log.warning("Could not write heartbeat %s: %s", path, exc)


def is_fresh(path: Path, max_age: float, *, now: float | None = None) -> bool:
    try:
        stamp = float(path.read_text().strip())
    except (OSError, ValueError):
        return False
    return ((now if now is not None else time.time()) - stamp) <= max_age


def ping_url(base: str, ok: bool) -> str:
    """healthchecks.io convention: <url> = success, <url>/fail = failure. Uptime Kuma
    push URLs carry their status as a query parameter instead."""
    if "status=" in base or "/api/push/" in base:
        sep = "&" if "?" in base else "?"
        status = "up" if ok else "down"
        base = base.replace("status=up", f"status={status}").replace("status=down", f"status={status}")
        return base if "status=" in base else f"{base}{sep}status={status}"
    return base if ok else base.rstrip("/") + "/fail"


async def ping(session: aiohttp.ClientSession, base: str, ok: bool) -> bool:
    url = ping_url(base, ok)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status >= 400:
                log.warning("Healthcheck ping got HTTP %s", resp.status)
                return False
            return True
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("Healthcheck ping failed: %s", exc)
        return False
