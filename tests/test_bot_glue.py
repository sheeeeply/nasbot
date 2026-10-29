"""The thin discord.py layer: interaction checks, deferral/follow-up, the background loop, command sync."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest

from nasbot import bot as bot_mod
from nasbot.bot import NASBot
from nasbot.monitor import Monitor
from nasbot.state import MemoryStateStore
from nasbot.truenas import NASUnavailable

from .conftest import make_cfg
from .test_bot import FakeChannel


class FakeResponse:
    def __init__(self):
        self.deferred = None
        self.messages = []

    async def defer(self, **kw):
        self.deferred = kw

    async def send_message(self, *args, **kw):
        self.messages.append((args, kw))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


def interaction(user_id=1, roles=(), guild_id=500, command="status"):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=r) for r in roles], __str__=lambda s: "user"),
        guild_id=guild_id,
        response=FakeResponse(),
        followup=FakeFollowup(),
        command=SimpleNamespace(qualified_name=f"nas {command}"),
    )


def make_bot(api, clock, channel=None, **cfg_kw) -> NASBot:
    cfg = make_cfg(**cfg_kw)
    b = NASBot(cfg, api, Monitor(api, cfg, MemoryStateStore(), now=clock))
    ch = channel or FakeChannel()

    async def _channel():
        return ch
    b._alert_channel = _channel  # type: ignore[method-assign]
    b._test_channel = ch
    return b


def group_of(b: NASBot):
    return next(c for c in b.tree.get_commands() if c.name == "nas")


async def test_interaction_check_allows_and_denies(api, clock):
    g = group_of(make_bot(api, clock, allowed_user_ids=frozenset({1})))
    ok = interaction(user_id=1)
    assert await g.interaction_check(ok) is True and ok.response.messages == []
    bad = interaction(user_id=2)
    assert await g.interaction_check(bad) is False
    (args, kw), = bad.response.messages
    assert "not allowed" in args[0] and kw["ephemeral"] is True


@pytest.mark.parametrize("name", ["status", "pools", "disks", "alerts", "apps", "ping"])
async def test_each_command_defers_then_follows_up(api, clock, name):
    b = make_bot(api, clock, ephemeral_replies=True)
    g = group_of(b)
    inter = interaction(command=name)
    cmd = g.get_command(name)
    await cmd.callback(g, inter)
    assert inter.response.deferred == {"ephemeral": True, "thinking": True}
    (sent,) = inter.followup.sent
    assert isinstance(sent["embed"], discord.Embed) and sent["ephemeral"] is True


async def test_command_errors_become_embeds(api, clock, monkeypatch):
    b = make_bot(api, clock)
    g = group_of(b)
    api.down = True
    inter = interaction()
    await g.get_command("status").callback(g, inter)
    assert inter.followup.sent[0]["embed"].title == "🔴 Can't reach the NAS"

    async def boom():
        raise RuntimeError("kaput")
    monkeypatch.setattr(b.handlers, "status", boom)
    inter = interaction()
    await g.get_command("status").callback(g, inter)
    assert inter.followup.sent[0]["embed"].title == "⚠️ Something went wrong"


async def test_mute_command_posts_summary_on_unmute(api, clock):
    b = make_bot(api, clock)
    g = group_of(b)
    inter = interaction(command="mute")
    await g.get_command("mute").callback(g, inter, 60)
    assert inter.response.messages[0][1]["embed"].title == "🔕 Notifications muted"
    inter = interaction(command="mute")
    await g.get_command("mute").callback(g, inter, 0)
    assert b._test_channel.sent[-1]["embed"].title.startswith("🔔 Notifications resumed")


async def test_monitor_loop_one_iteration(api, clock, tmp_path, monkeypatch):
    b = make_bot(api, clock, data_dir=tmp_path, healthcheck_url="https://hc.example/uuid", poll_interval=15)
    pings = []

    async def fake_ping(session, url, ok):
        pings.append((url, ok))
        return True

    async def ready():
        return None

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        raise asyncio.CancelledError

    monkeypatch.setattr(bot_mod.heartbeat, "ping", fake_ping)
    monkeypatch.setattr(b, "wait_until_ready", ready)
    monkeypatch.setattr(bot_mod.asyncio, "sleep", fake_sleep)
    b._http = object()  # truthy placeholder; ping is faked
    with pytest.raises(asyncio.CancelledError):
        await b._monitor_loop()
    assert b._test_channel.sent[0]["embed"].title == "🤖 NAS bot online"
    assert (tmp_path / "heartbeat").exists()
    assert pings == [("https://hc.example/uuid", True)] and sleeps == [15]


async def test_monitor_loop_survives_crashes(api, clock, tmp_path, monkeypatch):
    b = make_bot(api, clock, data_dir=tmp_path, announce_startup=False)
    calls = 0

    async def exploding_poll():
        nonlocal calls
        calls += 1
        raise RuntimeError("unexpected")

    async def ready():
        return None

    async def fake_sleep(_):
        if calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(b.monitor, "poll", exploding_poll)
    monkeypatch.setattr(b, "wait_until_ready", ready)
    monkeypatch.setattr(bot_mod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await b._monitor_loop()
    assert calls == 2 and b._test_channel.sent == []


@pytest.mark.parametrize("guild_id", [None, 4242])
async def test_setup_hook_syncs_commands(api, clock, monkeypatch, guild_id):
    b = make_bot(api, clock, guild_id=guild_id)
    synced = []

    async def fake_sync(*, guild=None):
        synced.append(guild.id if guild else None)
        return [object()]

    async def never():
        await asyncio.Event().wait()

    monkeypatch.setattr(b.tree, "sync", fake_sync)
    monkeypatch.setattr(b, "_monitor_loop", never)
    await b.setup_hook()
    assert synced == [guild_id]
    if guild_id:
        assert [c.name for c in b.tree.get_commands(guild=discord.Object(guild_id))] == ["nas"]
    b._monitor_task.cancel()
    await b._http.close()


async def test_on_ready_logs_invite(api, clock, caplog, monkeypatch):
    caplog.set_level("INFO")
    b = make_bot(api, clock)
    b._connection.user = SimpleNamespace(id=123456789012345678, __str__=lambda s: "nasbot#0001")
    monkeypatch.setattr(type(b), "user", property(lambda s: s._connection.user))
    await b.on_ready()
    assert "client_id=123456789012345678" in caplog.text and "scope=bot+applications.commands" in caplog.text
    assert "anyone in the server" in caplog.text


async def test_error_embed_footer_mentions_last_poll(api, clock):
    b = make_bot(api, clock)
    await b.monitor.poll()
    e = bot_mod.error_embed(NASUnavailable("x"), b.monitor)
    assert e.footer.text == "Last background poll succeeded"
