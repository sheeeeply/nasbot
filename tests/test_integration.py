"""End to end: the real official truenas_api_client, through nasbot's wrapper,
against a protocol-faithful mock middlewared (tests/mock_truenas.py)."""

from __future__ import annotations

import asyncio
import os
import ssl
import subprocess
import sys
import time

import pytest

from nasbot import collectors
from nasbot.config import from_env
from nasbot.monitor import Monitor
from nasbot.state import MemoryStateStore
from nasbot.truenas import NASAuthError, NASCallError, NASUnavailable, TrueNAS

from .conftest import Clock
from .fixtures import alert
from .mock_truenas import API_KEY, USERNAME, MockTrueNAS


@pytest.fixture
def server():
    srv = MockTrueNAS().start()
    yield srv
    srv.stop()


def env_for(srv: MockTrueNAS, **extra) -> dict:
    return {"TRUENAS_URL": srv.url, "TRUENAS_USERNAME": USERNAME, "TRUENAS_API_KEY": API_KEY,
            "TRUENAS_CALL_TIMEOUT": "5",
            "TRUENAS_ALLOW_INSECURE": "true",  # mock speaks ws://; never do this against a real NAS
            **extra}


def api_for(srv: MockTrueNAS, **extra) -> TrueNAS:
    cfg = from_env(env_for(srv, **extra), require_discord=False)
    return TrueNAS(cfg, base_backoff=0, auth_backoff=0)


async def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_full_snapshot_over_real_client(server):
    api = api_for(server)
    try:
        snap = await collectors.snapshot(api, with_disks=True)
        assert snap.system.hostname == "truenas" and snap.system.boottime is not None
        assert [p.name for p in snap.pools] == ["fast", "tank"] and snap.boot_pool.name == "boot-pool"
        assert len(snap.alerts) == 4 and len(snap.apps) == 4 and len(snap.disks) == 5
        assert snap.errors == {}
        assert snap.health == "CRIT"
        await wait_for(lambda: api.realtime() is not None)
        assert api.realtime().cpu_percent == 7.25
        assert server.logins == 1
        assert {"core.set_options", "auth.login_ex"} <= set(server.calls)
    finally:
        await api.close()


async def test_wrong_key_is_an_auth_error(server):
    api = api_for(server, TRUENAS_API_KEY="7-wrongWrongWrong")
    with pytest.raises(NASAuthError):
        await api.call("system.info")
    await api.close()


async def test_permission_denied_is_a_call_error(server):
    server.denied.add("app.query")
    api = api_for(server)
    try:
        with pytest.raises(NASCallError, match="Not authorized"):
            await api.call("app.query")
        assert api.connected  # connection survives a permission error
        snap = await collectors.snapshot(api)
        assert snap.apps is None and "permission denied" in snap.errors["apps"]
    finally:
        await api.close()


async def test_reconnects_after_server_drops_connection(server):
    api = api_for(server)
    try:
        await api.call("system.info")
        server.drop_connections()
        await asyncio.sleep(0.2)
        with pytest.raises(NASUnavailable):
            await api.call("system.info")
        info = await collectors.system_info(api)  # transparently reconnects and re-authenticates
        assert info.hostname == "truenas"
        assert server.logins == 2
        await wait_for(lambda: api.realtime() is not None)  # realtime re-subscribed
    finally:
        await api.close()


async def test_monitor_lifecycle_against_mock(server):
    clock = Clock()
    cfg = from_env(env_for(server) | {"DISCORD_TOKEN": "x", "DISCORD_ALERT_CHANNEL_ID": "1",
                                      "UNREACHABLE_AFTER_FAILURES": "2"})
    api = TrueNAS(cfg, base_backoff=0)
    m = Monitor(api, cfg, MemoryStateStore(), now=clock)
    try:
        assert await m.poll() == []  # baseline

        server.responses["alert.list"].append(alert("live-1", "CRITICAL", "ZpoolCapacityCritical",
                                                    "Space usage for pool tank is 95%."))
        notes = await m.poll()
        assert [(n.kind, n.urgent) for n in notes] == [("alert_raised", True)]

        server.responses["app.query"][0]["state"] = "CRASHED"
        notes = await m.poll()
        assert [n.summary for n in notes] == ["App immich: running → crashed"]

        # NAS goes away for two polls
        server.reject_connections = True
        server.drop_connections()
        await asyncio.sleep(0.2)
        clock.advance(minutes=1)
        assert await m.poll() == []
        clock.advance(minutes=1)
        assert [n.kind for n in await m.poll()] == ["nas_down"]

        # ...and comes back after a reboot, with the capacity alert gone
        server.reject_connections = False
        server.responses["system.info"]["boottime"] = {"$date": int(time.time() * 1000)}
        server.responses["alert.list"] = [a for a in server.responses["alert.list"] if a["uuid"] != "live-1"]
        clock.advance(minutes=3)
        notes = await m.poll()
        assert [n.kind for n in notes] == ["nas_up", "alert_cleared"]
        # downtime is measured from the first failed poll (1 + 3 minutes)
        assert "rebooted" in notes[0].embed.description and "4m" in notes[0].embed.description
    finally:
        await api.close()


def run_cli(args: list[str], env: dict) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(("DISCORD_", "TRUENAS_"))} | env
    return subprocess.run([sys.executable, "-m", "nasbot", *args], env=full_env, capture_output=True,
                          text=True, timeout=60)


def test_check_cli_success(server):
    result = run_cli(["check"], env_for(server))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    for needle in ("✔ connect + login", "truenas · TrueNAS 25.10.2", "fast DEGRADED 90%", "tank ONLINE 31%",
                   "4 total, 2 would be reported", "4 apps", "5 disks, max 52.0°C", "CPU 7%", "All good."):
        assert needle in out, out


def test_check_cli_bad_key(server):
    result = run_cli(["check"], env_for(server, TRUENAS_API_KEY="7-nope"))
    assert result.returncode == 1 and "✘ login" in result.stdout and "TRUENAS_AUTH" in result.stdout


def test_check_cli_unreachable():
    result = run_cli(["check"], {"TRUENAS_URL": "ws://127.0.0.1:9/api/current", "TRUENAS_USERNAME": "u",
                                 "TRUENAS_API_KEY": "1-x", "TRUENAS_CALL_TIMEOUT": "3",
                                 "TRUENAS_ALLOW_INSECURE": "true"})
    assert result.returncode == 1 and "✘ connect" in result.stdout


def test_check_cli_missing_permission(server):
    server.denied.add("pool.query")
    result = run_cli(["check"], env_for(server))
    assert result.returncode == 1 and "✘ pool.query" in result.stdout


def test_run_cli_reports_config_errors():
    result = run_cli(["run"], {})
    assert result.returncode == 2 and "DISCORD_TOKEN" in result.stderr and "TRUENAS_URL" in result.stderr


async def test_check_in_process(server, capsys):
    from nasbot.__main__ import _check
    cfg = from_env(env_for(server), require_discord=False)
    assert await _check(cfg) == 0
    assert "All good." in capsys.readouterr().out
    server.denied.add("app.query")
    assert await _check(cfg) == 0  # apps are optional
    assert "! app.query" in capsys.readouterr().out
    server.denied.add("alert.list")
    assert await _check(cfg) == 1


def test_run_cli_bad_discord_token_is_friendly(server):
    """Talks to the real Discord API, which rejects the token; skip when offline."""
    result = run_cli(["run"], env_for(server) | {"DISCORD_TOKEN": "bogus.token.value", "DISCORD_ALERT_CHANNEL_ID": "1",
                                                 "DATA_DIR": "/tmp/nasbot-test-data"})
    if "Cannot connect to host" in result.stderr or "ClientConnectorError" in result.stderr:
        pytest.skip("no network access to discord.com")
    assert result.returncode == 2, result.stderr
    assert "Discord rejected DISCORD_TOKEN" in result.stderr and "Traceback" not in result.stderr


@pytest.fixture(scope="module")
def self_signed(tmp_path_factory):
    """A throwaway self-signed certificate, like the one TrueNAS ships with."""
    d = tmp_path_factory.mktemp("tls")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=truenas.local", "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem")],
                   check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "cert.pem", d / "key.pem")
    return ctx


@pytest.fixture
def tls_server(self_signed):
    srv = MockTrueNAS(ssl_context=self_signed).start()
    yield srv
    srv.stop()


def tls_env(srv: MockTrueNAS, **extra) -> dict:
    # Plain API-key auth over TLS, i.e. exactly the recommended 25.x setup; no insecure override needed.
    return {"TRUENAS_URL": srv.url, "TRUENAS_USERNAME": USERNAME, "TRUENAS_API_KEY": API_KEY,
            "TRUENAS_CALL_TIMEOUT": "5", **extra}


async def test_wss_with_self_signed_cert_and_verification_off(tls_server):
    cfg = from_env(tls_env(tls_server, TRUENAS_VERIFY_SSL="false"), require_discord=False)
    assert cfg.truenas_url.startswith("wss://")
    api = TrueNAS(cfg, base_backoff=0)
    try:
        info = await collectors.system_info(api)
        assert info.hostname == "truenas" and tls_server.logins == 1
        await wait_for(lambda: api.realtime() is not None)
    finally:
        await api.close()


async def test_wss_self_signed_rejected_when_verifying(tls_server):
    cfg = from_env(tls_env(tls_server), require_discord=False)
    api = TrueNAS(cfg, base_backoff=0)
    with pytest.raises(NASUnavailable, match="(?i)certificate"):
        await api.call("system.info")
    assert tls_server.logins == 0  # the key was never sent to an unverified peer
    await api.close()


def test_check_cli_explains_tls_failure(tls_server):
    result = run_cli(["check"], tls_env(tls_server))
    assert result.returncode == 1 and "TRUENAS_VERIFY_SSL=false" in result.stdout
