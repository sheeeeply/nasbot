"""A small Sonarr/Radarr v3 API stand-in (aiohttp), faithful to the parts nasbot uses:
API-key header auth, paged /queue with the include* flags, /health, /history/since
and /system/status. Records are shaped like real Sonarr 4 / Radarr 5 responses."""

from __future__ import annotations

import copy
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

API_KEY = "0123456789abcdef0123456789abcdef"


class MockArr:
    def __init__(self, kind: str, api_key: str = API_KEY):
        self.kind = kind
        self.api_key = api_key
        self.queue: list[dict[str, Any]] = []
        self.health: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.fail_with: int | None = None  # force an HTTP status on every request
        self.html_instead = False  # answer like a reverse proxy login page
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self._server: TestServer | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/api/v3/system/status", self._status)
        app.router.add_get("/api/v3/queue", self._queue)
        app.router.add_get("/api/v3/health", self._health)
        app.router.add_get("/api/v3/history/since", self._history)
        self._server = TestServer(app)
        await self._server.start_server()
        return str(self._server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.close()

    # ---------------------------------------------------------------- helpers

    def _guard(self, request: web.Request) -> web.Response | None:
        self.requests.append((request.path, dict(request.query), dict(request.headers)))
        if self.fail_with:
            return web.Response(status=self.fail_with, text="nope")
        if request.headers.get("X-Api-Key") != self.api_key:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if self.html_instead:
            return web.Response(text="<html><body>Login</body></html>", content_type="text/html")
        return None

    async def _status(self, request: web.Request) -> web.Response:
        if (r := self._guard(request)) is not None:
            return r
        name = "Sonarr" if self.kind == "sonarr" else "Radarr"
        return web.json_response({"appName": name, "version": "4.0.15.2941" if self.kind == "sonarr" else "5.26.2"})

    async def _queue(self, request: web.Request) -> web.Response:
        if (r := self._guard(request)) is not None:
            return r
        page = int(request.query.get("page", "1"))
        size = int(request.query.get("pageSize", "10"))
        include = "includeSeries" if self.kind == "sonarr" else "includeMovie"
        records = copy.deepcopy(self.queue[(page - 1) * size: page * size])
        if request.query.get(include) != "true":  # the real API omits the nested objects unless asked
            for rec in records:
                rec.pop("series", None)
                rec.pop("movie", None)
                rec.pop("episode", None)
        return web.json_response({"page": page, "pageSize": size, "sortKey": "timeleft", "sortDirection": "ascending",
                                  "totalRecords": len(self.queue), "records": records})

    async def _health(self, request: web.Request) -> web.Response:
        if (r := self._guard(request)) is not None:
            return r
        return web.json_response(self.health)

    async def _history(self, request: web.Request) -> web.Response:
        if (r := self._guard(request)) is not None:
            return r
        since = request.query["date"]
        wanted = request.query.get("eventType")
        out = [h for h in self.history if h["date"] > since and (wanted is None or h["eventType"] == wanted)]
        return web.json_response(out)


# ----------------------------------------------------------------- record makers

def sonarr_row(row_id: int, *, download_id: str, release: str, series: str, season: int, episode: int,
               state: str = "downloading", status: str = "ok", queue_status: str = "downloading",
               messages: list[dict] | None = None, size: float = 1e9, sizeleft: float = 5e8,
               absolute: int | None = None) -> dict[str, Any]:
    return {
        "id": row_id, "seriesId": 7, "episodeId": 1000 + row_id, "seasonNumber": season,
        "series": {"title": series, "id": 7},
        "episode": {"seasonNumber": season, "episodeNumber": episode, "absoluteEpisodeNumber": absolute,
                    "title": f"Episode {episode}"},
        "title": release, "size": size, "sizeleft": sizeleft, "timeleft": "00:21:42",
        "estimatedCompletionTime": "2026-10-31T09:30:00Z",
        "status": queue_status, "trackedDownloadStatus": status, "trackedDownloadState": state,
        "statusMessages": messages or [], "downloadId": download_id, "protocol": "torrent",
        "downloadClient": "qBittorrent", "indexer": "Nyaa.si (Prowlarr)", "outputPath": f"/media/downloads/{release}",
    }


def radarr_row(row_id: int, *, download_id: str, release: str, title: str, year: int,
               state: str = "downloading", status: str = "ok", messages: list[dict] | None = None,
               error: str | None = None) -> dict[str, Any]:
    rec = {
        "id": row_id, "movieId": 3, "movie": {"title": title, "year": year, "id": 3},
        "title": release, "size": 8e9, "sizeleft": 0, "status": "completed",
        "trackedDownloadStatus": status, "trackedDownloadState": state, "statusMessages": messages or [],
        "downloadId": download_id, "protocol": "torrent", "downloadClient": "qBittorrent",
    }
    if error:
        rec["errorMessage"] = error
    return rec


def black_clover_pack(first: int = 124, last: int = 126) -> list[dict[str, Any]]:
    """A finished anime batch Sonarr couldn't map — one queue row per episode, each carrying the
    same per-file messages (exactly what Sonarr shows for "Unable to Import Automatically")."""
    release = f"[AnimeDevil] Black Clover {first}-{last} [1080p x265 HEVC 10bit WEB Dual Audio]"
    files = [{"title": f"Black Clover - Ep{n}.mkv", "messages": ["Invalid season or episode"]}
             for n in range(first, last + 1)]
    msgs = [{"title": release, "messages": ["One or more episodes expected in this release were not imported "
                                            "or missing from the release"]}, *files]
    return [sonarr_row(n, download_id="BC-HASH", release=release, series="Black Clover", season=1, episode=n,
                       absolute=n, state="importBlocked", status="warning", queue_status="completed",
                       messages=msgs, sizeleft=0) for n in range(first, last + 1)]
