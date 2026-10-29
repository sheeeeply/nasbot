from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from nasbot.monitor import BURST_THRESHOLD, Monitor
from nasbot.state import MemoryStateStore, StateStore
from nasbot.truenas import NASCallError

from .conftest import Clock, FakeAPI, make_cfg
from .fixtures import alert, app


def kinds(notes):
    return [n.kind for n in notes]


async def started(api: FakeAPI, clock: Clock, **cfg_overrides) -> Monitor:
    """A monitor that has already completed its baseline poll."""
    m = Monitor(api, make_cfg(**cfg_overrides), MemoryStateStore(), now=clock)
    assert await m.poll() == []
    return m


# --------------------------------------------------------------------- alerts

async def test_first_poll_is_a_silent_baseline(api, clock):
    m = Monitor(api, make_cfg(), MemoryStateStore(), now=clock)
    assert await m.poll() == []
    # WARNING + CRITICAL are eligible; INFO is below threshold; the dismissed one is excluded
    assert set(m.state.known_alerts) == {"a-1", "a-2"}
    assert m.state.hostname == "truenas" and m.last_poll_ok


async def test_new_alert_is_reported_once(api, clock):
    m = await started(api, clock)
    api.responses["alert.list"].append(alert("a-9", "ERROR", "SMARTTestFailed", "SMART test failed on sdc"))
    notes = await m.poll()
    assert kinds(notes) == ["alert_raised"]
    assert notes[0].summary == "Error: SMART test failed on sdc"
    assert notes[0].urgent is False  # default mention threshold is CRITICAL
    assert await m.poll() == []  # not repeated


async def test_urgency_follows_mention_threshold(api, clock):
    m = await started(api, clock, mention_min_level="ERROR")
    api.responses["alert.list"].append(alert("a-9", "ERROR", "K", "x"))
    assert (await m.poll())[0].urgent is True


async def test_resolved_and_dismissed_are_distinguished(api, clock):
    m = await started(api, clock)
    alerts = api.responses["alert.list"]
    alerts[0]["dismissed"] = True  # a-1 dismissed in the UI
    del alerts[1]  # a-2 resolved by TrueNAS
    notes = await m.poll()
    by_summary = {n.summary for n in notes}
    assert by_summary == {"Dismissed: Device: /dev/sdb [SAT], 8 Currently unreadable (pending) sectors.",
                          "Resolved: Pool fast state is DEGRADED: One or more devices could not be used."}
    assert all(n.kind == "alert_cleared" and not n.urgent for n in notes)


async def test_level_and_class_filters(api, clock):
    m = await started(api, clock, min_alert_level="ERROR", ignore_alert_classes=frozenset({"Noisy"}))
    assert set(m.state.known_alerts) == {"a-2"}
    api.responses["alert.list"] += [alert("w", "WARNING", "K", "below"), alert("n", "CRITICAL", "Noisy", "ignored")]
    assert await m.poll() == []


async def test_alert_burst_is_bundled(api, clock):
    m = await started(api, clock)
    api.responses["alert.list"] += [alert(f"b{i}", "CRITICAL", "K", f"thing {i}") for i in range(BURST_THRESHOLD + 3)]
    notes = await m.poll()
    assert kinds(notes) == ["alert_burst"] and notes[0].urgent
    api.responses["alert.list"] = []
    notes = await m.poll()
    assert kinds(notes) == ["alert_cleared"] and "cleared" in notes[0].summary


async def test_alert_list_permission_error_is_not_an_outage(api, clock):
    m = await started(api, clock)
    api.errors["alert.list"] = NASCallError("alert.list failed: Not authorized")
    for _ in range(5):
        assert await m.poll() == []
    assert m.state.down_since is None and m.last_poll_ok


# --------------------------------------------------------------- reachability

async def test_unreachable_after_n_failures_then_recovery(api, clock):
    m = await started(api, clock, unreachable_after=3)
    api.down = True
    clock.advance(minutes=1)
    assert await m.poll() == []
    clock.advance(minutes=1)
    assert await m.poll() == []
    clock.advance(minutes=1)
    notes = await m.poll()
    assert kinds(notes) == ["nas_down"] and notes[0].urgent
    assert m.last_poll_ok is False
    clock.advance(minutes=1)
    assert await m.poll() == []  # no repeat while still down

    api.down = False
    clock.advance(minutes=10)
    notes = await m.poll()
    assert kinds(notes) == ["nas_up"]
    # downtime is measured from the *first* failed poll: 1 + 1 + 1 + 10 = 13 minutes
    assert "13m" in notes[0].embed.description and "rebooted" not in notes[0].embed.description
    assert m.state.down_since is None and m.state.consecutive_failures == 0


async def test_blip_below_threshold_is_silent(api, clock):
    m = await started(api, clock, unreachable_after=3)
    api.down = True
    await m.poll()
    await m.poll()
    api.down = False
    assert await m.poll() == []
    assert m.state.consecutive_failures == 0 and m.state.first_failure_at is None


async def test_reboot_during_outage_is_mentioned_in_recovery(api, clock):
    m = await started(api, clock, unreachable_after=1)
    api.down = True
    assert kinds(await m.poll()) == ["nas_down"]
    api.down = False
    api.responses["system.info"]["boottime"] = {"$date": int(clock().timestamp() * 1000)}
    notes = await m.poll()
    assert kinds(notes) == ["nas_up"] and "rebooted" in notes[0].embed.description


async def test_reboot_between_polls_is_reported(api, clock):
    m = await started(api, clock)
    api.responses["system.info"]["boottime"] = {"$date": int(clock().timestamp() * 1000)}
    notes = await m.poll()
    assert kinds(notes) == ["rebooted"]
    assert await m.poll() == []


async def test_boottime_jitter_is_ignored(api, clock):
    m = await started(api, clock)
    api.responses["system.info"]["boottime"]["$date"] += 30_000
    assert await m.poll() == []


# ------------------------------------------------------------------------ apps

async def test_app_transitions(api, clock):
    m = await started(api, clock, mention_min_level="ERROR")
    apps = api.responses["app.query"]
    apps[0]["state"] = "CRASHED"  # immich
    apps[1]["state"] = "DEPLOYING"  # jellyfin, transient: ignored
    apps[2]["state"] = "RUNNING"  # navidrome started
    notes = await m.poll()
    assert {n.summary: n.urgent for n in notes} == {"App immich: running → crashed": True,
                                                     "App navidrome: stopped → running": False}
    assert m.state.app_states["jellyfin"] == "RUNNING"  # last stable state kept

    apps[1]["state"] = "RUNNING"  # deploy finished: back to where it was, no message
    apps[0]["state"] = "RUNNING"
    notes = await m.poll()
    assert [n.summary for n in notes] == ["App immich: crashed → running"]


async def test_new_and_removed_apps_are_quiet(api, clock):
    m = await started(api, clock)
    api.responses["app.query"] = [app("immich"), app("newthing", "STOPPED")]
    assert await m.poll() == []
    assert set(m.state.app_states) == {"immich", "newthing"}


async def test_app_errors_and_disable(api, clock):
    m = await started(api, clock)
    api.errors["app.query"] = NASCallError("app.query failed: apps not configured")
    assert await m.poll() == []
    m2 = await started(FakeAPI(), clock, watch_apps=False)
    assert m2.state.app_states is None


# ---------------------------------------------------------------------- silence

async def test_mute_suppresses_then_summarizes(api, clock):
    m = await started(api, clock)
    assert m.set_silence(30) == []
    assert m.silenced_until is not None
    api.responses["alert.list"].append(alert("s1", "CRITICAL", "K", "during maintenance"))
    assert await m.poll() == []
    assert m.state.suppressed == ["Critical: during maintenance"]

    clock.advance(minutes=31)
    api.responses["alert.list"].append(alert("s2", "WARNING", "K", "after"))
    notes = await m.poll()
    assert kinds(notes) == ["silence_ended", "alert_raised"]
    assert "1 suppressed" in notes[0].embed.title
    assert m.state.silence_until is None and m.state.suppressed == []


async def test_manual_unmute(api, clock):
    m = await started(api, clock)
    assert m.set_silence(0) == []  # not muted: nothing to say
    m.set_silence(60)
    notes = m.set_silence(0)
    assert kinds(notes) == ["silence_ended"] and m.silenced_until is None


async def test_suppressed_list_is_bounded(api, clock):
    m = await started(api, clock)
    m.set_silence(600)
    for i in range(150):
        api.responses["alert.list"] = [alert(f"x{i}", "WARNING", "K", f"n{i}")]
        await m.poll()
    assert len(m.state.suppressed) == 100
    assert m.state.suppressed[-2:] == ["Warning: n149", "Resolved: n148"]  # newest kept, oldest dropped


# ----------------------------------------------------------------------- digest

async def test_digest_fires_once_a_day_in_configured_tz(api):
    # 08:30 in Seoul == 23:30 UTC the previous day
    clock = Clock(datetime(2026, 10, 26, 23, 0, tzinfo=UTC))  # 08:00 KST
    m = Monitor(api, make_cfg(digest_time=time(8, 30), digest_tz="Asia/Seoul"), MemoryStateStore(), now=clock)
    assert await m.poll() == []  # baseline; digest scheduled for later today
    clock.advance(minutes=29)
    assert await m.poll() == []
    clock.advance(minutes=2)  # 08:31 KST
    notes = await m.poll()
    assert kinds(notes) == ["digest"] and notes[0].embed.title.startswith("Daily digest · ")
    clock.advance(hours=3)
    assert await m.poll() == []
    clock.advance(hours=21)  # next day 08:31
    assert kinds(await m.poll()) == ["digest"]


async def test_digest_not_sent_on_late_startup(api):
    clock = Clock(datetime(2026, 10, 27, 5, 0, tzinfo=UTC))  # 14:00 KST, after 08:30
    m = Monitor(api, make_cfg(digest_time=time(8, 30), digest_tz="Asia/Seoul"), MemoryStateStore(), now=clock)
    await m.poll()
    clock.advance(minutes=5)
    assert await m.poll() == []
    assert m.state.last_digest_date == "2026-10-27"


# ----------------------------------------------------------------- persistence

async def test_state_survives_restart(api, clock, tmp_path):
    store = StateStore(tmp_path / "state.json")
    m = Monitor(api, make_cfg(), store, now=clock)
    await m.poll()
    # While the bot is down, one alert clears and one appears
    del api.responses["alert.list"][0]
    api.responses["alert.list"].append(alert("new", "WARNING", "K", "appeared while bot was down"))
    m2 = Monitor(api, make_cfg(), StateStore(tmp_path / "state.json"), now=clock)
    notes = await m2.poll()
    assert sorted(kinds(notes)) == ["alert_cleared", "alert_raised"]


async def test_outage_state_survives_restart(api, clock, tmp_path):
    path = tmp_path / "state.json"
    m = Monitor(api, make_cfg(unreachable_after=1), StateStore(path), now=clock)
    await m.poll()
    api.down = True
    assert kinds(await m.poll()) == ["nas_down"]
    m2 = Monitor(api, make_cfg(unreachable_after=1), StateStore(path), now=clock)
    assert await m2.poll() == []  # already announced before the restart
    api.down = False
    assert kinds(await m2.poll()) == ["nas_up"]


def test_corrupt_state_file_is_quarantined(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = StateStore(path).load()
    assert state.known_alerts is None
    assert (tmp_path / "state.corrupt").exists() and not path.exists()


def test_state_file_ignores_unknown_keys(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "hostname": "x", "from_the_future": 1, "known_alerts": '
                    '{"u": {"title": "t", "level": "WARNING", "klass": "K"}}}')
    state = StateStore(path).load()
    assert state.hostname == "x" and state.known_alerts["u"].title == "t"


@pytest.mark.parametrize("bad", ["[]", '{"known_alerts": {"u": {"nope": 1}}}'])
def test_state_file_wrong_shape(tmp_path, bad):
    path = tmp_path / "state.json"
    path.write_text(bad)
    assert StateStore(path).load().known_alerts is None
