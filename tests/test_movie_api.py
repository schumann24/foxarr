from __future__ import annotations

from fastapi.testclient import TestClient

from foxarr.app import FoxarrSettings, create_app
from foxarr.storage import MovieStore


def make_client(api_key: str = "") -> TestClient:
    settings = FoxarrSettings(
        database=":memory:",
        api_key=api_key,
        root_folder="/movies",
        quality_profile_id=1,
        quality_profile_name="Any",
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
