"""Command handlers, access control and posting — no Discord connection needed."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from nasbot import collectors
from nasbot.bot import Handlers, NASBot, NASGroup, error_embed
from nasbot.monitor import Monitor, Notification
from nasbot.state import MemoryStateStore
from nasbot.truenas import NASCallError, NASUnavailable

from .conftest import make_cfg


def handlers(api, clock, **cfg_kw) -> Handlers:
    cfg = make_cfg(**cfg_kw)
    return Handlers(api, Monitor(api, cfg, MemoryStateStore(), now=clock), cfg)


# --------------------------------------------------------------- access control

@pytest.mark.parametrize(("cfg_kw", "user", "roles", "guild", "ok"), [
    ({}, 1, [], 500, True),  # open access inside a guild
    ({}, 1, [], None, False),  # never in DMs
    ({"guild_id": 500}, 1, [], 501, False),  # wrong server
    ({"allowed_user_ids": frozenset({1})}, 1, [], 500, True),
    ({"allowed_user_ids": frozenset({1})}, 2, [], 500, False),
    ({"allowed_role_ids": frozenset({77})}, 2, [5, 77], 500, True),
    ({"allowed_role_ids": frozenset({77})}, 2, [5], 500, False),
])
def test_authorization_matrix(api, clock, cfg_kw, user, roles, guild, ok):
    assert handlers(api, clock, **cfg_kw).authorized(user, roles, guild) is ok


# -------------------------------------------------------------------- commands

async def test_status_command(api, clock):
    e = await handlers(api, clock).status()
    assert e.title == "🔴 truenas — critical"


async def test_pools_disks_alerts_apps(api, clock):
    h = handlers(api, clock)
    assert [f.name for f in (await h.pools()).fields] == ["🟠 fast", "🟢 tank", "🟢 boot-pool"]  # sorted, boot last
    disks = await h.disks()
    assert "sdd" in disks.description
    assert ("disk.query", ([], {"extra": {"pools": True}})) in api.calls
    assert "(3)" in (await h.alerts()).title
    assert "(4)" in (await h.alerts(include_dismissed=True)).title
    assert "immich" in (await h.apps()).description


async def test_ping_reports_both_latencies_and_mute(api, clock):
    h = handlers(api, clock)
    await h.monitor.poll()
    h.monitor.set_silence(15)
    e = await h.ping(0.0421)
    assert "42 ms" in e.description and "3 ms" in e.description and "Muted until" in e.description
    assert "ok" in e.description
    api.down = True
    e = await h.ping(0.05)
    assert "🔴" in e.description and e.color.value == 0xE74C3C


def test_mute_command(api, clock):
    h = handlers(api, clock)
    embed, notes = h.mute(30)
    assert embed.title == "🔕 Notifications muted" and notes == []
    embed, notes = h.mute(0)
    assert embed.title == "🔔 Notifications unmuted" and [n.kind for n in notes] == ["silence_ended"]


def test_error_embeds():
    assert error_embed(NASUnavailable("refused")).title == "🔴 Can't reach the NAS"
    perm = error_embed(NASCallError("app.query failed: Not authorized"))
    assert "Readonly Admin" in perm.description
    assert error_embed(RuntimeError("x")).title == "⚠️ Something went wrong"


async def test_partial_snapshot_when_apps_denied(api, clock):
    api.errors["app.query"] = NASCallError("app.query failed: Not authorized")
    snap = await collectors.snapshot(api)
    assert snap.apps is None and snap.errors == {"apps": "permission denied for this API user"}
    e = await handlers(api, clock).status()
    assert e.fields[-1].name == "⚠️ Partial data"


async def test_snapshot_propagates_outage(api):
    api.down = True
    with pytest.raises(NASUnavailable):
        await collectors.snapshot(api)


# --------------------------------------------------------------------- posting

class FakeChannel:
    def __init__(self, exc: Exception | None = None):
        self.sent: list[dict] = []
        self.exc = exc

    async def send(self, **kwargs):
        if self.exc:
            raise self.exc
        self.sent.append(kwargs)


def bot_with_channel(api, clock, channel, **cfg_kw) -> NASBot:
    cfg = make_cfg(**cfg_kw)
    bot = NASBot(cfg, api, Monitor(api, cfg, MemoryStateStore(), now=clock))

    async def _channel():
        return channel
    bot._alert_channel = _channel  # type: ignore[method-assign]
    return bot


def note(urgent: bool) -> Notification:
    return Notification("x", discord.Embed(title="t"), "s", urgent=urgent)


async def test_post_mentions_only_when_urgent(api, clock):
    ch = FakeChannel()
    bot = bot_with_channel(api, clock, ch, mention_role_id=99)
    await bot.post(note(urgent=False))
    await bot.post(note(urgent=True))
    quiet, loud = ch.sent
    assert quiet["content"] is None and quiet["allowed_mentions"].roles is False
    assert loud["content"] == "<@&99>"
    am = loud["allowed_mentions"]
    assert [r.id for r in am.roles] == [99] and am.users is False and am.everyone is False


async def test_post_user_mention(api, clock):
    ch = FakeChannel()
    bot = bot_with_channel(api, clock, ch, mention_user_id=5)
    await bot.post(note(urgent=True))
    assert ch.sent[0]["content"] == "<@5>" and [u.id for u in ch.sent[0]["allowed_mentions"].users] == [5]


@pytest.mark.parametrize("exc", [
    discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access"),
    discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel"),
    discord.HTTPException(SimpleNamespace(status=500, reason="err"), "boom"),
])
async def test_post_failures_are_logged_not_raised(api, clock, caplog, exc):
    bot = bot_with_channel(api, clock, FakeChannel(exc))
    await bot.post(note(urgent=False))
    assert "s" in caplog.text


async def test_startup_notice(api, clock):
    bot = bot_with_channel(api, clock, FakeChannel(), mention_role_id=9)
    await bot.monitor.poll()
    n = bot._startup_notice()
    assert n.kind == "startup" and "Watching **truenas**" in n.embed.description
    assert "2 active alert(s) at warning" in n.embed.description and "<@&9>" in n.embed.description
    api.down = True
    bot2 = bot_with_channel(api, clock, FakeChannel())
    await bot2.monitor.poll()
    assert "can't reach TrueNAS yet" in bot2._startup_notice().embed.description


def test_command_tree_shape(api, clock):
    bot = bot_with_channel(api, clock, FakeChannel())
    group = next(c for c in bot.tree.get_commands() if c.name == "nas")
    assert isinstance(group, NASGroup) and group.guild_only
    assert sorted(c.name for c in group.commands) == ["alerts", "apps", "disks", "mute", "ping", "pools", "status"]
    mute = group.get_command("mute")
    param = mute.parameters[0]
    assert param.name == "minutes" and param.min_value == 0 and param.max_value == 10080
    payload = group.to_dict(bot.tree)
    assert payload["name"] == "nas" and len(payload["options"]) == 7
