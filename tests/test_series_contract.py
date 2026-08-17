from __future__ import annotations

from fastapi.testclient import TestClient

from foxarr.app import FoxarrSettings, create_app
from foxarr.storage import MovieStore

TVDB_ID = 121361


def make_sonarr_client() -> TestClient:
    settings = FoxarrSettings(
        database=":memory:",
        role="sonarr",
        series_root_folder="/tv",
        quality_profile_id=1,
        quality_profile_name="Any",
    )
    return TestClient(create_app(store=MovieStore(":memory:"), settings=settings))


def seerr_series_payload(*, seasons: list[dict[str, object]]) -> dict[str, object]:
    return {
        "tvdbId": TVDB_ID,
        "title": "Игра Престолов",
        "qualityProfileId": 1,
        "seasons": seasons,
        "tags": [],
        "seasonFolder": True,
        "monitored": True,
        "rootFolderPath": "/tv",
        "seriesType": "standard",
        "addOptions": {
            "ignoreEpisodesWithFiles": True,
            "searchForMissingEpisodes": False,
        },
    }


def test_seerr_new_series_contract() -> None:
    client = make_sonarr_client()

    assert client.get("/api/v3/system/status").json() == {
        "appName": "Sonarr",
        "version": "4.0.8.0",
        "instanceName": "foxarr-sonarr",
    }
    assert client.get("/api/v3/qualityProfile").json()[0]["id"] == 1
    assert client.get("/api/v3/rootfolder").json()[0]["path"] == "/tv"
    assert client.get("/api/v3/tag").json() == []

    lookup = client.get(f"/api/v3/series/lookup?term=tvdb:{TVDB_ID}")
    assert lookup.status_code == 200
    assert lookup.json()[0]["id"] == 0

    created = client.post(
        "/api/v3/series",
        json=seerr_series_payload(
            seasons=[
                {"seasonNumber": 1, "monitored": True},
                {"seasonNumber": 2, "monitored": False},
            ]
        ),
    )
    assert created.status_code == 200
    series = created.json()
    assert series["id"] > 0
    assert series["tvdbId"] == TVDB_ID
    assert series["seasons"] == [
        {"seasonNumber": 1, "monitored": True},
        {"seasonNumber": 2, "monitored": False},
    ]

    episodes = client.get(f"/api/v3/episode?seriesId={series['id']}")
    assert episodes.status_code == 200
    assert len(episodes.json()) == 3
    assert episodes.json()[0]["hasFile"] is False


def test_seerr_existing_series_update_and_search_contract() -> None:
    client = make_sonarr_client()
    created = client.post(
        "/api/v3/series",
        json=seerr_series_payload(
            seasons=[
                {"seasonNumber": 1, "monitored": True},
                {"seasonNumber": 2, "monitored": False},
            ]
        ),
    ).json()

    lookup = client.get(f"/api/v3/series/lookup?term=tvdb:{TVDB_ID}")
    assert lookup.json()[0]["id"] == created["id"]

    updated_body = dict(created)
    updated_body["seasons"] = [
        {"seasonNumber": 1, "monitored": True},
        {"seasonNumber": 2, "monitored": True},
    ]
    updated = client.put("/api/v3/series", json=updated_body)
    assert updated.status_code == 200
    assert len(client.get(f"/api/v3/episode?seriesId={created['id']}").json()) == 6

    episode_ids = [
        episode["id"]
        for episode in client.get(f"/api/v3/episode?seriesId={created['id']}").json()
    ]
    monitored = client.put(
        "/api/v3/episode/monitor",
        json={"episodeIds": episode_ids, "monitored": True},
    )
    assert monitored.status_code == 200

    command = client.post(
        "/api/v3/command",
        json={"name": "MissingEpisodeSearch", "seriesId": created["id"]},
    )
    assert command.status_code == 200
    assert command.json()["id"] > 0
    assert command.json()["status"] == "completed"
    assert command.json()["result"] == "successful"

    status = client.get(f"/api/v3/series/{created['id']}")
    assert status.status_code == 200
    assert status.json()["searchRequested"] is True


def test_missing_episode_search_job_runs_explicitly_through_prowlarr(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        if url.endswith("/api/v1/indexer"):
            return FakeResponse([{"id": 1, "enable": True}])
        assert url.endswith("/api/v1/search")
        assert kwargs["params"]["query"] == "Игра Престолов"
        return FakeResponse([
            {
                "title": "Mock Series 121361 S01E01 1080p",
                "indexer": "test",
                "indexerId": 1,
                "protocol": "torrent",
                "size": 100,
                "seeders": 10,
                "guid": "https://example.test/series-episode-1",
                "downloadUrl": "magnet:?xt=urn:btih:must-not-persist",
            }
        ])

    monkeypatch.setattr("foxarr.prowlarr.httpx.get", fake_get)
    client = make_sonarr_client()
    client.app.state.settings.prowlarr_url = "http://prowlarr.test"
    client.app.state.settings.prowlarr_api_key = "test-key"
    series = client.post(
        "/api/v3/series",
        json=seerr_series_payload(seasons=[{"seasonNumber": 1, "monitored": True}]),
    ).json()

    command = client.post(
        "/api/v3/command",
        json={"name": "MissingEpisodeSearch", "seriesId": series["id"]},
    )
    assert command.status_code == 200
    command_body = command.json()
    assert command_body["searchJobId"] > 0
    assert command_body["body"]["searchJobId"] == command_body["searchJobId"]

    queued = client.get(f"/api/internal/dry-run/search/{command_body['searchJobId']}")
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    assert queued.json()["mediaType"] == "series"
    assert queued.json()["seriesId"] == series["id"]
    assert queued.json()["targetEpisodeIds"]

    result = client.post(f"/api/internal/dry-run/search/{command_body['searchJobId']}/run")
    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["resultCount"] == 1
    assert result.json()["results"][0]["guid"] == "https://example.test/series-episode-1"
    assert "must-not-persist" not in str(result.json())


def test_series_search_reuses_selection_and_transmission_plan(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [{
                "title": "Игра Престолов S01E01 1080p WEB-DL HEVC",
                "indexer": "test",
                "indexerId": 1,
                "protocol": "torrent",
                "size": 2_000_000_000,
                "seeders": 12,
                "guid": "https://example.test/series-plan",
            }]

    monkeypatch.setattr("foxarr.prowlarr.httpx.get", lambda *args, **kwargs: FakeResponse())
    client = make_sonarr_client()
    client.app.state.settings.prowlarr_url = "http://prowlarr.test"
    client.app.state.settings.prowlarr_api_key = "test-key"
    series = client.post(
        "/api/v3/series",
        json=seerr_series_payload(seasons=[{"seasonNumber": 1, "monitored": True}]),
    ).json()
    command = client.post(
        "/api/v3/command",
        json={"name": "MissingEpisodeSearch", "seriesId": series["id"]},
    ).json()
    job_id = command["searchJobId"]

    searched = client.post(f"/api/internal/dry-run/search/{job_id}/run")
    assert searched.status_code == 200
    assert searched.json()["mediaType"] == "series"

    selected = client.post(
        f"/api/internal/dry-run/search/{job_id}/select",
        json={"allowedResolutions": ["1080p"], "allowedVideoCodecs": ["hevc"]},
    )
    assert selected.status_code == 200
    assert selected.json()["status"] == "selected"

    plan = client.post(
        f"/api/internal/dry-run/search/{job_id}/plan",
        json={"mediaType": "series"},
    )
    assert plan.status_code == 200
    data = plan.json()
    assert data["status"] == "download_planned"
    assert data["mediaType"] == "series"
    assert data["downloadPlan"]["downloadDir"] == "/home/blackfox/data/serial"
    assert data["downloadPlan"]["rpcPreview"]["arguments"]["paused"] is True
    assert data["downloadPlan"]["rpcPreview"]["arguments"]["labels"] == [
        "foxarr",
        f"foxarr-job-{job_id}",
    ]

    wrong_type = client.post(
        f"/api/internal/dry-run/search/{job_id}/plan",
        json={"mediaType": "movie"},
    )
    assert wrong_type.status_code == 409


def test_series_submit_uses_common_worker_and_series_directory(monkeypatch) -> None:
    class FakeProwlarrResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [{
                "title": "Игра Престолов S01E01 1080p WEB-DL HEVC",
                "indexer": "test",
                "indexerId": 1,
                "protocol": "torrent",
                "size": 2_000_000_000,
                "seeders": 12,
                "guid": "https://example.test/series-submit",
            }]

    monkeypatch.setattr(
        "foxarr.prowlarr.httpx.get",
        lambda *args, **kwargs: FakeProwlarrResponse(),
    )
    monkeypatch.setattr(
        "foxarr.prowlarr.ProwlarrClient.resolve_download_url",
        lambda self, query, guid, indexer_ids=None, limit=100: (
            {"guid": guid},
            "magnet:?xt=urn:btih:series-secret",
        ),
    )

    torrent = {
        "id": 88,
        "name": "Игра Престолов S01E01.mkv",
        "status": 0,
        "percentDone": 0,
        "downloadDir": "/home/blackfox/data/serial",
        "totalSize": 2_000_000_000,
        "labels": ["foxarr", "foxarr-job-88"],
        "error": 0,
        "errorString": "",
        "rateDownload": 0,
    }
    rpc_calls: list[str] = []

    def fake_rpc(self, method, arguments=None):
        rpc_calls.append(method)
        if method == "torrent-get":
            return {"result": "success", "arguments": {"torrents": []}}
        if method == "torrent-add":
            assert arguments["download-dir"] == "/home/blackfox/data/serial"
            assert arguments["paused"] is True
            assert arguments["labels"] == ["foxarr", "foxarr-job-1"]
            assert arguments["filename"] == "magnet:?xt=urn:btih:series-secret"
            return {"result": "success", "arguments": {"torrent-added": {"id": 88}}}
        raise AssertionError(method)

    # The worker refreshes the torrent after torrent-add. Return the created
    # object only for that refresh; the first lookup must remain empty.
    lookup_count = {"value": 0}

    def fake_rpc_with_refresh(self, method, arguments=None):
        rpc_calls.append(method)
        if method != "torrent-get":
            if method == "torrent-add":
                assert arguments["download-dir"] == "/home/blackfox/data/serial"
                assert arguments["paused"] is True
                assert arguments["labels"] == ["foxarr", "foxarr-job-1"]
                assert arguments["filename"] == "magnet:?xt=urn:btih:series-secret"
                return {
                    "result": "success",
                    "arguments": {"torrent-added": {"id": 88}},
                }
            raise AssertionError(method)
        lookup_count["value"] += 1
        if lookup_count["value"] == 1:
            return {"result": "success", "arguments": {"torrents": []}}
        return {"result": "success", "arguments": {"torrents": [torrent]}}

    monkeypatch.setattr("foxarr.transmission.TransmissionClient._rpc", fake_rpc_with_refresh)

    client = make_sonarr_client()
    client.app.state.settings.prowlarr_url = "http://prowlarr.test"
    client.app.state.settings.prowlarr_api_key = "test-key"
    client.app.state.settings.transmission_url = "http://transmission.test/rpc"
    series = client.post(
        "/api/v3/series",
        json=seerr_series_payload(seasons=[{"seasonNumber": 1, "monitored": True}]),
    ).json()
    command = client.post(
        "/api/v3/command",
        json={"name": "MissingEpisodeSearch", "seriesId": series["id"]},
    ).json()
    job_id = command["searchJobId"]
    assert client.post(f"/api/internal/dry-run/search/{job_id}/run").status_code == 200
    assert client.post(f"/api/internal/dry-run/search/{job_id}/select", json={}).status_code == 200

    submitted = client.post(
        f"/api/internal/transmission/search/{job_id}/submit",
        json={"confirm": True, "mediaType": "series"},
    )
    assert submitted.status_code == 200
    data = submitted.json()
    assert data["mediaType"] == "series"
    assert data["paused"] is True
    assert data["torrent"]["torrentId"] == 88
    assert data["torrent"]["downloadDir"] == "/home/blackfox/data/serial"
    assert data["job"]["status"] == "paused"
    assert "series-secret" not in str(data)
    assert rpc_calls == ["torrent-get", "torrent-add", "torrent-get"]

    queue = client.get("/api/v3/queue?page=1&pageSize=10")
    assert queue.status_code == 200
    assert queue.json()["totalRecords"] == 1
    record = queue.json()["records"][0]
    assert record["seriesId"] == series["id"]
    assert record["episodeId"] > 0
    assert record["downloadId"] == "88"
    assert record["outputPath"] == "/home/blackfox/data/serial"
    assert record["status"] == "paused"

    episodes = client.get(f"/api/v3/episode?seriesId={series['id']}").json()
    imported = client.post(
        f"/api/internal/series/{series['id']}/import",
        json={
            "episodeFiles": [
                {
                    "episodeId": episode["id"],
                    "path": f"/home/blackfox/data/serial/{episode['title']}.mkv",
                    "size": 1_000_000_000,
                }
                for episode in episodes
            ]
        },
    )
    assert imported.status_code == 200
    imported_data = imported.json()
    assert imported_data["series"]["hasFile"] is True
    assert imported_data["series"]["statistics"] == {
        "episodeFileCount": 3,
        "sizeOnDisk": 3_000_000_000,
    }
    assert all(item["hasFile"] is True for item in imported_data["episodes"])
    assert imported_data["jobs"][0]["status"] == "imported"
    assert client.get(f"/api/v3/series/{series['id']}").json()["hasFile"] is True
    assert client.get("/api/v3/queue").json()["records"][0]["status"] == "completed"

    wrong_type = client.post(
        f"/api/internal/transmission/search/{job_id}/submit",
        json={"confirm": True, "mediaType": "movie"},
    )
    assert wrong_type.status_code == 409
