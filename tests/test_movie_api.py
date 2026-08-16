from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from foxarr.app import FoxarrSettings, create_app
from foxarr.storage import MovieStore
from foxarr.transmission import TransmissionClient, TransmissionError


def make_client(api_key: str = "") -> TestClient:
    settings = FoxarrSettings(
        database=":memory:",
        api_key=api_key,
        root_folder="/movies",
        quality_profile_id=1,
        quality_profile_name="Any",
    )
    return TestClient(create_app(store=MovieStore(":memory:"), settings=settings))


def make_search_client(monkeypatch, response: list[dict]) -> TestClient:
    def fake_get(*args, **kwargs):
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list[dict]:
                return response

        return FakeResponse()

    monkeypatch.setattr("foxarr.prowlarr.httpx.get", fake_get)
    settings = FoxarrSettings(
        database=":memory:",
        api_key="local-test-key",
        prowlarr_url="http://prowlarr.test",
        prowlarr_api_key="prowlarr-test-key",
        dry_run=True,
    )
    return TestClient(create_app(store=MovieStore(":memory:"), settings=settings))


def movie_payload(tmdb_id: int = 550) -> dict:
    return {
        "title": "Fight Club",
        "qualityProfileId": 1,
        "profileId": 1,
        "titleSlug": str(tmdb_id),
        "minimumAvailability": "released",
        "tmdbId": tmdb_id,
        "year": 1999,
        "rootFolderPath": "/movies",
        "monitored": True,
        "tags": [],
        "addOptions": {"searchForMovie": False},
    }


def test_connection_check_and_lookup() -> None:
    client = make_client()

    assert client.get("/api/v3/system/status").json()["appName"] == "Radarr"
    assert client.get("/api/v3/qualityProfile").json()[0]["id"] == 1
    assert client.get("/api/v3/rootfolder").json()[0]["path"] == "/movies"
    assert client.get("/api/v3/tag").json() == []
    assert client.get("/api/v3/movie/lookup?term=tmdb:550").json()[0]["tmdbId"] == 550


def test_movie_create_is_idempotent_and_status_is_seerr_compatible() -> None:
    client = make_client()

    first = client.post("/api/v3/movie", json=movie_payload())
    second = client.post("/api/v3/movie", json=movie_payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert client.get("/api/v3/movie").json() == [first.json()]

    movie_id = first.json()["id"]
    status = client.get(f"/api/v3/movie/{movie_id}")
    assert status.status_code == 200
    assert status.json()["tmdbId"] == 550
    assert status.json()["hasFile"] is False
    assert status.json()["statistics"] == {"movieFileCount": 0, "sizeOnDisk": 0}


def test_api_key_can_be_supplied_as_query_or_header() -> None:
    client = make_client(api_key="local-test-key")

    assert client.get("/api/v3/system/status").status_code == 401
    assert client.get("/api/v3/system/status?apikey=local-test-key").status_code == 200
    assert client.get(
        "/api/v3/system/status", headers={"X-Api-Key": "local-test-key"}
    ).status_code == 200


def test_missing_movie_returns_404() -> None:
    response = make_client().get("/api/v3/movie/999")
    assert response.status_code == 404


def test_dry_run_search_saves_safe_results_and_job(monkeypatch) -> None:
    client = make_search_client(
        monkeypatch,
        [
            {
                "title": "The Matrix 1999",
                "indexer": "RuTracker.org",
                "indexerId": 1,
                "protocol": "torrent",
                "size": 123,
                "seeders": 7,
                "guid": "https://rutracker.org/forum/viewtopic.php?t=123",
                "downloadUrl": "magnet:?xt=urn:btih:secret",
                "magnetUrl": "magnet:?xt=urn:btih:secret",
            }
        ],
    )

    response = client.post(
        "/api/internal/dry-run/search",
        headers={"X-Api-Key": "local-test-key"},
        json={"query": "Матрица", "indexerIds": [1], "limit": 5},
    )

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "completed"
    assert job["dryRun"] is True
    assert job["resultCount"] == 1
    assert job["results"][0]["title"] == "The Matrix 1999"
    assert "downloadUrl" not in job["results"][0]
    assert "magnetUrl" not in job["results"][0]
    assert client.get(
        f"/api/internal/dry-run/search/{job['id']}",
        headers={"X-Api-Key": "local-test-key"},
    ).json()["id"] == job["id"]


def test_dry_run_requires_configured_prowlarr() -> None:
    client = make_client(api_key="local-test-key")
    response = client.post(
        "/api/internal/dry-run/search",
        headers={"X-Api-Key": "local-test-key"},
        json={"query": "Матрица"},
    )
    assert response.status_code == 503


def test_dry_run_release_selection_is_persisted(monkeypatch) -> None:
    client = make_search_client(
        monkeypatch,
        [
            {
                "title": "Matrix 1080p small",
                "indexer": "test",
                "indexerId": 1,
                "protocol": "torrent",
                "size": 10,
                "seeders": 4,
                "guid": "https://example.test/1",
                "quality": "1080p",
            },
            {
                "title": "Matrix 2160p",
                "indexer": "test",
                "indexerId": 1,
                "protocol": "torrent",
                "size": 20,
                "seeders": 12,
                "guid": "https://example.test/2",
                "quality": "2160p",
            },
        ],
    )
    job = client.post(
        "/api/internal/dry-run/search",
        headers={"X-Api-Key": "local-test-key"},
        json={"query": "Матрица", "limit": 2},
    ).json()

    selected = client.post(
        f"/api/internal/dry-run/search/{job['id']}/select",
        headers={"X-Api-Key": "local-test-key"},
        json={"minSeeders": 5, "preferredQuality": ["2160p"]},
    )

    assert selected.status_code == 200
    data = selected.json()
    assert data["status"] == "selected"
    assert data["selectedIndex"] == 1
    assert data["selectedResult"]["title"] == "Matrix 2160p"
    assert data["selectedResult"]["selectionScore"] > 0
    assert data["selectionCriteria"]["minSeeders"] == 5
    assert client.get(
        f"/api/internal/dry-run/search/{job['id']}",
        headers={"X-Api-Key": "local-test-key"},
    ).json()["status"] == "selected"


def test_dry_run_release_selection_rejects_no_match(monkeypatch) -> None:
    client = make_search_client(monkeypatch, [{"title": "Matrix", "protocol": "torrent"}])
    job = client.post(
        "/api/internal/dry-run/search",
        headers={"X-Api-Key": "local-test-key"},
        json={"query": "Матрица"},
    ).json()
    response = client.post(
        f"/api/internal/dry-run/search/{job['id']}/select",
        headers={"X-Api-Key": "local-test-key"},
        json={"minSeeders": 1},
    )
    assert response.status_code == 409


def test_download_plan_is_preview_only(monkeypatch) -> None:
    client = make_search_client(
        monkeypatch,
        [{
            "title": "Matrix 1080p",
            "indexer": "test",
            "indexerId": 1,
            "protocol": "torrent",
            "size": 10,
            "seeders": 5,
            "guid": "https://example.test/1",
        }],
    )
    job = client.post(
        "/api/internal/dry-run/search",
        headers={"X-Api-Key": "local-test-key"},
        json={"query": "Матрица"},
    ).json()
    client.post(
        f"/api/internal/dry-run/search/{job['id']}/select",
        headers={"X-Api-Key": "local-test-key"},
        json={},
    )

    response = client.post(
        f"/api/internal/dry-run/search/{job['id']}/plan",
        headers={"X-Api-Key": "local-test-key"},
        json={"downloadDir": "/downloads/test"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "download_planned"
    assert data["downloadPlan"]["execution"] == "not_submitted"
    assert data["downloadPlan"]["rpcPreview"]["method"] == "torrent-add"
    assert data["downloadPlan"]["rpcPreview"]["arguments"]["filename"].startswith("<")
    assert client.get(
        f"/api/internal/dry-run/search/{job['id']}",
        headers={"X-Api-Key": "local-test-key"},
    ).json()["status"] == "download_planned"


def test_transmission_rpc_session_handshake_and_paused_add() -> None:
    calls: list[tuple[bytes, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.read(), dict(request.headers)))
        if len(calls) == 1:
            return httpx.Response(
                409,
                headers={"X-Transmission-Session-Id": "session-test"},
            )
        return httpx.Response(200, json={"result": "success", "arguments": {"id": 42}})

    client = TransmissionClient(
        "http://transmission.test/transmission/rpc",
        transport=httpx.MockTransport(handler),
    )

    response = client.add_torrent(
        "magnet:?xt=urn:btih:test",
        "/downloads/test",
        labels=["foxarr", "foxarr-job-2"],
        paused=True,
    )

    assert response["result"] == "success"
    assert len(calls) == 2
    first = json.loads(calls[0][0])
    second = json.loads(calls[1][0])
    assert first == second
    assert calls[1][1]["x-transmission-session-id"] == "session-test"
    assert first["method"] == "torrent-add"
    assert first["arguments"]["download-dir"] == "/downloads/test"
    assert first["arguments"]["paused"] is True
    assert first["arguments"]["labels"] == ["foxarr", "foxarr-job-2"]


def test_transmission_rpc_rejects_non_success_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "torrent duplicate"})

    client = TransmissionClient("http://transmission.test/rpc", transport=httpx.MockTransport(handler))

    try:
        client.add_torrent("https://example.test/file.torrent", "/downloads/test")
    except TransmissionError as error:
        assert "torrent duplicate" in str(error)
    else:
        raise AssertionError("TransmissionError was not raised")
