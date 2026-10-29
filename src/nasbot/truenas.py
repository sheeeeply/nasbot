"""Async facade over the official (synchronous) TrueNAS API client.

* One persistent, authenticated websocket — iX explicitly asks pollers not to
  re-authenticate per call (20 auth attempts/min trips a 10-minute lockout).
* Blocking client calls run in worker threads so the Discord event loop never stalls.
* Broken connections are detected on the failing call and rebuilt lazily on the
  next one, with exponential backoff. Auth failures back off much harder so a
  wrong key can never trigger the server's rate-limit lockout.
* Live CPU/memory figures come from a ``reporting.realtime`` subscription that
  is (re)established with every connection.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .config import Config
from .models import Realtime

log = logging.getLogger(__name__)

REALTIME_EVENT = "reporting.realtime"
REALTIME_MAX_AGE = 30.0  # seconds before cached realtime stats count as stale


class NASUnavailable(Exception):
    """The NAS could not be reached (network, TLS, websocket, timeout, backoff)."""


class NASAuthError(NASUnavailable):
    """Connected, but the API key/username was rejected."""


class NASCallError(Exception):
    """The NAS answered, but the method failed (permissions, bad args, service off)."""


class SyncClient(Protocol):
    """The slice of truenas_api_client.Client this module relies on."""

    def call(self, method: str, *params: Any, timeout: float = ...) -> Any: ...
    def login_with_api_key(self, username: str, api_key: str, auth_mechanism: Any = ...,
                           *, channel_binding: bool = ...) -> None: ...
    def subscribe(self, name: str, callback: Callable[..., None]) -> str: ...
    def close(self) -> None: ...


ClientFactory = Callable[[Config], SyncClient]


def default_client_factory(cfg: Config) -> SyncClient:
    from truenas_api_client import Client

    return Client(uri=cfg.truenas_url, verify_ssl=cfg.truenas_verify_ssl, call_timeout=cfg.call_timeout)


def _default_login(client: SyncClient, cfg: Config) -> None:
    from truenas_api_client import APIKeyAuthMech

    mech = APIKeyAuthMech.SCRAM if cfg.truenas_auth == "scram" else APIKeyAuthMech.PLAIN
    client.login_with_api_key(cfg.truenas_username, cfg.truenas_api_key, mech,
                              channel_binding=cfg.truenas_channel_binding)


def _classify(exc: BaseException) -> type[Exception]:
    """Decide whether an exception means 'connection is dead' or 'call failed'."""
    from truenas_api_client.exc import CallTimeout, ClientException

    if isinstance(exc, CallTimeout | TimeoutError | ConnectionError | OSError):
        return NASUnavailable
    if isinstance(exc, ClientException):
        msg = str(exc).lower()
        # A dead socket, or a session that lost its auth (e.g. middlewared restarted):
        # both are fixed by reconnecting, so both count as "unavailable".
        reconnect_errnos = (errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE, ClientException.ENOTAUTHENTICATED)
        if getattr(exc, "errno", None) in reconnect_errnos \
                or "connection closed" in msg or "not connected" in msg or "not authenticated" in msg:
            return NASUnavailable
        return NASCallError
    name = type(exc).__name__
    if name.startswith("WebSocket") or "SSL" in name:
        return NASUnavailable
    return NASCallError


class TrueNAS:
    """Thread-safe, auto-reconnecting TrueNAS API connection."""

    def __init__(
        self,
        cfg: Config,
        *,
        client_factory: ClientFactory = default_client_factory,
        login: Callable[[SyncClient, Config], None] = _default_login,
        clock: Callable[[], float] = time.monotonic,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        auth_backoff: float = 600.0,
    ):
        self._cfg = cfg
        self._factory = client_factory
        self._login = login
        self._clock = clock
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._auth_backoff = auth_backoff

        self._client: SyncClient | None = None
        self._connect_lock = asyncio.Lock()
        self._failures = 0
        self._retry_at = 0.0
        self._last_error: str | None = None
        self._realtime: tuple[dict, float] | None = None
        self._realtime_lock = threading.Lock()

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def call(self, method: str, *params: Any, timeout: float | None = None) -> Any:
        client = await self._ensure_client()
        try:
            return await asyncio.to_thread(client.call, method, *params,
                                           timeout=timeout or self._cfg.call_timeout)
        except Exception as exc:  # noqa: BLE001 - re-raised as one of ours
            kind = _classify(exc)
            if kind is NASUnavailable:
                self._drop(client, f"{type(exc).__name__}: {exc}")
                raise NASUnavailable(f"lost connection during {method}: {exc}") from exc
            raise NASCallError(f"{method} failed: {exc}") from exc

    async def ping(self) -> float:
        """Round-trip time to middlewared in milliseconds."""
        start = time.perf_counter()
        await self.call("core.ping", timeout=10)
        return (time.perf_counter() - start) * 1000

    def realtime(self) -> Realtime | None:
        with self._realtime_lock:
            if not self._realtime:
                return None
            fields, at = self._realtime
        if self._clock() - at > REALTIME_MAX_AGE:
            return None
        return Realtime.parse(fields)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(self._safe_close, client)

    # ----------------------------------------------------------------- private

    async def _ensure_client(self) -> SyncClient:
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is not None:  # another task connected while we waited
                return self._client
            now = self._clock()
            if now < self._retry_at:
                wait = int(self._retry_at - now)
                raise NASUnavailable(f"reconnect backoff ({wait}s left); last error: {self._last_error}")
            try:
                client = await asyncio.to_thread(self._connect_blocking)
            except NASAuthError as exc:
                self._register_failure(str(exc), auth=True)
                raise
            except Exception as exc:  # noqa: BLE001
                self._register_failure(f"{type(exc).__name__}: {exc}")
                raise NASUnavailable(f"cannot connect to {self._cfg.truenas_url}: {exc}") from exc
            self._client = client
            if self._failures:
                log.info("Reconnected to TrueNAS after %d failed attempt(s)", self._failures)
            else:
                log.info("Connected to TrueNAS at %s", self._cfg.truenas_url)
            self._failures = 0
            self._retry_at = 0.0
            self._last_error = None
            return client

    def _connect_blocking(self) -> SyncClient:
        client = self._factory(self._cfg)
        try:
            self._login(client, self._cfg)
        except ValueError as exc:  # the official client signals bad key/username this way
            self._safe_close(client)
            raise NASAuthError(f"TrueNAS rejected the API key for user {self._cfg.truenas_username!r}: {exc}") \
                from exc
        except BaseException:
            self._safe_close(client)
            raise
        try:
            client.subscribe(REALTIME_EVENT, self._on_realtime)
        except Exception as exc:  # noqa: BLE001 - live stats are a nice-to-have
            log.warning("Realtime stats unavailable (%s); CPU/memory will be omitted", exc)
        return client

    def _on_realtime(self, mtype: str, **message: Any) -> None:
        fields = message.get("fields")
        if isinstance(fields, dict) and fields:
            with self._realtime_lock:
                self._realtime = (fields, self._clock())

    def _register_failure(self, error: str, *, auth: bool = False) -> None:
        self._failures += 1
        self._last_error = error
        delay = min(self._max_backoff, self._base_backoff * 2 ** (self._failures - 1))
        if auth:
            delay = max(delay, self._auth_backoff)
        self._retry_at = self._clock() + delay
        log.warning("TrueNAS connection failed (attempt %d, retry in %.0fs): %s", self._failures, delay, error)

    def _drop(self, client: SyncClient, reason: str) -> None:
        if self._client is client:
            self._client = None
            self._last_error = reason
            with self._realtime_lock:
                self._realtime = None
            log.warning("TrueNAS connection dropped: %s", reason)
            threading.Thread(target=self._safe_close, args=(client,), daemon=True).start()

    @staticmethod
    def _safe_close(client: SyncClient) -> None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
