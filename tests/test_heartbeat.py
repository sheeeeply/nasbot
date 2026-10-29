import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from nasbot import heartbeat
from nasbot.__main__ import main


@pytest.mark.parametrize(("base", "ok", "expected"), [
    ("https://hc-ping.com/uuid", True, "https://hc-ping.com/uuid"),
    ("https://hc-ping.com/uuid", False, "https://hc-ping.com/uuid/fail"),
    ("https://hc-ping.com/uuid/", False, "https://hc-ping.com/uuid/fail"),
    ("http://kuma:3001/api/push/abc?status=up&msg=OK", False, "http://kuma:3001/api/push/abc?status=down&msg=OK"),
    ("http://kuma:3001/api/push/abc", True, "http://kuma:3001/api/push/abc?status=up"),
    ("http://kuma:3001/api/push/abc?msg=x", False, "http://kuma:3001/api/push/abc?msg=x&status=down"),
])
def test_ping_url(base, ok, expected):
    assert heartbeat.ping_url(base, ok) == expected


def test_touch_and_freshness(tmp_path):
    p = tmp_path / "sub" / "heartbeat"
    assert not heartbeat.is_fresh(p, 60)
    heartbeat.touch(p)
    assert heartbeat.is_fresh(p, 60)
    assert not heartbeat.is_fresh(p, 60, now=time.time() + 120)
    p.write_text("garbage")
    assert not heartbeat.is_fresh(p, 60)


async def test_ping_hits_server():
    hits = []

    async def handler(request):
        hits.append(request.path)
        return web.Response(status=200 if "ok" in request.path or "fail" in request.path else 404)

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    async with TestServer(app) as server:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            assert await heartbeat.ping(s, str(server.make_url("/ok")), True)
            assert await heartbeat.ping(s, str(server.make_url("/ok")), False)
            assert not await heartbeat.ping(s, str(server.make_url("/missing")), True)
            assert not await heartbeat.ping(s, "http://127.0.0.1:1/unreachable", True)
    assert hits == ["/ok", "/ok/fail", "/missing"]


def test_healthcheck_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["healthcheck"]) == 1
    heartbeat.touch(tmp_path / "heartbeat")
    assert main(["healthcheck"]) == 0
    assert "healthy" in capsys.readouterr().out
