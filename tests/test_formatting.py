from datetime import UTC, datetime

import discord
import pytest

from nasbot import formatting as f
from nasbot.models import Alert, App, Disk, Pool, Realtime, Snapshot, SystemInfo

from .fixtures import (
    ALERTS,
    APPS,
    BOOT_POOL,
    DISK_TEMPS,
    DISKS,
    POOL_FAST_DEGRADED,
    POOL_TANK,
    REALTIME_FIELDS,
    SYSTEM_INFO,
    alert,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)


def embed_size(e: discord.Embed) -> int:
    return len(e)  # discord.py implements the documented 6000-char total


def assert_within_limits(e: discord.Embed) -> None:
    assert len(e.title or "") <= 256
    assert len(e.description or "") <= 4096
    assert len(e.fields) <= 25
    for fld in e.fields:
        assert len(fld.name) <= 256 and 0 < len(fld.value) <= 1024
    assert embed_size(e) <= 6000


@pytest.mark.parametrize(("n", "s"), [(None, "?"), (512, "512 B"), (1536, "1.5 KiB"), (36.4 * 1024**4, "36.4 TiB"),
                                      (3 * 1024**6, "3072.0 PiB")])
def test_human_bytes(n, s):
    assert f.human_bytes(n) == s


@pytest.mark.parametrize(("sec", "s"), [(None, "?"), (5, "5s"), (125, "2m"), (3 * 3600 + 120, "3h 2m"),
                                        (12 * 86400 + 4 * 3600, "12d 4h"), (-3, "0s")])
def test_human_duration(sec, s):
    assert f.human_duration(sec) == s


def test_bar():
    assert f.bar(0.31) == "▰▰▰▱▱▱▱▱▱▱"
    assert f.bar(None) == "▱" * 10
    assert f.bar(1.7, 4) == "▰▰▰▰"


def test_ts_markup():
    assert f.ts(NOW) == f"<t:{int(NOW.timestamp())}:R>"
    assert f.ts(None) == "unknown"


def test_join_lines_drops_whole_lines():
    lines = [f"line {i:03d} " + "x" * 40 for i in range(100)]
    out = f._join_lines(lines, 500)
    assert len(out) <= 500
    assert out.splitlines()[-1].startswith("…and ")
    assert all(line in lines for line in out.splitlines()[:-1])


def full_snapshot(**kw) -> Snapshot:
    base = dict(
        system=SystemInfo.parse(SYSTEM_INFO), fetched_at=NOW,
        pools=[Pool.parse(POOL_TANK), Pool.parse(POOL_FAST_DEGRADED)], boot_pool=Pool.parse(BOOT_POOL),
        alerts=[Alert.parse(a) for a in ALERTS], apps=[App.parse(a) for a in APPS],
        realtime=Realtime.parse(REALTIME_FIELDS),
    )
    return Snapshot(**(base | kw))


def test_status_embed():
    e = f.status_embed(full_snapshot())
    assert_within_limits(e)
    assert e.title == "🔴 truenas — critical"
    assert e.color.value == f.COLOR_CRIT
    names = [fld.name for fld in e.fields]
    assert names == ["CPU", "Memory", "Apps (4)", "Storage", "Alerts"]
    cpu = e.fields[0].value
    assert "7%" in cpu and "46°C" in cpu and "load 0.42" in cpu
    storage = e.fields[3].value
    assert "**tank** `ONLINE`" in storage and "**fast** `DEGRADED`" in storage and "resilver in progress 42%" in storage
    assert "boot `ONLINE`" in storage
    assert "1 critical" in e.fields[4].value and "1 warning" in e.fields[4].value
    assert "Scrub" not in e.fields[4].value  # dismissed alert hidden


def test_status_embed_healthy_and_partial():
    e = f.status_embed(full_snapshot(pools=[Pool.parse(POOL_TANK)], alerts=[], apps=None, realtime=None,
                                     errors={"apps": "permission denied for this API user"}))
    assert e.title.startswith("🟢") and e.color.value == f.COLOR_OK
    assert e.fields[0].value.startswith("—")  # no realtime
    assert "32.0 GiB installed" in e.fields[1].value
    assert e.fields[-1].name == "⚠️ Partial data" and "apps: permission denied" in e.fields[-1].value
    assert any(fld.value == "✅ None" for fld in e.fields)


def test_status_embed_handles_huge_inputs():
    many_pools = [Pool.parse(POOL_TANK | {"name": f"pool{i:02d}-" + "x" * 30}) for i in range(60)]
    many_alerts = [Alert.parse(alert(f"u{i}", "CRITICAL", "K", "z" * 400)) for i in range(300)]
    many_apps = [App.parse(APPS[0] | {"name": f"app{i}" + "y" * 40, "state": "CRASHED"}) for i in range(80)]
    e = f.status_embed(full_snapshot(pools=many_pools, alerts=many_alerts, apps=many_apps))
    assert_within_limits(e)
    assert "…and" in e.fields[3].value
    for builder in (f.pools_embed(full_snapshot(pools=many_pools)),
                    f.alerts_embed(many_alerts, "h"),
                    f.apps_embed(many_apps, "h"),
                    f.alert_burst_embed(many_alerts, "h")):
        assert_within_limits(builder)


def test_pools_embed():
    e = f.pools_embed(full_snapshot())
    assert_within_limits(e)
    assert [fld.name for fld in e.fields] == ["🟢 tank", "🟠 fast", "🟢 boot-pool"]
    tank = e.fields[0].value
    assert "31.0% used" in tank and "Fragmentation: 2%" in tank and "Last scrub finished <t:" in tank
    assert ", 0 errors" in tank
    fast = e.fields[1].value
    assert "Resilver running — 41.5%, ~1h 30m left" in fast
    assert "> One or more devices" in fast
    assert e.color.value == f.COLOR_WARN


def test_pools_embed_error():
    e = f.pools_embed(full_snapshot(pools=None, boot_pool=None, errors={"pools": "boom"}))
    assert "Could not read pools: boom" in e.description


def test_disks_embed():
    disks = [Disk.parse(d, DISK_TEMPS) for d in DISKS]
    e = f.disks_embed(disks, "truenas", NOW)
    assert_within_limits(e)
    assert e.description.startswith("```\ndisk")
    assert "sdd" in e.description and " 52°!" in e.description
    assert "nvme0n1" in e.description and "  —" in e.description
    assert e.color.value == f.COLOR_WARN  # hottest 52
    assert "max 52°C" in e.footer.text
    assert f.disks_embed([], "h", NOW).description == "No disks reported."


def test_alerts_embed():
    alerts = [Alert.parse(a) for a in ALERTS]
    e = f.alerts_embed(alerts, "truenas")
    assert e.title == "Alerts — truenas (3)" and e.color.value == f.COLOR_CRIT
    assert "(dismissed)" not in e.description
    with_dismissed = f.alerts_embed(alerts, "truenas", include_dismissed=True)
    assert "(dismissed)" in with_dismissed.description
    assert f.alerts_embed([], "x").description == "✅ No active alerts."


def test_apps_embed():
    e = f.apps_embed([App.parse(a) for a in APPS], "truenas")
    assert "🟢 **immich** `v1.0.0_1.2.3` — running · ⬆️ update" in e.description
    assert "⚪ **navidrome**" in e.description
    assert f.apps_embed([], "x").description == "No apps installed."


def test_notification_embeds():
    a = Alert.parse(ALERTS[1])
    raised = f.alert_raised_embed(a, "truenas")
    assert raised.title.startswith("🔴 Critical: Pool fast") and raised.footer.text == "truenas · VolumeStatus"
    cleared = f.alert_cleared_embed("t", "WARNING", "SMART", "h", dismissed=False)
    assert cleared.title == "✅ Resolved: t" and cleared.color.value == f.COLOR_OK
    assert f.alert_cleared_embed("t", "WARNING", "SMART", "h", dismissed=True).title.startswith("✅ Dismissed")
    down = f.nas_down_embed("truenas", NOW, "Connection refused")
    assert "unreachable" in down.title and "Connection refused" in down.description
    up = f.nas_up_embed("truenas", 3700, rebooted=True)
    assert "1h 1m" in up.description and "rebooted" in up.description
    crash = f.app_state_embed(App.parse(APPS[0] | {"state": "CRASHED"}), "RUNNING", "h")
    assert crash.title == "🔴 App crashed: immich" and "`running` → `crashed`" in crash.description
    assert f.silence_summary_embed([]).description == "Nothing happened while muted."
    assert "2 suppressed" in f.silence_summary_embed(["a", "b"]).title
