"""Fetch and normalize data from TrueNAS. Pure glue between API and models."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import Alert, App, Disk, Pool, Realtime, Snapshot, SystemInfo
from .truenas import NASCallError, NASUnavailable


class API(Protocol):
    async def call(self, method: str, *params: Any, timeout: float | None = None) -> Any: ...
    def realtime(self) -> Realtime | None: ...


async def system_info(api: API) -> SystemInfo:
    return SystemInfo.parse(await api.call("system.info"))


async def pools(api: API) -> list[Pool]:
    rows = await api.call("pool.query")
    return sorted((Pool.parse(r) for r in rows), key=lambda p: p.name)


async def boot_pool(api: API) -> Pool:
    return Pool.parse(await api.call("boot.get_state"), default_name="boot-pool")


async def alerts(api: API) -> list[Alert]:
    rows = await api.call("alert.list")
    return sorted((Alert.parse(r) for r in rows),
                  key=lambda a: (-a.rank, -(a.when.timestamp() if a.when else 0)))


async def apps(api: API) -> list[App]:
    rows = await api.call("app.query")
    return sorted((App.parse(r) for r in rows), key=lambda a: a.name.lower())


async def disks(api: API) -> list[Disk]:
    rows, temps = await asyncio.gather(
        api.call("disk.query", [], {"extra": {"pools": True}}),
        api.call("disk.temperatures"),
    )
    temps = temps if isinstance(temps, dict) else {}
    return sorted((Disk.parse(r, temps) for r in rows), key=lambda d: (d.pool or "~", d.name))


async def snapshot(api: API, *, with_disks: bool = False) -> Snapshot:
    """Gather everything at once. system.info is mandatory (it doubles as the
    reachability check); every other section degrades to None + an error note."""
    info = await system_info(api)
    sections: dict[str, Any] = {
        "pools": pools(api),
        "boot_pool": boot_pool(api),
        "alerts": alerts(api),
        "apps": apps(api),
    }
    if with_disks:
        sections["disks"] = disks(api)
    results = await asyncio.gather(*sections.values(), return_exceptions=True)

    snap = Snapshot(system=info, fetched_at=datetime.now(UTC), realtime=api.realtime())
    for name, result in zip(sections, results, strict=True):
        if isinstance(result, NASUnavailable):
            raise result
        if isinstance(result, BaseException):
            snap.errors[name] = _short_error(result)
        else:
            setattr(snap, name, result)
    return snap


def _short_error(exc: BaseException) -> str:
    msg = str(exc)
    if isinstance(exc, NASCallError) and ("Not authorized" in msg or "EACCES" in msg):
        return "permission denied for this API user"
    if "app" in msg.lower() and ("not configured" in msg.lower() or "docker" in msg.lower()):
        return "apps service not configured"
    return msg if len(msg) <= 160 else msg[:157] + "…"
