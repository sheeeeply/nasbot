from datetime import UTC, datetime

from nasbot.models import Alert, App, Disk, Pool, Realtime, Scan, Snapshot, SystemInfo, _dt, html_to_text

from .fixtures import (
    ALERTS,
    APPS,
    BOOT_POOL,
    DISK_TEMPS,
    DISKS,
    POOL_FAST_DEGRADED,
    POOL_TANK,
    REALTIME_FIELDS,
    REALTIME_FIELDS_2504,
    SYSTEM_INFO,
    TiB,
)


def test_datetime_shapes():
    ref = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    ms = int(ref.timestamp() * 1000)
    assert _dt({"$date": ms}) == ref
    assert _dt(ms) == ref
    assert _dt(ref.timestamp()) == ref
    assert _dt("2026-01-02T03:04:05Z") == ref
    assert _dt(datetime(2026, 1, 2, 3, 4, 5)) == ref  # naive -> UTC
    assert _dt("garbage") is None
    assert _dt(None) is None


def test_system_info():
    info = SystemInfo.parse(SYSTEM_INFO)
    assert info.hostname == "truenas"
    assert info.version == "25.10.2"
    assert info.loadavg == (0.42, 0.38, 0.31)
    assert info.boottime is not None and info.boottime.tzinfo is not None
    assert info.cores == 8


def test_system_info_tolerates_missing_fields():
    info = SystemInfo.parse({})
    assert info.hostname == "truenas" and info.version == "unknown" and info.boottime is None


def test_pool_parsing():
    tank = Pool.parse(POOL_TANK)
    assert tank.ok and not tank.warning
    assert round(tank.used_fraction, 3) == round(11.3 / 36.4, 3)
    assert tank.scan and tank.scan.function == "SCRUB" and not tank.scan.running
    fast = Pool.parse(POOL_FAST_DEGRADED)
    assert not fast.ok and fast.status == "DEGRADED"
    assert fast.scan.running and fast.scan.percentage == 41.5 and fast.scan.seconds_left == 5400
    boot = Pool.parse(BOOT_POOL, default_name="boot-pool")
    assert boot.name == "boot-pool" and boot.scan is None


def test_pool_property_dict_values():
    p = Pool.parse({"name": "x", "status": "ONLINE", "healthy": True, "size": {"parsed": 2 * TiB},
                    "allocated": {"parsed": TiB}, "free": "1099511627776"})
    assert p.used_fraction == 0.5 and p.free == TiB


def test_scan_none_without_function():
    assert Scan.parse(None) is None
    assert Scan.parse({"function": None, "state": None}) is None


def test_alert_html_is_flattened():
    a = Alert.parse(ALERTS[1])
    assert a.level == "CRITICAL" and a.klass == "VolumeStatus"
    assert "<" not in a.text
    assert a.text.splitlines()[0] == "Pool fast state is DEGRADED: One or more devices could not be used."
    assert "UNAVAIL" in a.text
    assert a.title.startswith("Pool fast state is DEGRADED")


def test_alert_title_truncates():
    a = Alert.parse({"uuid": "u", "level": "WARNING", "klass": "X", "formatted": "y" * 500})
    assert len(a.title) == 178 and a.title.endswith("…")


def test_html_entities():
    assert html_to_text("a &amp; b<br/>c <b>d</b>") == "a & b\nc d"


def test_apps_and_disks():
    apps = [App.parse(a) for a in APPS]
    assert apps[0].upgrade_available and apps[2].state == "STOPPED"
    disks = [Disk.parse(d, DISK_TEMPS) for d in DISKS]
    assert disks[3].temp_c == 52 and disks[4].temp_c is None and disks[4].pool is None
    assert Disk.parse(DISKS[0], {"sda": [40, 60]}).temp_c == 40  # include_thresholds form


def test_realtime_shapes_2510_and_2504():
    rt = Realtime.parse(REALTIME_FIELDS)
    assert rt.cpu_percent == 7.25 and rt.cpu_temp_c == 46.0
    assert 0.5 < rt.mem_used_fraction < 0.6
    old = Realtime.parse(REALTIME_FIELDS_2504)
    assert old.cpu_percent == 12.0 and old.cpu_temp_c == 47.5
    empty = Realtime.parse({})
    assert empty.cpu_percent is None and empty.mem_used_fraction is None


def _snap(**kw) -> Snapshot:
    return Snapshot(system=SystemInfo.parse(SYSTEM_INFO), fetched_at=datetime.now(UTC), **kw)


def test_health_rollup():
    assert _snap(pools=[Pool.parse(POOL_TANK)], alerts=[], apps=[]).health == "OK"
    assert _snap(pools=[Pool.parse(POOL_FAST_DEGRADED)]).health == "WARN"
    assert _snap(pools=[Pool.parse(POOL_TANK | {"status": "FAULTED", "healthy": False})]).health == "CRIT"
    assert _snap(alerts=[Alert.parse(ALERTS[1])]).health == "CRIT"
    assert _snap(alerts=[Alert.parse(ALERTS[0])]).health == "WARN"
    assert _snap(alerts=[Alert.parse(ALERTS[2])]).health == "OK"  # INFO doesn't count
    assert _snap(alerts=[Alert.parse(ALERTS[3])]).health == "OK"  # dismissed doesn't count
    assert _snap(apps=[App.parse(APPS[0] | {"state": "CRASHED"})]).health == "WARN"
