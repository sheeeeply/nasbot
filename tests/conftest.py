from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nasbot.config import Config, from_env
from nasbot.models import Realtime
from nasbot.truenas import NASCallError, NASUnavailable

from .fixtures import REALTIME_FIELDS, default_responses

BASE_ENV = {
    "DISCORD_TOKEN": "token",
    "DISCORD_ALERT_CHANNEL_ID": "1001",
    "TRUENAS_URL": "truenas.tailnet.ts.net",
    "TRUENAS_USERNAME": "nasbot",
    "TRUENAS_API_KEY": "1-abcDEF123",
}


def make_cfg(**overrides: Any) -> Config:
    cfg = from_env(BASE_ENV)
    return replace(cfg, **overrides)


@pytest.fixture
def cfg() -> Config:
    return make_cfg()


class FakeAPI:
    """Stands in for nasbot.truenas.TrueNAS: canned responses, injectable failures."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = default_responses()
        self.errors: dict[str, Exception] = {}
        self.down = False
        self.calls: list[tuple[str, tuple]] = []
        self.rt: Realtime | None = Realtime.parse(REALTIME_FIELDS)
        self.last_error: str | None = None

    async def call(self, method: str, *params: Any, timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        if self.down:
            raise NASUnavailable("connection refused")
        if method in self.errors:
            raise self.errors[method]
        if method not in self.responses:
            raise NASCallError(f"{method} failed: Method does not exist")
        return copy.deepcopy(self.responses[method])

    async def ping(self) -> float:
        await self.call("core.ping")
        return 3.2

    def realtime(self) -> Realtime | None:
        return self.rt

    async def close(self) -> None:
        pass


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


class Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 10, 27, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()
