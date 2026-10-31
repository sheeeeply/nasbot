"""/media commands, media embeds, and the whole path end to end: real ArrClient over HTTP
(tests/mock_arr.py) → MediaWatcher → Monitor → NASBot.post → channel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
import pytest

from nasbot import formatting as f
from nasbot.arr import ArrAuthError, ArrClient, ArrInstance, ArrUnavailable, ImportEvent, QueueItem, group_releases
from nasbot.bot import MediaGroup, NASBot, error_embed
from nasbot.media import AppOverview, MediaWatcher
from nasbot.monitor import Monitor
from nasbot.state import MemoryStateStore

from .conftest import make_cfg
from .mock_arr import API_KEY, MockArr, black_clover_pack, radarr_row, sonarr_row
from .test_bot import FakeChannel
from .test_bot_glue import interaction

SONARR = ArrInstance("sonarr", "http://192.168.1.158:8989", "k", link_url="http://nas.soay-terrapin.ts.net:8989")
RADARR = ArrInstance("radarr", "http://192.168.1.158:7878", "k")


# ------------------------------------------------------------------ formatting

@pytest.mark.parametrize(("eps", "expected"), [
    ([(1, 4)], "S01E04"),
    ([(1, 4), (1, 6), (1, 7), (1, 8), (1, 12)], "S01E04, E06–E08, E12"),
    ([(5, e) for e in range(1, 23)], "S05E01–E22"),
    ([(1, 124), (1, 125), (1, 170)], "S01E124–E125, E170"),
    ([(1, 9), (1, 10), (2, 1)], "S01E09–E10 · S02E01"),
    ([(1, 2), (1, 2)], "S01E02"),
])
def test_episode_ranges(eps, expected):
    assert f.episode_ranges(eps) == expected


def test_episode_ranges_truncates():
    assert len(f.episode_ranges([(1, e) for e in range(1, 400, 2)], limit=50)) == 50


def rel_from(rows, kind="sonarr"):
    return group_releases([QueueItem.parse(kind, r) for r in rows])[0]


def test_attention_embed_uses_link_url_and_lists_problems():
    e = f.attention_embed(SONARR, rel_from(black_clover_pack(124, 170)), "import")
    assert e.title == "📥 Needs manual import: Black Clover"
    assert e.url == "http://nas.soay-terrapin.ts.net:8989/activity/queue"
    assert "`[AnimeDevil] Black Clover 124-170" in e.description and "Sonarr → Activity → Queue" in e.description
    assert e.footer.text == "Sonarr · qBittorrent"
    assert len(e) <= 6000


def test_attention_embed_many_messages_are_capped():
    rows = [sonarr_row(1, download_id="D", release="R", series="S", season=1, episode=1, state="importBlocked",
                       status="warning", messages=[{"title": f"f{i}", "messages": [f"problem {i}"]}
                                                   for i in range(9)])]
    field = next(x for x in f.attention_embed(SONARR, rel_from(rows), "import").fields if x.name.startswith("What"))
    assert field.value.endswith("…and 5 more")


def test_resolved_health_down_up_embeds():
    one = f.attention_resolved_embed(SONARR, [{"subject": "Glee", "release": "Glee S01"}])
    assert one.title == "✅ Sorted: Glee" and "Glee S01" in one.description
    many = f.attention_resolved_embed(SONARR, [{"subject": "A"}, {"subject": "B"}])
    assert many.title == "✅ 2 Sonarr downloads sorted"
    from nasbot.arr import HealthIssue
    issue = HealthIssue("radarr", "IndexerStatusCheck", "warning", "Indexers unavailable: EZTV", None)
    h = f.arr_health_embed(RADARR, issue)
    assert h.title == "🟡 Radarr warning" and h.url == "http://192.168.1.158:7878/system/status"
    assert h.footer.text == "Radarr · IndexerStatusCheck"
    assert f.arr_health_embed(RADARR, issue, cleared=True).title.startswith("✅ Radarr resolved")
    down = f.arr_down_embed(SONARR, datetime(2026, 10, 31, tzinfo=UTC), "refused")
    assert down.title == "🔴 Sonarr is unreachable" and "refused" in down.description
    assert "1h" in f.arr_up_embed(SONARR, 3700).description


def test_imported_embeds():
    t = datetime(2026, 10, 31, 9, tzinfo=UTC)
    movie = [ImportEvent("radarr", "D", "Dune (2021)", t, quality="Bluray-1080p")]
    e = f.imported_embed(RADARR, movie)
    assert e.title == "🎬 Ready to watch: Dune (2021)" and e.description == "Bluray-1080p" and e.timestamp == t
    burst = f.imported_burst_embed(SONARR, [[ImportEvent("sonarr", "A", "Glee", t, 5, 1)],
                                            [ImportEvent("sonarr", "B", "Lost", None)]])
    assert burst.title == "📺 2 new downloads ready to watch" and "**Lost**" in burst.description


def test_media_queue_and_health_embeds():
    ok = AppOverview(SONARR, releases=[
        (rel_from(black_clover_pack()), "import"),
        (rel_from([sonarr_row(9, download_id="G", release="Glee S05", series="Glee", season=5, episode=1,
                              sizeleft=2.5e8)]), None),
    ])
    down = AppOverview(RADARR, error="Radarr unreachable at http://192.168.1.158:7878: refused")
    e = f.media_queue_embed([ok, down])
    assert e.title == "Media queue" and e.color.value == f.COLOR_CRIT
    sonarr_field, radarr_field = e.fields
    assert sonarr_field.name == "🟠 Sonarr" and "⚠️ 1 need attention" in sonarr_field.value
    assert "📥 **Black Clover** — needs manual import" in sonarr_field.value and "75%" in sonarr_field.value
    assert radarr_field.name == "🔴 Radarr"
    empty = f.media_queue_embed([AppOverview(SONARR)])
    assert empty.fields[0].value == "Queue is empty." and empty.color.value == f.COLOR_OK

    from nasbot.arr import HealthIssue
    h = f.media_health_embed([AppOverview(SONARR, health=[HealthIssue("sonarr", "X", "error", "Disk full")]),
                              AppOverview(RADARR)])
    assert h.color.value == f.COLOR_ERROR and "Disk full" in h.fields[0].value
    assert h.fields[1].value == "No health issues."
    assert f.media_health_embed([AppOverview(RADARR)]).color.value == f.COLOR_OK


# --------------------------------------------------------------------- bot glue

def make_media_bot(api, clock, watcher: MediaWatcher | None, **cfg_kw) -> NASBot:
    cfg = make_cfg(**cfg_kw)
    b = NASBot(cfg, api, Monitor(api, cfg, MemoryStateStore(), now=clock, media=watcher))
    ch = FakeChannel()

    async def _channel():
        return ch
    b._alert_channel = _channel  # type: ignore[method-assign]
    b._test_channel = ch
    return b


def test_media_group_only_when_configured(api, clock):
    assert [c.name for c in make_media_bot(api, clock, None).tree.get_commands()] == ["nas"]
    cfg = make_cfg(arr_instances=(SONARR,))
    b = make_media_bot(api, clock, MediaWatcher.from_config(cfg))
    names = sorted(c.name for c in b.tree.get_commands())
    assert names == ["media", "nas"]
    group = next(c for c in b.tree.get_commands() if c.name == "media")
    assert isinstance(group, MediaGroup) and group.guild_only
    assert sorted(c.name for c in group.commands) == ["health", "queue"]


async def test_media_handlers_when_off(api, clock):
    b = make_media_bot(api, clock, None)
    assert (await b.handlers.media_queue()).title == "Media watching is off"
    assert (await b.handlers.media_health()).title == "Media watching is off"


def test_error_embeds_for_arr():
    assert error_embed(ArrAuthError("Sonarr rejected the API key (HTTP 401)")).title == "🔑 API key rejected"
    assert error_embed(ArrUnavailable("refused")).title == "🔴 Can't reach Sonarr/Radarr"


async def test_startup_notice_mentions_media(api, clock):
    cfg = make_cfg(arr_instances=(SONARR, RADARR))
    b = make_media_bot(api, clock, MediaWatcher.from_config(cfg), media_poll_interval=300)
    b.monitor.media.last_check = clock()  # not due: don't dial the (fake) LAN addresses
    await b.monitor.poll()
    assert "Also watching **Sonarr & Radarr** every 5 min" in b._startup_notice().embed.description
    await b.monitor.media.close()


# ---------------------------------------------------------------- end to end

@pytest.fixture
async def servers():
    s, r = MockArr("sonarr"), MockArr("radarr")
    s.url, r.url = await s.start(), await r.start()
    yield s, r
    await s.stop()
    await r.stop()


async def test_end_to_end_over_http(api, clock, servers):
    sonarr, radarr = servers
    sonarr.queue = black_clover_pack(124, 170) + [sonarr_row(500, download_id="G5", release="Glee S05",
                                                              series="Glee", season=5, episode=1)]
    radarr.queue = [radarr_row(1, download_id="D", release="Dune.2021.1080p", title="Dune", year=2021)]
    insts = (ArrInstance("sonarr", sonarr.url, API_KEY), ArrInstance("radarr", radarr.url, API_KEY))
    cfg_kw = dict(arr_instances=insts, media_grace_minutes=10, media_poll_interval=300, unreachable_after=2)
    cfg = make_cfg(**cfg_kw)
    watcher = MediaWatcher([ArrClient(i, timeout=5) for i in insts], cfg, now=clock)
    b = make_media_bot(api, clock, watcher, **cfg_kw)
    channel = b._test_channel

    async def tick(minutes: int):
        clock.advance(minutes=minutes)
        for n in await b.monitor.poll():
            await b.post(n)

    try:
        await tick(0)
        assert channel.sent == []  # Black Clover still inside the grace period
        await tick(5)
        assert channel.sent == []
        await tick(5)
        (msg,) = channel.sent
        assert msg["embed"].title == "📥 Needs manual import: Black Clover"
        assert {x.name: x.value for x in msg["embed"].fields}["Episodes"] == "S01E124–E170 (47 episodes)"

        # the slash command sees the same thing live
        group = next(c for c in b.tree.get_commands() if c.name == "media")
        inter = interaction(command="queue")
        await group.get_command("queue").callback(group, inter)
        queue_embed = inter.followup.sent[0]["embed"]
        assert [x.name for x in queue_embed.fields] == ["🟠 Sonarr", "🟢 Radarr"]

        # Radarr reports a health problem; Sonarr's import gets sorted
        radarr.health = [{"source": "DownloadClientCheck", "type": "error",
                          "message": "Unable to communicate with qBittorrent"}]
        sonarr.queue = sonarr.queue[-1:]
        await tick(5)
        assert [m["embed"].title for m in channel.sent[1:]] == ["✅ Sorted: Black Clover", "🟠 Radarr error"]

        # Radarr goes away: reported after 2 failed checks, then back
        await radarr.stop()
        await tick(5)
        await tick(5)
        assert channel.sent[-1]["embed"].title == "🔴 Radarr is unreachable"
        radarr.url = await radarr.start()  # new port: point the client at it
        watcher.clients[1].instance = ArrInstance("radarr", radarr.url, API_KEY)
        await tick(5)
        assert channel.sent[-1]["embed"].title == "🟢 Radarr is reachable again"
    finally:
        await watcher.close()


async def test_end_to_end_bad_key_and_command_error(api, clock, servers):
    sonarr, _ = servers
    inst = ArrInstance("sonarr", sonarr.url, "wrong-key")
    cfg = make_cfg(arr_instances=(inst,))
    watcher = MediaWatcher([ArrClient(inst, timeout=5)], cfg, now=clock)
    b = make_media_bot(api, clock, watcher, arr_instances=(inst,))
    try:
        for n in await b.monitor.poll():
            await b.post(n)
        assert b._test_channel.sent[-1]["embed"].title == "🔴 Sonarr is unreachable"
        assert "rejected the API key" in b._test_channel.sent[-1]["embed"].description
        group = next(c for c in b.tree.get_commands() if c.name == "media")
        inter = interaction(command="health")
        await group.get_command("health").callback(group, inter)
        assert "rejected the API key" in inter.followup.sent[0]["embed"].fields[0].value
    finally:
        await watcher.close()


async def test_check_command_media_section(servers, capsys, monkeypatch):
    from nasbot.__main__ import _check_media
    sonarr, radarr = servers
    sonarr.queue = black_clover_pack()
    sonarr.health = [{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers unavailable: EZTV"}]
    cfg = make_cfg(arr_instances=(ArrInstance("sonarr", sonarr.url, API_KEY),
                                  ArrInstance("radarr", radarr.url, "bad")))
    lines = []
    ok = await _check_media(cfg, lambda mark, what, detail: lines.append((mark, what, detail)))
    assert ok is False
    assert ("✔", "connect + API key", "Sonarr 4.0.15.2941") in lines
    assert ("✔", "queue", "1 download(s), 1 need attention now") in lines
    assert any(m == "✔" and w == "health" and "EZTV" in d for m, w, d in lines)
    assert any(m == "✘" and w == "API key" for m, w, _ in lines)
    assert "Settings → General → Security" in capsys.readouterr().out
    assert await _check_media(make_cfg(), lambda *a: None) is True


def test_embeds_fit_discord_limits():
    rows = black_clover_pack(1, 400)
    for row in rows:
        row["title"] = "X" * 600
    e = f.attention_embed(SONARR, rel_from(rows), "import")
    assert len(e.title) <= 256 and len(e.description) <= 4096 and len(e) <= 6000
    assert all(len(x.value) <= 1024 for x in e.fields)
    assert isinstance(e, discord.Embed)
    t = datetime(2026, 10, 31, tzinfo=UTC)
    burst = f.imported_burst_embed(SONARR, [[ImportEvent("sonarr", str(i), "Y" * 120, t + timedelta(minutes=i),
                                                         1, i)] for i in range(200)])
    assert len(burst.description) <= 4096
