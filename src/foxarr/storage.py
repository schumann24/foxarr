"""SQLite persistence for Foxarr's movie-only MVP."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MovieNotFoundError(KeyError):
    """Raised when a requested movie id does not exist."""


class MovieStore:
    """Small SQLite repository with idempotent TMDB-based movie creation."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._memory_connection: sqlite3.Connection | None = None
        if self.database == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        connection = sqlite3.connect(self.database, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS movies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tmdb_id INTEGER NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    sort_title TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    overview TEXT NOT NULL,
                    status TEXT NOT NULL,
                    monitored INTEGER NOT NULL DEFAULT 1,
                    quality_profile_id INTEGER NOT NULL DEFAULT 1,
                    profile_id INTEGER NOT NULL DEFAULT 1,
                    title_slug TEXT NOT NULL,
                    minimum_availability TEXT NOT NULL DEFAULT 'released',
                    root_folder_path TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    search_requested INTEGER NOT NULL DEFAULT 0,
                    has_file INTEGER NOT NULL DEFAULT 0,
                    movie_file_count INTEGER NOT NULL DEFAULT 0,
                    size_on_disk INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    indexer_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0,
                    results_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(search_jobs)").fetchall()
            }
            if "selected_index" not in columns:
                connection.execute("ALTER TABLE search_jobs ADD COLUMN selected_index INTEGER")
            if "selected_result_json" not in columns:
                connection.execute(
                    "ALTER TABLE search_jobs ADD COLUMN selected_result_json TEXT"
                )
            if "selection_criteria_json" not in columns:
                connection.execute(
                    "ALTER TABLE search_jobs ADD COLUMN selection_criteria_json TEXT"
                )
            if "download_plan_json" not in columns:
                connection.execute("ALTER TABLE search_jobs ADD COLUMN download_plan_json TEXT")
            for name, definition in (
                ("transmission_id", "INTEGER"),
                ("transmission_status", "TEXT"),
                ("transmission_percent_done", "REAL"),
                ("transmission_download_dir", "TEXT"),
                ("transmission_error", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE search_jobs ADD COLUMN {name} {definition}")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def lookup_item(tmdb_id: int) -> dict[str, Any]:
        return {
            "id": 0,
            "tmdbId": tmdb_id,
            "imdbId": f"tt{tmdb_id:07d}",
            "title": f"Mock Movie {tmdb_id}",
            "originalTitle": f"Mock Movie {tmdb_id}",
            "sortTitle": f"mock movie {tmdb_id}",
            "year": 2026,
            "overview": "Movie metadata placeholder for the Foxarr MVP.",
            "status": "released",
            "monitored": True,
            "qualityProfileId": 1,
            "rootFolderPath": "/movies",
            "minimumAvailability": "released",
            "hasFile": False,
            "isAvailable": True,
            "images": [],
            "genres": ["Foxarr"],
            "tags": [],
            "statistics": {"movieFileCount": 0, "sizeOnDisk": 0},
        }

    def lookup(self, term: str) -> list[dict[str, Any]]:
        source, separator, raw_id = term.partition(":")
        if separator and source.lower() == "tmdb":
            try:
                tmdb_id = int(raw_id)
            except ValueError:
                return []
            return [self.lookup_item(tmdb_id)] if tmdb_id > 0 else []

        normalized = term.strip().lower()
        if not normalized:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM movies WHERE lower(title) LIKE ? ORDER BY id",
                (f"%{normalized}%",),
            ).fetchall()
        return [self._row_to_movie(row) for row in rows]

    def create_or_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            tmdb_id = int(payload["tmdbId"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("tmdbId must be a positive integer") from error
        if tmdb_id <= 0:
            raise ValueError("tmdbId must be a positive integer")

        metadata = self.lookup_item(tmdb_id)
        now = self._now()
        tags = payload.get("tags", [])
        if not isinstance(tags, list):
            raise TypeError("tags must be an array")
        title = str(payload.get("title") or metadata["title"])
        values = {
            "tmdb_id": tmdb_id,
            "title": title,
            "original_title": metadata["originalTitle"],
            "sort_title": title.lower(),
            "year": int(payload.get("year") or metadata["year"]),
            "overview": metadata["overview"],
            "status": metadata["status"],
            "monitored": int(bool(payload.get("monitored", True))),
            "quality_profile_id": int(payload.get("qualityProfileId", 1)),
            "profile_id": int(payload.get("profileId", payload.get("qualityProfileId", 1))),
            "title_slug": str(payload.get("titleSlug") or tmdb_id),
            "minimum_availability": str(payload.get("minimumAvailability", "released")),
            "root_folder_path": str(payload.get("rootFolderPath", "/movies")),
            "tags_json": json.dumps(tags, ensure_ascii=False),
            "search_requested": int(bool(payload.get("addOptions", {}).get("searchForMovie", False))),
            "updated_at": now,
        }

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM movies WHERE tmdb_id = ?", (tmdb_id,)
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE movies SET title=?, sort_title=?, year=?, monitored=?,
                        quality_profile_id=?, profile_id=?, title_slug=?,
                        minimum_availability=?, root_folder_path=?, tags_json=?,
                        search_requested=?, updated_at=? WHERE tmdb_id=?
                    """,
                    (
                        values["title"],
                        values["sort_title"],
                        values["year"],
                        values["monitored"],
                        values["quality_profile_id"],
                        values["profile_id"],
                        values["title_slug"],
                        values["minimum_availability"],
                        values["root_folder_path"],
                        values["tags_json"],
                        values["search_requested"],
                        values["updated_at"],
                        tmdb_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO movies (
                        tmdb_id, title, original_title, sort_title, year, overview,
                        status, monitored, quality_profile_id, profile_id, title_slug,
                        minimum_availability, root_folder_path, tags_json,
                        search_requested, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["tmdb_id"],
                        values["title"],
                        values["original_title"],
                        values["sort_title"],
                        values["year"],
                        values["overview"],
                        values["status"],
                        values["monitored"],
                        values["quality_profile_id"],
                        values["profile_id"],
                        values["title_slug"],
                        values["minimum_availability"],
                        values["root_folder_path"],
                        values["tags_json"],
                        values["search_requested"],
                        now,
                        now,
                    ),
                )
            row = connection.execute("SELECT * FROM movies WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
        assert row is not None
        return self._row_to_movie(row)

    def list_movies(self, tmdb_id: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if tmdb_id is None:
                rows = connection.execute("SELECT * FROM movies ORDER BY id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM movies WHERE tmdb_id = ? ORDER BY id", (tmdb_id,)
                ).fetchall()
        return [self._row_to_movie(row) for row in rows]

    def get_movie(self, movie_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
        if row is None:
            raise MovieNotFoundError(movie_id)
        return self._row_to_movie(row)

    def create_search_job(self, query: str, indexer_ids: list[int], limit: int) -> int:
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO search_jobs (
                    query, indexer_ids_json, status, result_count, results_json,
                    error, dry_run, created_at, updated_at
                ) VALUES (?, ?, 'running', 0, '[]', NULL, 1, ?, ?)
                """,
                (query, json.dumps(indexer_ids), now, now),
            )
            return int(cursor.lastrowid)

    def finish_search_job(
        self,
        job_id: int,
        results: list[dict[str, Any]],
        error: str | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        status = "failed" if error else "completed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE search_jobs
                SET status=?, result_count=?, results_json=?, error=?, updated_at=?
                WHERE id=?
                """,
                (status, len(results), json.dumps(results, ensure_ascii=False), error, now, job_id),
            )
            row = connection.execute("SELECT * FROM search_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_search_job(row)

    def get_search_job(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM search_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_search_job(row)

    def plan_search_job(self, job_id: int, plan: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"selected", "download_planned"}:
                raise ValueError("only selected search jobs can be planned")
            connection.execute(
                """
                UPDATE search_jobs SET status='download_planned', download_plan_json=?,
                    updated_at=? WHERE id=?
                """,
                (json.dumps(plan, ensure_ascii=False), now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_search_job(updated)

    def update_transmission_status(
        self,
        job_id: int,
        torrent_id: int,
        status: str,
        percent_done: float,
        error: Any = None,
        download_dir: Any = None,
    ) -> dict[str, Any]:
        now = self._now()
        if percent_done < 0 or percent_done > 1:
            raise ValueError("percentDone must be between 0 and 1")
        lifecycle = {
            "paused": "paused",
            "stopped": "paused",
            "downloading": "downloading",
            "queued": "downloading",
            "seeding": "transmission_completed",
            "completed": "transmission_completed",
            "error": "error",
        }.get(status.lower(), status)
        if percent_done >= 1 and lifecycle not in {"error", "awaiting_external_import"}:
            lifecycle = "transmission_completed"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            connection.execute(
                """
                UPDATE search_jobs SET status=?, transmission_id=?, transmission_status=?,
                    transmission_percent_done=?, transmission_download_dir=?,
                    transmission_error=?, updated_at=? WHERE id=?
                """,
                (
                    lifecycle,
                    torrent_id,
                    status,
                    percent_done,
                    download_dir,
                    str(error) if error else None,
                    now,
                    job_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_search_job(updated)

    def select_search_job(
        self,
        job_id: int,
        selected_index: int,
        selected_result: dict[str, Any],
        criteria: dict[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "completed":
                raise ValueError("only completed search jobs can be selected")
            connection.execute(
                """
                UPDATE search_jobs
                SET status='selected', selected_index=?, selected_result_json=?,
                    selection_criteria_json=?, updated_at=? WHERE id=?
                """,
                (
                    selected_index,
                    json.dumps(selected_result, ensure_ascii=False),
                    json.dumps(criteria, ensure_ascii=False),
                    now,
                    job_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_search_job(updated)

    @staticmethod
    def _row_to_search_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "query": row["query"],
            "indexerIds": json.loads(row["indexer_ids_json"]),
            "status": row["status"],
            "resultCount": row["result_count"],
            "results": json.loads(row["results_json"]),
            "error": row["error"],
            "dryRun": bool(row["dry_run"]),
            "selectedIndex": row["selected_index"],
            "selectedResult": (
                json.loads(row["selected_result_json"])
                if row["selected_result_json"]
                else None
            ),
            "selectionCriteria": (
                json.loads(row["selection_criteria_json"])
                if row["selection_criteria_json"]
                else None
            ),
            "downloadPlan": (
                json.loads(row["download_plan_json"])
                if row["download_plan_json"]
                else None
            ),
            "transmission": {
                "torrentId": row["transmission_id"],
                "status": row["transmission_status"],
                "percentDone": row["transmission_percent_done"],
                "downloadDir": row["transmission_download_dir"],
                "error": row["transmission_error"],
            },
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _row_to_movie(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tmdbId": row["tmdb_id"],
            "imdbId": f"tt{row['tmdb_id']:07d}",
            "title": row["title"],
            "originalTitle": row["original_title"],
            "sortTitle": row["sort_title"],
            "year": row["year"],
            "overview": row["overview"],
            "status": row["status"],
            "monitored": bool(row["monitored"]),
            "qualityProfileId": row["quality_profile_id"],
            "rootFolderPath": row["root_folder_path"],
            "minimumAvailability": row["minimum_availability"],
            "hasFile": bool(row["has_file"]),
            "isAvailable": True,
            "images": [],
            "genres": ["Foxarr"],
            "tags": json.loads(row["tags_json"]),
            "added": row["created_at"],
            "statistics": {
                "movieFileCount": row["movie_file_count"],
                "sizeOnDisk": row["size_on_disk"],
            },
        }
