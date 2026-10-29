"""Realistic TrueNAS 25.10 API payloads (shapes taken from middlewared's api/v25_10_0 models)."""

from __future__ import annotations

import copy
import time

BOOT_MS = int((time.time() - 12 * 86400 - 4 * 3600) * 1000)
NOW_MS = int(time.time() * 1000)
TiB = 1024**4
GiB = 1024**3


def date(ms: int) -> dict:
    return {"$date": ms}


SYSTEM_INFO = {
    "version": "25.10.2",
    "buildtime": date(NOW_MS - 40 * 86400_000),
    "hostname": "truenas",
    "physmem": 32 * GiB,
    "model": "Intel(R) Core(TM) i3-N305",
    "cores": 8,
    "physical_cores": 8,
    "loadavg": [0.42, 0.38, 0.31],
    "uptime": "12 days, 4:00:00",
    "uptime_seconds": 12 * 86400 + 4 * 3600,
    "system_serial": None,
    "system_product": "CW-N305",
    "system_product_version": None,
    "license": None,
    "boottime": date(BOOT_MS),
    "datetime": date(NOW_MS),
    "timezone": "America/Los_Angeles",
    "system_manufacturer": "CWWK",
    "ecc_memory": False,
}

POOL_TANK = {
    "id": 1, "name": "tank", "guid": "123", "status": "ONLINE", "path": "/mnt/tank",
    "scan": {"function": "SCRUB", "state": "FINISHED", "start_time": date(NOW_MS - 3 * 86400_000),
             "end_time": date(NOW_MS - 3 * 86400_000 + 5 * 3600_000), "percentage": 100.0,
             "bytes_to_process": 0, "bytes_processed": 0, "bytes_issued": 0, "pause": None, "errors": 0,
             "total_secs_left": None},
    "expand": None, "is_upgraded": True, "healthy": True, "warning": False,
    "status_code": "OK", "status_detail": None,
    "size": int(36.4 * TiB), "allocated": int(11.3 * TiB), "free": int(25.1 * TiB), "freeing": 0,
    "dedup_table_size": 0, "dedup_table_quota": "auto", "fragmentation": "2",
    "size_str": None, "allocated_str": None,
}

POOL_FAST_DEGRADED = {
    **copy.deepcopy(POOL_TANK), "id": 2, "name": "fast", "guid": "456", "status": "DEGRADED",
    "healthy": False, "warning": True, "status_code": "DEGRADED",
    "status_detail": "One or more devices could not be used because the label is missing or invalid.",
    "size": 2 * TiB, "allocated": int(1.8 * TiB), "free": int(0.2 * TiB),
    "scan": {"function": "RESILVER", "state": "SCANNING", "start_time": date(NOW_MS - 600_000), "end_time": None,
             "percentage": 41.5, "errors": 0, "total_secs_left": 5400},
}

BOOT_POOL = {k: v for k, v in POOL_TANK.items() if k not in ("id", "guid")} | {
    "name": "boot-pool", "path": "/", "size": 240 * GiB, "allocated": 12 * GiB, "free": 228 * GiB, "scan": None,
}


def alert(uuid: str, level: str, klass: str, formatted: str, *, dismissed: bool = False, age_s: int = 3600) -> dict:
    return {
        "uuid": uuid, "source": "", "klass": klass, "args": {}, "node": "A", "key": uuid,
        "datetime": date(NOW_MS - age_s * 1000), "last_occurrence": date(NOW_MS - age_s * 1000),
        "dismissed": dismissed, "mail": None, "text": formatted, "id": uuid, "level": level,
        "formatted": formatted, "one_shot": False,
    }


ALERTS = [
    alert("a-1", "WARNING", "SMART", "Device: /dev/sdb [SAT], 8 Currently unreadable (pending) sectors."),
    alert("a-2", "CRITICAL", "VolumeStatus",
          "Pool fast state is DEGRADED: One or more devices could not be used.<br>"
          "The following devices are not healthy:<ul><li>Disk WDC_WD140EDGZ 9MG12345 is UNAVAIL</li></ul>"),
    alert("a-3", "INFO", "HasUpdate", "A system update is available. Go to System → Update to download it."),
    alert("a-4", "WARNING", "ScrubPaused", "Scrub for pool tank is paused for more than 8 hours.", dismissed=True),
]


def app(name: str, state: str = "RUNNING", *, upgrade: bool = False) -> dict:
    return {
        "name": name, "id": name, "state": state, "upgrade_available": upgrade, "latest_version": None,
        "image_updates_available": False, "custom_app": False, "migrated": False,
        "human_version": "v1.0.0_1.2.3", "version": "1.2.3", "metadata": {}, "active_workloads": {},
        "notes": None, "portals": {},
    }


APPS = [app("immich", upgrade=True), app("jellyfin"), app("navidrome", "STOPPED"), app("vaultwarden")]


def disk(name: str, serial: str, pool: str | None, size: int, model: str, kind: str = "HDD") -> dict:
    return {"identifier": f"{{serial_lunid}}{serial}", "name": name, "subsystem": "SCSI", "number": 0,
            "serial": serial, "lunid": None, "size": size, "description": "", "transfermode": "Auto",
            "hddstandby": "ALWAYS ON", "advpowermgmt": "DISABLED", "expiretime": None, "model": model,
            "rotationrate": 7200 if kind == "HDD" else None, "type": kind, "pool": pool}


DISKS = [disk(f"sd{c}", f"9MG1234{i}", "tank", 14 * 1000**4, "WDC WD140EDGZ-11B2DA2") for i, c in enumerate("abcd")]
DISKS.append(disk("nvme0n1", "S6XNNS0T", None, 500 * 1000**3, "Samsung SSD 980 PRO 500GB", "SSD"))
DISK_TEMPS = {"sda": 36, "sdb": 41, "sdc": 38, "sdd": 52, "nvme0n1": None}

REALTIME_FIELDS = {
    "cpu": {"cpu": {"usage": 7.25, "temp": 46.0}, "cpu0": {"usage": 9.0, "temp": 46.0}},
    "memory": {"arc_size": 12 * GiB, "arc_free_memory": 2 * GiB, "arc_available_memory": 4 * GiB,
               "physical_memory_total": 32 * GiB, "physical_memory_available": 15 * GiB},
    "zfs": {}, "interfaces": {}, "disks": {}, "pools": {},
}

# 25.04 used a different CPU shape in reporting.realtime
REALTIME_FIELDS_2504 = {
    "cpu": {"average": {"usage": 12.0}, "temperature_celsius": [44.0, 47.5]},
    "memory": REALTIME_FIELDS["memory"],
}


def default_responses() -> dict:
    return {
        "system.info": copy.deepcopy(SYSTEM_INFO),
        "pool.query": [copy.deepcopy(POOL_TANK), copy.deepcopy(POOL_FAST_DEGRADED)],
        "boot.get_state": copy.deepcopy(BOOT_POOL),
        "alert.list": copy.deepcopy(ALERTS),
        "app.query": copy.deepcopy(APPS),
        "disk.query": copy.deepcopy(DISKS),
        "disk.temperatures": dict(DISK_TEMPS),
        "core.ping": "pong",
    }
