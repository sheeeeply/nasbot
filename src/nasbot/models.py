"""Normalized views of TrueNAS API results.

Parsing is defensive on purpose: field shapes drift between TrueNAS releases
(25.04 -> 25.10 -> 26), and a status bot that crashes on an unexpected key is
worse than one that shows "?".
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import level_rank

POOL_OK_STATES = {"ONLINE"}
APP_STABLE_STATES = {"RUNNING", "STOPPED", "CRASHED"}


def _dt(value: Any) -> datetime | None:
    """Accept datetime (ejson-decoded), {"$date": ms}, epoch seconds/ms, or ISO strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, dict) and "$date" in value:
        return datetime.fromtimestamp(value["$date"] / 1000, tz=UTC)
    if isinstance(value, int | float):
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict) and "parsed" in value:  # some pool properties are {"parsed": n, "rawvalue": ...}
        return _num(value["parsed"])
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def html_to_text(value: str) -> str:
    """TrueNAS 'formatted' alert text is light HTML (<br>, <a>, entities)."""
    text = _BR.sub("\n", value)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


@dataclass(frozen=True)
class SystemInfo:
    hostname: str
    version: str
    uptime_seconds: float
    boottime: datetime | None
    cores: int | None
    physmem: int | None
    loadavg: tuple[float, ...]
    cpu_model: str | None
    ecc_memory: bool | None

    @classmethod
    def parse(cls, raw: dict) -> SystemInfo:
        load = raw.get("loadavg") or []
        return cls(
            hostname=str(raw.get("hostname") or "truenas"),
            version=str(raw.get("version") or "unknown"),
            uptime_seconds=_num(raw.get("uptime_seconds")) or 0.0,
            boottime=_dt(raw.get("boottime")),
            cores=raw.get("cores") if isinstance(raw.get("cores"), int) else None,
            physmem=int(raw["physmem"]) if _num(raw.get("physmem")) else None,
            loadavg=tuple(float(x) for x in load if _num(x) is not None),
            cpu_model=raw.get("model"),
            ecc_memory=raw.get("ecc_memory"),
        )


@dataclass(frozen=True)
class Scan:
    function: str | None  # SCRUB / RESILVER
    state: str | None  # SCANNING / FINISHED / CANCELED
    start_time: datetime | None
    end_time: datetime | None
    percentage: float | None
    errors: int | None
    seconds_left: float | None

    @property
    def running(self) -> bool:
        return (self.state or "").upper() == "SCANNING"

    @classmethod
    def parse(cls, raw: dict | None) -> Scan | None:
        if not raw or not raw.get("function"):
            return None
        errors = _num(raw.get("errors"))
        return cls(
            function=raw.get("function"),
            state=raw.get("state"),
            start_time=_dt(raw.get("start_time")),
            end_time=_dt(raw.get("end_time")),
            percentage=_num(raw.get("percentage")),
            errors=int(errors) if errors is not None else None,
            seconds_left=_num(raw.get("total_secs_left")),
        )


@dataclass(frozen=True)
class Pool:
    name: str
    status: str
    healthy: bool
    warning: bool
    size: int | None
    allocated: int | None
    free: int | None
    fragmentation: str | None
    status_detail: str | None
    scan: Scan | None

    @property
    def used_fraction(self) -> float | None:
        if self.size and self.allocated is not None:
            return self.allocated / self.size
        return None

    @property
    def ok(self) -> bool:
        return self.healthy and self.status.upper() in POOL_OK_STATES

    @classmethod
    def parse(cls, raw: dict, default_name: str = "pool") -> Pool:
        def as_int(key: str) -> int | None:
            v = _num(raw.get(key))
            return int(v) if v is not None else None

        frag = raw.get("fragmentation")
        return cls(
            name=str(raw.get("name") or default_name),
            status=str(raw.get("status") or "UNKNOWN"),
            healthy=bool(raw.get("healthy", False)),
            warning=bool(raw.get("warning", False)),
            size=as_int("size"),
            allocated=as_int("allocated"),
            free=as_int("free"),
            fragmentation=str(frag) if frag not in (None, "") else None,
            status_detail=raw.get("status_detail"),
            scan=Scan.parse(raw.get("scan")),
        )


@dataclass(frozen=True)
class Alert:
    uuid: str
    klass: str
    level: str
    text: str
    when: datetime | None
    dismissed: bool
    source: str

    @property
    def rank(self) -> int:
        return level_rank(self.level)

    @property
    def title(self) -> str:
        """One-line summary used in lists and in 'resolved' messages."""
        first = self.text.splitlines()[0] if self.text else self.klass
        return first if len(first) <= 180 else first[:177] + "…"

    @classmethod
    def parse(cls, raw: dict) -> Alert:
        formatted = raw.get("formatted")
        text = html_to_text(formatted) if formatted else str(raw.get("text") or raw.get("klass") or "")
        return cls(
            uuid=str(raw.get("uuid") or raw.get("id") or raw.get("key")),
            klass=str(raw.get("klass") or "Unknown"),
            level=str(raw.get("level") or "WARNING").upper(),
            text=text,
            when=_dt(raw.get("datetime")),
            dismissed=bool(raw.get("dismissed", False)),
            source=str(raw.get("source") or ""),
        )


@dataclass(frozen=True)
class App:
    name: str
    state: str
    version: str | None
    upgrade_available: bool
    image_updates_available: bool

    @classmethod
    def parse(cls, raw: dict) -> App:
        return cls(
            name=str(raw.get("name") or raw.get("id") or "app"),
            state=str(raw.get("state") or "UNKNOWN").upper(),
            version=raw.get("human_version") or raw.get("version"),
            upgrade_available=bool(raw.get("upgrade_available", False)),
            image_updates_available=bool(raw.get("image_updates_available", False)),
        )


@dataclass(frozen=True)
class Disk:
    name: str
    model: str | None
    serial: str | None
    size: int | None
    kind: str | None  # HDD / SSD
    pool: str | None
    temp_c: float | None

    @classmethod
    def parse(cls, raw: dict, temps: dict[str, Any]) -> Disk:
        name = str(raw.get("name") or raw.get("devname") or "?")
        t = temps.get(name)
        if isinstance(t, list | tuple):  # include_thresholds=True form: (temp, crit)
            t = t[0] if t else None
        size = _num(raw.get("size"))
        return cls(
            name=name,
            model=raw.get("model"),
            serial=raw.get("serial"),
            size=int(size) if size else None,
            kind=raw.get("type"),
            pool=raw.get("pool"),
            temp_c=_num(t),
        )


@dataclass(frozen=True)
class Realtime:
    cpu_percent: float | None
    cpu_temp_c: float | None
    mem_total: int | None
    mem_available: int | None
    arc_size: int | None

    @property
    def mem_used_fraction(self) -> float | None:
        if self.mem_total and self.mem_available is not None:
            return max(0.0, min(1.0, 1 - self.mem_available / self.mem_total))
        return None

    @classmethod
    def parse(cls, fields: dict) -> Realtime:
        cpu = fields.get("cpu") or {}
        # 25.10+: {"cpu": {"cpu": {"usage", "temp"}, "cpu0": ...}}; 25.04: {"cpu": {"average": {"usage"}...}}
        agg = cpu.get("cpu") if isinstance(cpu.get("cpu"), dict) else cpu.get("average") or {}
        temp = _num(agg.get("temp"))
        if temp is None:
            temps = cpu.get("temperature_celsius") or []
            vals = [v for v in (_num(x) for x in temps) if v is not None]
            temp = max(vals) if vals else None
        mem = fields.get("memory") or {}

        def as_int(v: Any) -> int | None:
            n = _num(v)
            return int(n) if n is not None else None

        return cls(
            cpu_percent=_num(agg.get("usage")),
            cpu_temp_c=temp,
            mem_total=as_int(mem.get("physical_memory_total")),
            mem_available=as_int(mem.get("physical_memory_available")),
            arc_size=as_int(mem.get("arc_size")),
        )


@dataclass
class Snapshot:
    """Everything /nas status needs, fetched in one go. Sections that failed are
    None with the reason in ``errors`` — a partial answer beats no answer."""

    system: SystemInfo
    fetched_at: datetime
    pools: list[Pool] | None = None
    boot_pool: Pool | None = None
    alerts: list[Alert] | None = None
    apps: list[App] | None = None
    disks: list[Disk] | None = None
    realtime: Realtime | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def active_alerts(self) -> list[Alert]:
        return [a for a in (self.alerts or []) if not a.dismissed]

    @property
    def health(self) -> str:
        """OK / WARN / CRIT roll-up used for embed colors."""
        crit = warn = False
        for p in (self.pools or []) + ([self.boot_pool] if self.boot_pool else []):
            if p.status.upper() in ("FAULTED", "UNAVAIL", "SUSPENDED", "REMOVED", "OFFLINE"):
                crit = True
            elif not p.ok:
                warn = True
        for a in self.active_alerts:
            if a.rank >= level_rank("CRITICAL"):
                crit = True
            elif a.rank >= level_rank("WARNING"):
                warn = True
        if any(app.state == "CRASHED" for app in self.apps or []):
            warn = True
        return "CRIT" if crit else "WARN" if warn else "OK"
