"""The media watcher's rules: grace, dedupe, grouping, health baseline, reachability,
imports, and how it rides on the main monitor (mute, persistence, NAS-down)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nasbot.arr import ArrAuthError, ArrCallError, ArrInstance, ArrUnavailable, HealthIssue, ImportEvent, QueueItem
from nasbot.media import MediaWatcher, classify
from nasbot.monitor import Monitor
from nasbot.state import MemoryStateStore, State

from .conftest import Clock, make_cfg
from .mock_arr import black_clover_pack, radarr_row, sonarr_row

T0 = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)


class FakeArr:
    """Stands in for ArrClient: canned rows, injectable failures."""

    def __init__(self, kind: str = "sonarr") -> None:
        self.instance = ArrInstance(kind, f"http://192.168.1.158:{8989 if kind == 'sonarr' else 7878}", "k")  # type: ignore[arg-type]
        self.rows: list[dict] = []
        self.health_rows: list[dict] = []
        self.imports: list[ImportEvent] = []
        self.exc: Exception | None = None
        self.import_calls: list[datetime] = []
        self.closed = False

    kind = property(lambda self: self.instance.kind)
    name = property(lambda self: self.instance.name)

    async def queue(self):
        if self.exc:
            raise self.exc
        return [QueueItem.parse(self.kind, r) for r in self.rows]

    async def health(self):
        if self.exc:
            raise self.exc
        return [HealthIssue.parse(self.kind, r) for r in self.health_rows]

    async def imports_since(self, since):
        self.import_calls.append(since)
        return [e for e in self.imports if e.when and e.when > since]

    async def close(self):
        self.closed = True


def watcher(*clients: FakeArr, **cfg_kw) -> MediaWatcher:
    cfg_kw.setdefault("media_grace_minutes", 10)
    return MediaWatcher(list(clients), make_cfg(**cfg_kw))


def kinds(notes):
    return [n.kind for n in notes]


async def run(w: MediaWatcher, state: State, at: datetime, **kw):
    return await w.check(state, at, **kw)


# ------------------------------------------------------------------ classify

@pytest.mark.parametrize(("state", "status", "stalled", "expected"), [
    ("importBlocked", "warning", False, "import"),
    ("importPending", "warning", False, "waiting"),  # "Waiting to Import": a temporary hold (TBA title…)
    ("importPending", "ok", False, None),  # importing normally
    ("failedPending", "warning", False, "failed"),
    ("downloading", "error", False, "failed"),
    ("downloading", "warning", False, None),  # stalled, but not watched by default
    ("downloading", "warning", True, "stalled"),
    ("downloading", "ok", True, None),
    ("imported", "ok", True, None),
])
def test_classify(state, status, stalled, expected):
    item = QueueItem.parse("sonarr", sonarr_row(1, download_id="d", release="r", series="s", season=1, episode=1,
                                                state=state, status=status))
    assert classify(item, watch_stalled=stalled) == expected


# --------------------------------------------------------------- attention

async def test_import_blocked_pack_is_one_message_after_grace_then_quiet():
    s = FakeArr()
    s.rows = black_clover_pack(124, 170)
    w, state = watcher(s), State()
    assert await run(w, state, T0) == []  # inside grace
    assert await run(w, state, T0 + timedelta(minutes=5)) == []
    notes = await run(w, state, T0 + timedelta(minutes=10))
    assert kinds(notes) == ["media_attention"]
    n = notes[0]
    assert n.summary == "Sonarr: needs manual import — Black Clover" and n.urgent is False
    assert "Manual Import" in n.embed.description and n.embed.url.endswith(":8989/activity/queue")
    fields = {f.name: f.value for f in n.embed.fields}
    assert fields["Episodes"] == "S01E124–E170 (47 episodes)"
    assert "Invalid season or episode — 47 files" in fields["What Sonarr says"]
    assert await run(w, state, T0 + timedelta(minutes=15)) == []  # never repeated


async def test_waiting_to_import_is_only_reported_if_it_outlasts_the_hold():
    """Sonarr holds a new episode whose title is still TBA for up to 48 h ("Waiting to Import").
    That's normal and resolves itself, so it must not ping after the usual 10 minutes."""
    s = FakeArr()
    s.rows = [sonarr_row(1, download_id="E5", release="[Erai-raws] Black Clover 2nd Season - 05 [1080p]",
                         series="Black Clover", season=2, episode=5, state="importPending", status="warning",
                         queue_status="completed", sizeleft=0,
                         messages=[{"title": "[Erai-raws] Black Clover 2nd Season - 05.mkv",
                                    "messages": ["Episode has a TBA title and recently aired"]}])]
    w, state = watcher(s), State()
    assert await run(w, state, T0) == []
    assert await run(w, state, T0 + timedelta(hours=24)) == []
    assert await run(w, state, T0 + timedelta(hours=48, minutes=59)) == []
    notes = await run(w, state, T0 + timedelta(hours=49))
    assert notes[0].summary == "Sonarr: still waiting to import — Black Clover"
    assert "TBA" in notes[0].embed.description
    # the common case: the title arrives / 48 h pass and it imports — nothing was ever posted
    s2 = FakeArr()
    s2.rows = s.rows
    w2, state2 = watcher(s2), State()
    await run(w2, state2, T0)
    s2.rows = []
    assert await run(w2, state2, T0 + timedelta(hours=30)) == []


async def test_resolved_only_after_it_was_announced():
    s = FakeArr()
    s.rows = black_clover_pack()
    w, state = watcher(s, media_grace_minutes=0), State()
    assert kinds(await run(w, state, T0)) == ["media_attention"]
    s.rows = []
    notes = await run(w, state, T0 + timedelta(minutes=5))
    assert kinds(notes) == ["media_resolved"] and notes[0].embed.title == "✅ Sorted: Black Clover"
    assert state.media_attention == {}
    # something that sorted itself out inside the grace period never makes noise
    s.rows = black_clover_pack()
    w2, state2 = watcher(s), State()
    await run(w2, state2, T0)
    s.rows = []
    assert await run(w2, state2, T0 + timedelta(minutes=5)) == []


async def test_failed_movie_and_mention_option():
    r = FakeArr("radarr")
    r.rows = [radarr_row(1, download_id="D", release="Dune.2021", title="Dune", year=2021, state="failedPending",
                         status="warning", error="Download client reports a failure")]
    notes = await run(watcher(r, media_grace_minutes=0, media_mention=True), State(), T0)
    assert notes[0].summary == "Radarr: download failed — Dune (2021)" and notes[0].urgent is True
    assert "Episodes" not in [f.name for f in notes[0].embed.fields]


async def test_stalled_only_when_enabled():
    s = FakeArr()
    s.rows = [sonarr_row(1, download_id="G1", release="Glee S01", series="Glee", season=1, episode=1,
                         state="downloading", status="warning", sizeleft=1.3e8)]
    assert await run(watcher(s, media_grace_minutes=0), State(), T0) == []
    notes = await run(watcher(s, media_grace_minutes=0, media_watch_stalled=True), State(), T0)
    assert notes[0].summary == "Sonarr: download stalled — Glee"
    assert any("87%" in f.value for f in notes[0].embed.fields)


async def test_reason_change_is_announced_again():
    s = FakeArr()
    s.rows = [sonarr_row(1, download_id="X", release="X", series="Show", season=1, episode=1,
                         state="downloading", status="warning")]
    w, state = watcher(s, media_grace_minutes=0, media_watch_stalled=True), State()
    assert kinds(await run(w, state, T0)) == ["media_attention"]
    s.rows[0]["trackedDownloadState"] = "importBlocked"
    notes = await run(w, state, T0 + timedelta(minutes=5))
    assert notes[0].summary == "Sonarr: needs manual import — Show"


async def test_burst_is_bundled():
    s = FakeArr()
    s.rows = [sonarr_row(i, download_id=f"D{i}", release=f"R{i}", series=f"Show {i}", season=1, episode=1,
                         state="importBlocked", status="warning") for i in range(8)]
    notes = await run(watcher(s, media_grace_minutes=0), State(), T0)
    assert len(notes) == 1 and notes[0].embed.title == "⚠️ 8 Sonarr downloads need attention"


# ------------------------------------------------------------------- health

async def test_health_baseline_new_cleared_and_ignore():
    s = FakeArr()
    s.health_rows = [{"source": "UpdateCheck", "type": "warning", "message": "New update available"}]
    w, state = watcher(s, media_health_ignore=frozenset({"IndexerLongTermStatusCheck"})), State()
    assert await run(w, state, T0) == []  # baseline: existing issues are not re-announced
    s.health_rows += [
        {"source": "DownloadClientCheck", "type": "error", "message": "Unable to communicate with qBittorrent"},
        {"source": "IndexerLongTermStatusCheck", "type": "warning", "message": "ignored"},
        {"source": "RecycleBinCheck", "type": "notice", "message": "notices are not alerts"},
    ]
    notes = await run(w, state, T0 + timedelta(minutes=5))
    assert [n.summary for n in notes] == ["Sonarr: Unable to communicate with qBittorrent"]
    assert notes[0].embed.title == "🟠 Sonarr error"
    s.health_rows[1]["message"] = "Unable to communicate with qBittorrent (timeout)"  # same check, new wording
    assert await run(w, state, T0 + timedelta(minutes=10)) == []
    s.health_rows = s.health_rows[:1]
    notes = await run(w, state, T0 + timedelta(minutes=15))
    assert [n.kind for n in notes] == ["media_health_cleared"]
    assert notes[0].summary == "Sonarr resolved: Unable to communicate with qBittorrent (timeout)"


async def test_health_error_urgency_follows_threshold():
    s = FakeArr()
    w, state = watcher(s, mention_min_level="ERROR"), State()
    await run(w, state, T0)
    s.health_rows = [{"source": "DownloadClientCheck", "type": "error", "message": "x"}]
    assert (await run(w, state, T0 + timedelta(minutes=5)))[0].urgent is True


# ------------------------------------------------------------- reachability

async def test_unreachable_after_threshold_then_back():
    s = FakeArr()
    w, state = watcher(s, unreachable_after=3), State()
    s.exc = ArrUnavailable("Sonarr unreachable at http://192.168.1.158:8989: Connection refused")
    assert await run(w, state, T0) == []
    assert await run(w, state, T0 + timedelta(minutes=5)) == []
    notes = await run(w, state, T0 + timedelta(minutes=10))
    assert kinds(notes) == ["media_down"] and "Connection refused" in notes[0].embed.description
    assert await run(w, state, T0 + timedelta(minutes=15)) == []  # once
    assert w.last_ok["sonarr"] is False
    s.exc = None
    notes = await run(w, state, T0 + timedelta(minutes=30))
    assert kinds(notes) == ["media_up"] and "20m" in notes[0].embed.description
    assert state.media_failures == {} and state.media_down_since == {}


async def test_bad_key_is_reported_immediately():
    s = FakeArr()
    s.exc = ArrAuthError("Sonarr rejected the API key (HTTP 401)")
    notes = await run(watcher(s), State(), T0)
    assert kinds(notes) == ["media_down"] and "rejected" in notes[0].embed.description


async def test_quiet_while_whole_nas_is_down():
    s = FakeArr()
    s.exc = ArrUnavailable("refused")
    w, state = watcher(s, unreachable_after=1), State()
    assert await run(w, state, T0, nas_down=True) == []
    assert state.media_failures["sonarr"] == 1 and state.media_down_since == {}
    s.exc = None
    assert await run(w, state, T0 + timedelta(minutes=5)) == []  # back with the NAS: nothing to say


async def test_call_error_is_logged_not_counted():
    s = FakeArr()
    s.exc = ArrCallError("Sonarr /queue didn't return JSON")
    w, state = watcher(s, unreachable_after=1), State()
    assert await run(w, state, T0) == []
    assert state.media_failures == {} and "JSON" in w.last_error["sonarr"]


async def test_one_app_crashing_does_not_stop_the_other(monkeypatch):
    s, r = FakeArr("sonarr"), FakeArr("radarr")
    r.rows = [radarr_row(1, download_id="D", release="Dune", title="Dune", year=2021, state="importBlocked",
                         status="warning")]

    async def boom():
        raise RuntimeError("bug")
    monkeypatch.setattr(s, "queue", boom)
    notes = await run(watcher(s, r, media_grace_minutes=0), State(), T0)
    assert [n.summary for n in notes] == ["Radarr: needs manual import — Dune (2021)"]


# ------------------------------------------------------------------ imports

def ev(subject, season, episode, minute, did="G5", quality="WEBDL-1080p"):
    return ImportEvent("sonarr", did, subject, T0 + timedelta(minutes=minute), season, episode, quality)


async def test_imports_start_from_now_then_group_by_download():
    s = FakeArr()
    s.imports = [ev("Old", 1, 1, -60, did="OLD")]
    w, state = watcher(s, media_notify_imports=True), State()
    assert await run(w, state, T0) == []  # first run only sets the cursor
    assert state.media_import_cursor["sonarr"] == T0.isoformat()
    s.imports += [ev("Glee", 5, e, 3) for e in range(1, 23)] + [ev("Dune", 1, 1, 4, did="X")]
    notes = await run(w, state, T0 + timedelta(minutes=5))
    assert [n.summary for n in notes] == ["Sonarr: ready — Glee", "Sonarr: ready — Dune"]
    assert notes[0].embed.title == "📺 Ready to watch: Glee"
    assert notes[0].embed.description == "S05E01–E22 (22 episodes) · WEBDL-1080p"
    assert state.media_import_cursor["sonarr"] == (T0 + timedelta(minutes=4)).isoformat()
    assert await run(w, state, T0 + timedelta(minutes=10)) == []  # nothing new since the cursor


async def test_imports_off_by_default_and_burst():
    s = FakeArr()
    w, state = watcher(s), State()
    await run(w, state, T0)
    assert s.import_calls == [] and state.media_import_cursor == {}
    w2, state2 = watcher(s, media_notify_imports=True), State()
    await run(w2, state2, T0)
    s.imports = [ev(f"Show {i}", 1, 1, 2, did=f"D{i}") for i in range(7)]
    notes = await run(w2, state2, T0 + timedelta(minutes=5))
    assert len(notes) == 1 and notes[0].embed.title == "📺 7 new downloads ready to watch"


# ------------------------------------------------------- monitor integration

async def test_watcher_runs_on_its_own_interval_inside_monitor(api, clock: Clock):
    s = FakeArr()
    s.rows = black_clover_pack()
    cfg = make_cfg(media_grace_minutes=0, media_poll_interval=300)
    w = MediaWatcher([s], cfg)  # type: ignore[list-item]
    store = MemoryStateStore()
    m = Monitor(api, cfg, store, now=clock, media=w)
    notes = await m.poll()  # baseline for TrueNAS, but media announces right away (grace 0)
    assert kinds(notes) == ["media_attention"]
    assert store.state.media_attention["sonarr"]["BC-HASH"]["notified"] is True  # persisted
    s.rows = []
    clock.advance(seconds=60)
    assert await m.poll() == []  # media not due yet
    clock.advance(seconds=240)
    assert kinds(await m.poll()) == ["media_resolved"]


async def test_mute_holds_media_notifications_too(api, clock: Clock):
    s = FakeArr()
    cfg = make_cfg(media_grace_minutes=0)
    m = Monitor(api, cfg, MemoryStateStore(), now=clock, media=MediaWatcher([s], cfg))  # type: ignore[list-item]
    await m.poll()
    m.set_silence(60)
    s.rows = black_clover_pack()
    clock.advance(minutes=5)
    assert await m.poll() == []
    assert m.state.suppressed == ["Sonarr: needs manual import — Black Clover"]


async def test_media_still_checked_when_truenas_api_is_down(api, clock: Clock):
    s = FakeArr()
    cfg = make_cfg(unreachable_after=1)
    w = MediaWatcher([s], cfg)  # type: ignore[list-item]
    m = Monitor(api, cfg, MemoryStateStore(), now=clock, media=w)
    assert await m.poll() == []
    api.down = True  # the whole box goes away: TrueNAS API and Sonarr together
    s.exc = ArrUnavailable("refused")
    clock.advance(minutes=5)
    notes = await m.poll()
    # the NAS-down alert is the one message; Sonarr being unreachable with it is implied
    assert kinds(notes) == ["nas_down"]
    assert m.state.media_failures == {"sonarr": 1} and w.last_ok["sonarr"] is False
    api.down = False
    s.exc = None
    clock.advance(minutes=5)
    assert kinds(await m.poll()) == ["nas_up"]  # and no separate "Sonarr is back"


async def test_media_crash_never_breaks_nas_polling(api, clock: Clock, monkeypatch):
    cfg = make_cfg()
    w = MediaWatcher([FakeArr()], cfg)  # type: ignore[list-item]

    async def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(w, "check", boom)
    m = Monitor(api, cfg, MemoryStateStore(), now=clock, media=w)
    assert await m.poll() == [] and m.last_poll_ok


def test_state_roundtrip_and_old_files_still_load():
    st = State(media_attention={"sonarr": {"K": {"first_seen": 1.0, "notified": True, "reason": "import"}}},
               media_health={"radarr": {}}, media_failures={"sonarr": 2}, media_down_since={"sonarr": 5.0},
               media_import_cursor={"sonarr": T0.isoformat()})
    assert State.from_json(st.to_json()) == st
    legacy = {"version": 1, "known_alerts": {}, "hostname": "truenas"}  # written by nasbot 1.0
    loaded = State.from_json(legacy)
    assert loaded.media_attention == {} and loaded.media_health == {}


async def test_overview_and_close():
    s, r = FakeArr("sonarr"), FakeArr("radarr")
    s.rows = black_clover_pack() + [sonarr_row(9, download_id="G", release="Glee", series="Glee", season=5,
                                               episode=1, state="downloading", status="warning")]
    s.health_rows = [{"source": "A", "type": "warning", "message": "m"}, {"source": "B", "type": "notice",
                                                                          "message": "n"}]
    r.exc = ArrUnavailable("Radarr unreachable")
    w = watcher(s, r)
    ov = await w.overview()
    assert [o.instance.name for o in ov] == ["Sonarr", "Radarr"]
    assert sorted(reason or "-" for _, reason in ov[0].releases) == ["import", "stalled"]
    assert [h.source for h in ov[0].health] == ["A"] and ov[1].error == "Radarr unreachable"
    await w.close()
    assert s.closed and r.closed


def test_due_and_from_config():
    w = watcher(FakeArr(), media_poll_interval=300)
    assert w.due(T0)
    w.last_check = T0
    assert not w.due(T0 + timedelta(seconds=299)) and w.due(T0 + timedelta(seconds=300))
    assert MediaWatcher.from_config(make_cfg()) is None
    cfg = make_cfg(arr_instances=(ArrInstance("sonarr", "http://x:8989", "k"),))
    built = MediaWatcher.from_config(cfg)
    assert built is not None and built.names == ["Sonarr"]
