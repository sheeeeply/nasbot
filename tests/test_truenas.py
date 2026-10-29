"""TrueNAS connection wrapper: login, reconnect, backoff, error classification, realtime cache."""

from __future__ import annotations

import asyncio
import errno
import threading

import pytest
from truenas_api_client import APIKeyAuthMech
from truenas_api_client.exc import CallTimeout, ClientException

from nasbot.truenas import NASAuthError, NASCallError, NASUnavailable, TrueNAS, _default_login

from .conftest import make_cfg
from .fixtures import REALTIME_FIELDS


class FakeSyncClient:
    def __init__(self, behavior: dict):
        self.behavior = behavior
        self.closed = False
        self.realtime_cb = None
        self.login_args = None

    def login_with_api_key(self, username, api_key, auth_mechanism=None, *, channel_binding=True):
        self.login_args = (username, api_key, auth_mechanism, channel_binding)
        if self.behavior.get("bad_key"):
            raise ValueError("Invalid API key")

    def subscribe(self, name, callback):
        if self.behavior.get("no_realtime"):
            raise ClientException("Not authorized", errno.EACCES)
        self.realtime_cb = callback
        return "sub-1"

    def call(self, method, *params, timeout=None):
        exc = self.behavior.get("raise", {}).get(method)
        if exc:
            raise exc
        return {"method": method, "params": params}

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, **behavior):
        self.behavior = behavior
        self.clients: list[FakeSyncClient] = []
        self.fail_connect: Exception | None = None
        self.lock = threading.Lock()

    def __call__(self, cfg):
        with self.lock:
            if self.fail_connect:
                raise self.fail_connect
            c = FakeSyncClient(self.behavior)
            self.clients.append(c)
            return c


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def recording_login(client, cfg):
    client.login_with_api_key(cfg.truenas_username, cfg.truenas_api_key, cfg.truenas_auth,
                              channel_binding=cfg.truenas_channel_binding)


def make(factory, clock=None, **cfg_kw):
    return TrueNAS(make_cfg(**cfg_kw), client_factory=factory, login=recording_login, clock=clock or FakeClock())


async def test_connects_lazily_and_reuses_connection():
    factory = Factory()
    api = make(factory)
    assert not api.connected and factory.clients == []
    r1 = await api.call("system.info")
    r2 = await api.call("pool.query", [], {"x": 1})
    assert r1["method"] == "system.info" and r2["params"] == ([], {"x": 1})
    assert len(factory.clients) == 1 and api.connected


async def test_concurrent_first_calls_share_one_connection():
    factory = Factory()
    api = make(factory)
    await asyncio.gather(*(api.call("core.ping") for _ in range(10)))
    assert len(factory.clients) == 1


def test_default_login_picks_mechanism():
    seen = []

    class C:
        def login_with_api_key(self, *a, **kw):
            seen.append((a, kw))

    _default_login(C(), make_cfg())
    _default_login(C(), make_cfg(truenas_auth="scram", truenas_channel_binding=False))
    assert seen[0] == (("nasbot", "1-abcDEF123", APIKeyAuthMech.PLAIN), {"channel_binding": True})
    assert seen[1] == (("nasbot", "1-abcDEF123", APIKeyAuthMech.SCRAM), {"channel_binding": False})


async def test_bad_key_raises_auth_error_and_backs_off_hard():
    factory = Factory(bad_key=True)
    clock = FakeClock()
    api = make(factory, clock)
    with pytest.raises(NASAuthError, match="rejected the API key"):
        await api.call("system.info")
    assert factory.clients[0].closed
    clock.t += 300  # well past normal backoff, still inside the auth backoff
    with pytest.raises(NASUnavailable, match="backoff"):
        await api.call("system.info")
    assert len(factory.clients) == 1  # no second login attempt: protects against the server's lockout
    clock.t += 301
    with pytest.raises(NASAuthError):
        await api.call("system.info")
    assert len(factory.clients) == 2


async def test_connect_failure_exponential_backoff():
    factory = Factory()
    factory.fail_connect = ConnectionRefusedError("refused")
    clock = FakeClock()
    api = make(factory, clock)
    delays = []
    for _ in range(8):
        with pytest.raises(NASUnavailable, match="cannot connect"):
            await api.call("x")
        delays.append(api._retry_at - clock.t)
        clock.t = api._retry_at
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]
    factory.fail_connect = None
    await api.call("x")
    assert api.last_error is None and api._failures == 0


@pytest.mark.parametrize("exc", [
    CallTimeout(),
    ClientException("WebSocket connection closed with code=1006", errno.ECONNABORTED),
    ClientException("Not authenticated", ClientException.ENOTAUTHENTICATED),
    BrokenPipeError(),
    type("WebSocketConnectionClosedException", (Exception,), {})("gone"),
])
async def test_connection_level_errors_drop_and_reconnect(exc):
    factory = Factory(**{"raise": {"system.info": exc}})
    api = make(factory)
    with pytest.raises(NASUnavailable):
        await api.call("system.info")
    assert not api.connected
    await asyncio.sleep(0.05)  # close runs on a daemon thread
    assert factory.clients[0].closed
    factory.behavior["raise"] = {}
    await api.call("system.info")  # base backoff not applied to drops: reconnect immediately
    assert len(factory.clients) == 2


@pytest.mark.parametrize("exc", [
    ClientException("Not authorized", errno.EACCES),
    ClientException("[ENOENT] pool not found", errno.ENOENT),
    ValueError("weird"),
])
async def test_method_errors_keep_connection(exc):
    factory = Factory(**{"raise": {"app.query": exc}})
    api = make(factory)
    with pytest.raises(NASCallError, match="app.query failed"):
        await api.call("app.query")
    assert api.connected and len(factory.clients) == 1


async def test_realtime_cache_and_staleness():
    factory = Factory()
    clock = FakeClock()
    api = make(factory, clock)
    await api.call("x")
    assert api.realtime() is None
    factory.clients[0].realtime_cb("ADDED", collection="reporting.realtime", fields=REALTIME_FIELDS)
    rt = api.realtime()
    assert rt and rt.cpu_percent == 7.25
    clock.t += 31
    assert api.realtime() is None  # stale
    factory.clients[0].realtime_cb("ADDED", fields={})  # empty payload ignored
    assert api.realtime() is None


async def test_realtime_subscription_failure_is_tolerated():
    factory = Factory(no_realtime=True)
    api = make(factory)
    assert (await api.call("x"))["method"] == "x"
    assert api.realtime() is None


async def test_ping_and_close():
    factory = Factory()
    api = make(factory)
    assert await api.ping() >= 0
    await api.close()
    assert factory.clients[0].closed and not api.connected
