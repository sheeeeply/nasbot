"""A small but protocol-faithful mock of TrueNAS's JSON-RPC 2.0 websocket API.

It speaks enough of middlewared's dialect for the *official* truenas_api_client to
connect, run its handshake (core.set_options, core.get_jobs subscription,
auth.mechanism_choices), authenticate with API_KEY_PLAIN via auth.login_ex, call
methods, and receive reporting.realtime events. Runs in its own thread and loop
because the official client is synchronous.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import ssl
import threading
import uuid

from websockets.asyncio.server import ServerConnection, serve

from .fixtures import REALTIME_FIELDS, default_responses

TRUENAS_CALL_ERROR = -32001
METHOD_NOT_FOUND = -32601
EACCES = 13
ENOTAUTHENTICATED = 207  # middlewared ErrnoMixin.ENOTAUTHENTICATED

USERNAME = "nasbot"
API_KEY = "7-QxJ3hVfM9kTq2Lr8sYwZ4bNc6DpEa1Gu5Hi0Kj"


class MockTrueNAS:
    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self.ssl_context = ssl_context
        self.responses: dict = default_responses()
        self.denied: set[str] = set()
        self.reject_connections = False
        self.realtime_interval = 0.1
        self.calls: list[str] = []
        self.logins = 0
        self.port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._connections: set[ServerConnection] = set()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # ------------------------------------------------------------- lifecycle

    @property
    def url(self) -> str:
        scheme = "wss" if self.ssl_context else "ws"
        return f"{scheme}://127.0.0.1:{self.port}/api/current"

    def start(self) -> MockTrueNAS:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("mock server failed to start")
        return self

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _start():
            return await serve(self._handler, "127.0.0.1", 0, ssl=self.ssl_context)

        self._server = self._loop.run_until_complete(_start())
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()
        self._server.close()
        with contextlib.suppress(Exception):
            self._loop.run_until_complete(self._server.wait_closed())
        self._loop.close()

    # ---------------------------------------------------------- test controls

    def drop_connections(self) -> None:
        """Simulate the NAS vanishing mid-session: abrupt TCP reset, no close handshake."""
        async def _drop():
            for ws in list(self._connections):
                ws.transport.abort()
        asyncio.run_coroutine_threadsafe(_drop(), self._loop).result(5)

    def set(self, method: str, value) -> None:
        self.responses[method] = value

    # ------------------------------------------------------------- protocol

    async def _handler(self, ws: ServerConnection) -> None:
        if self.reject_connections or ws.request.path != "/api/current":
            await ws.close(code=1011, reason="unavailable")
            return
        self._connections.add(ws)
        session = {"authed": False, "tasks": []}
        try:
            async for raw in ws:
                msg = json.loads(raw)
                await self._dispatch(ws, session, msg)
        except Exception:  # noqa: BLE001 - connection torn down
            pass
        finally:
            for t in session["tasks"]:
                t.cancel()
            self._connections.discard(ws)

    async def _send(self, ws: ServerConnection, payload: dict) -> None:
        with contextlib.suppress(Exception):
            await ws.send(json.dumps(payload))

    async def _error(self, ws, mid, reason: str, errno: int) -> None:
        await self._send(ws, {"jsonrpc": "2.0", "id": mid, "error": {
            "code": TRUENAS_CALL_ERROR, "message": reason,
            "data": {"error": errno, "errname": "", "reason": reason, "trace": None, "extra": []},
        }})

    async def _result(self, ws, mid, result) -> None:
        await self._send(ws, {"jsonrpc": "2.0", "id": mid, "result": result})

    async def _dispatch(self, ws, session, msg: dict) -> None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or []
        self.calls.append(method)

        if method == "core.set_options":
            return await self._result(ws, mid, {"legacy_jobs": False, "private_methods": False,
                                                "py_exceptions": False})
        if method == "auth.mechanism_choices":
            return await self._result(ws, mid, ["API_KEY_PLAIN", "PASSWORD_PLAIN", "TOKEN_PLAIN", "OTP_TOKEN"])
        if method == "auth.login_ex":
            data = params[0]
            ok = (data.get("mechanism") == "API_KEY_PLAIN" and data.get("username") == USERNAME
                  and data.get("api_key") == API_KEY)
            session["authed"] = ok
            if ok:
                self.logins += 1
            return await self._result(ws, mid, {"response_type": "SUCCESS", "user_info": None,
                                                "authenticator": "LEVEL_1"} if ok else {"response_type": "AUTH_ERR"})
        if not session["authed"]:
            return await self._error(ws, mid, "Not authenticated", ENOTAUTHENTICATED)
        if method == "core.subscribe":
            sub_id = str(uuid.uuid4())
            await self._result(ws, mid, sub_id)
            if params and params[0].split(":")[0] == "reporting.realtime":
                session["tasks"].append(asyncio.create_task(self._realtime(ws)))
            return None
        if method == "core.unsubscribe":
            return await self._result(ws, mid, None)
        if method in self.denied:
            return await self._error(ws, mid, "Not authorized", EACCES)
        if method not in self.responses:
            return await self._send(ws, {"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": METHOD_NOT_FOUND, "message": "Method does not exist"}})
        return await self._result(ws, mid, copy.deepcopy(self.responses[method]))

    async def _realtime(self, ws) -> None:
        while True:
            await self._send(ws, {"jsonrpc": "2.0", "method": "collection_update", "params": {
                "msg": "added", "collection": "reporting.realtime", "id": None, "fields": REALTIME_FIELDS,
            }})
            await asyncio.sleep(self.realtime_interval)
