"""The Sonarr/Radarr HTTP client and its parsing, against tests/mock_arr.py."""

from __future__ import annotations

import socket
from datetime import UTC, datetime

import pytest

from nasbot import arr
from nasbot.arr import (
    ArrAuthError,
    ArrCallError,
    ArrClient,
    ArrInstance,
    ArrUnavailable,
    HealthIssue,
    ImportEvent,
    QueueItem,
    group_releases,
    normalize_arr_url,
)

from .mock_arr import API_KEY, MockArr, black_clover_pack, radarr_row, sonarr_row


@pytest.fixture
async def sonarr():
    srv = MockArr("sonarr")
    url = await srv.start()
    srv.url = url
    yield srv
    await srv.stop()


@pytest.fixture
async def radarr():
    srv = MockArr("radarr")
    url = await srv.start()
    srv.url = url
    yield srv
    await srv.stop()


def client_for(srv: MockArr, key: str = API_KEY, **kw) -> ArrClient:
    return ArrClient(ArrInstance(srv.kind, srv.url, key), **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------------- URLs

@pytest.mark.parametrize(("raw", "expected"), [
    ("192.168.1.158:8989", "http://192.168.1.158:8989"),
    ("http://192.168.1.158:8989/", "http://192.168.1.158:8989"),
    ("https://nas.example.ts.net/sonarr/", "https://nas.example.ts.net/sonarr"),
    ("http://sonarr:8989/api/v3", "http://sonarr:8989"),
    ("HTTP://Sonarr:8989/api", "http://Sonarr:8989"),
])
def test_normalize_arr_url(raw, expected):
    assert normalize_arr_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "ftp://x", "http://"])
def test_normalize_arr_url_rejects(raw):
    with pytest.raises(ValueError):
        normalize_arr_url(raw)


def test_instance_hides_key_and_picks_link():
    inst = ArrInstance("sonarr", "http://10.0.0.5:8989", "secret", link_url="http://nas.ts.net:8989")
    assert "secret" not in repr(inst)
    assert inst.name == "Sonarr" and inst.web_url == "http://nas.ts.net:8989"
    assert ArrInstance("radarr", "http://x", "k").web_url == "http://x"


# ----------------------------------------------------------------------- parsing

def test_queue_item_parse_sonarr_and_progress():
    item = QueueItem.parse("sonarr", sonarr_row(1, download_id="H", release="Glee S05", series="Glee", season=5,
                                                episode=3, size=1000, sizeleft=250))
    assert (item.subject, item.season, item.episode) == ("Glee", 5, 3)
    assert item.progress == 0.75 and item.key == "H"
    assert item.tracked_state == "downloading" and item.client == "qBittorrent"
    assert item.eta == datetime(2026, 10, 31, 9, 30, tzinfo=UTC)


def test_queue_item_parse_radarr_and_fallbacks():
    item = QueueItem.parse("radarr", radarr_row(4, download_id="", release="Dune.2021", title="Dune", year=2021,
                                                error="Couldn't import"))
    assert item.subject == "Dune (2021)" and item.key == "item-4" and item.error == "Couldn't import"
    bare = QueueItem.parse("sonarr", {"id": 9, "title": "Unknown.Show.S01E01", "size": 0})
    assert bare.subject == "Unknown.Show.S01E01" and bare.progress is None and bare.season is None


def test_release_groups_pack_and_counts_files_not_rows():
    rels = group_releases([QueueItem.parse("sonarr", r) for r in black_clover_pack(124, 170)])
    assert len(rels) == 1
    rel = rels[0]
    assert rel.subject == "Black Clover" and len(rel.items) == 47
    assert rel.episodes[0] == (1, 124) and rel.episodes[-1] == (1, 170)
    msgs = dict(rel.messages)
    assert msgs["Invalid season or episode"] == 47  # 47 files, not 47 × 47 rows
    assert msgs["One or more episodes expected in this release were not imported or missing from the release"] == 1


def test_health_and_import_parse():
    h = HealthIssue.parse("sonarr", {"source": "IndexerStatusCheck", "type": "warning",
                                     "message": "Indexers unavailable due to failures: EZTV",
                                     "wikiUrl": "https://wiki.servarr.com/sonarr/system#indexers"})
    assert h.level == "WARNING" and h.key.startswith("IndexerStatusCheck|")
    assert HealthIssue.parse("radarr", {"source": "X", "type": "error"}).level == "ERROR"
    assert HealthIssue.parse("radarr", {"source": "X", "type": "notice"}).level == "NOTICE"
    ev = ImportEvent.parse("radarr", {"date": "2026-10-31T09:00:00Z", "downloadId": "D",
                                      "movie": {"title": "Dune", "year": 2021},
                                      "quality": {"quality": {"name": "Bluray-1080p"}}})
    assert (ev.subject, ev.quality, ev.group_key) == ("Dune (2021)", "Bluray-1080p", "D")


# ------------------------------------------------------------------------ client

async def test_status_queue_health_over_http(sonarr):
    sonarr.queue = black_clover_pack() + [sonarr_row(50, download_id="G5", release="Glee S05", series="Glee",
                                                     season=5, episode=1)]
    sonarr.health = [{"source": "DownloadClientCheck", "type": "error", "message": "Unable to communicate with "
                                                                                  "qBittorrent"}]
    c = client_for(sonarr)
    try:
        assert (await c.status())["appName"] == "Sonarr"
        queue = await c.queue()
        assert len(queue) == 4 and queue[0].subject == "Black Clover"  # nested series requested and parsed
        assert [h.source for h in await c.health()] == ["DownloadClientCheck"]
    finally:
        await c.close()
    path, query, headers = sonarr.requests[1]
    assert path == "/api/v3/queue" and headers["X-Api-Key"] == API_KEY
    assert query["includeSeries"] == "true" and query["includeEpisode"] == "true"
    assert query["includeUnknownSeriesItems"] == "false"  # hidden, like the Sonarr UI default


async def test_queue_follows_pages(sonarr, monkeypatch):
    monkeypatch.setattr(arr, "PAGE_SIZE", 2)
    sonarr.queue = black_clover_pack(1, 5)
    c = client_for(sonarr)
    try:
        assert [i.episode for i in await c.queue()] == [1, 2, 3, 4, 5]
    finally:
        await c.close()
    assert [q["page"] for p, q, _ in sonarr.requests if p == "/api/v3/queue"] == ["1", "2", "3"]


async def test_radarr_uses_movie_flags(radarr):
    radarr.queue = [radarr_row(1, download_id="D", release="Dune.2021.1080p", title="Dune", year=2021)]
    c = client_for(radarr)
    try:
        assert (await c.queue())[0].subject == "Dune (2021)"
    finally:
        await c.close()
    q = radarr.requests[-1][1]
    assert q["includeMovie"] == "true" and q["includeUnknownMovieItems"] == "false" and "includeSeries" not in q


async def test_imports_since_filters_and_sends_params(sonarr):
    sonarr.history = [
        {"date": "2026-10-31T08:00:00Z", "eventType": "downloadFolderImported", "downloadId": "OLD",
         "series": {"title": "Old"}, "episode": {"seasonNumber": 1, "episodeNumber": 1}},
        {"date": "2026-10-31T09:00:00Z", "eventType": "downloadFolderImported", "downloadId": "G5",
         "series": {"title": "Glee"}, "episode": {"seasonNumber": 5, "episodeNumber": 1},
         "quality": {"quality": {"name": "WEBDL-1080p"}}},
        {"date": "2026-10-31T09:01:00Z", "eventType": "grabbed", "downloadId": "X", "series": {"title": "X"}},
    ]
    c = client_for(sonarr)
    try:
        events = await c.imports_since(datetime(2026, 10, 31, 8, 30, tzinfo=UTC))
    finally:
        await c.close()
    assert [(e.subject, e.season, e.episode, e.quality) for e in events] == [("Glee", 5, 1, "WEBDL-1080p")]
    q = sonarr.requests[-1][1]
    assert q["date"] == "2026-10-31T08:30:00Z" and q["eventType"] == "downloadFolderImported"


async def test_bad_key_is_auth_error(sonarr):
    c = client_for(sonarr, key="wrong")
    try:
        with pytest.raises(ArrAuthError, match="rejected the API key"):
            await c.queue()
    finally:
        await c.close()


async def test_server_error_is_unavailable_and_404_is_call_error(sonarr):
    c = client_for(sonarr)
    try:
        sonarr.fail_with = 503
        with pytest.raises(ArrUnavailable, match="503"):
            await c.health()
        sonarr.fail_with = 404
        with pytest.raises(ArrCallError, match="404"):
            await c.health()
    finally:
        await c.close()


async def test_html_answer_explains_wrong_url(sonarr):
    sonarr.html_instead = True
    c = client_for(sonarr)
    try:
        with pytest.raises(ArrCallError, match="didn't return JSON"):
            await c.health()
    finally:
        await c.close()


async def test_connection_refused_is_unavailable():
    with socket.socket() as s:  # grab a free port, then close it so nothing listens there
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = ArrClient(ArrInstance("sonarr", f"http://127.0.0.1:{port}", "k"), timeout=3)
    try:
        with pytest.raises(ArrUnavailable, match="unreachable"):
            await c.status()
    finally:
        await c.close()


async def test_unexpected_shapes_are_call_errors(sonarr, monkeypatch):
    c = client_for(sonarr)
    try:
        async def fake_get(path, params=None):
            return {"not": "a list"}
        monkeypatch.setattr(c, "_get", fake_get)
        with pytest.raises(ArrCallError):
            await c.health()
        with pytest.raises(ArrCallError):
            await c.imports_since(datetime.now(UTC))

        async def fake_list(path, params=None):
            return []
        monkeypatch.setattr(c, "_get", fake_list)
        with pytest.raises(ArrCallError):
            await c.queue()
        with pytest.raises(ArrCallError):
            await c.status()
    finally:
        await c.close()
