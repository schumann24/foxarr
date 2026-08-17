"""SQLite persistence for Foxarr's Radarr/Sonarr-compatible MVP."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MovieNotFoundError(KeyError):
    """Raised when a requested movie id does not exist."""


class SeriesNotFoundError(KeyError):
    """Raised when a requested series id does not exist."""


class EpisodeNotFoundError(KeyError):
    """Raised when a requested episode id does not exist."""


class MovieStore:
    """Small SQLite repository with idempotent movie and series creation."""

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
            connection = self._memory_connection
        else:
            connection = sqlite3.connect(self.database, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
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
                    movie_file_json TEXT,
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
                    updated_at TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'movie',
                    movie_id INTEGER,
                    series_id INTEGER,
                    season_number INTEGER,
                    target_episode_ids_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tvdb_id INTEGER NOT NULL UNIQUE,
                    tvmaze_id INTEGER,
                    title TEXT NOT NULL,
                    sort_title TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    overview TEXT NOT NULL,
                    status TEXT NOT NULL,
                    monitored INTEGER NOT NULL DEFAULT 1,
                    quality_profile_id INTEGER NOT NULL DEFAULT 1,
                    profile_id INTEGER NOT NULL DEFAULT 1,
                    root_folder_path TEXT NOT NULL,
                    series_type TEXT NOT NULL DEFAULT 'standard',
                    season_folder INTEGER NOT NULL DEFAULT 1,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    seasons_json TEXT NOT NULL DEFAULT '[]',
                    search_requested INTEGER NOT NULL DEFAULT 0,
                    ignore_episodes_with_files INTEGER NOT NULL DEFAULT 0,
                    episode_file_count INTEGER NOT NULL DEFAULT 0,
                    size_on_disk INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER NOT NULL,
                    season_number INTEGER NOT NULL,
                    episode_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    overview TEXT,
                    air_date TEXT,
                    monitored INTEGER NOT NULL DEFAULT 1,
                    has_file INTEGER NOT NULL DEFAULT 0,
                    episode_file_json TEXT,
                    UNIQUE(series_id, season_number, episode_number),
                    FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    search_job_id INTEGER,
                    status TEXT NOT NULL,
                    result TEXT,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT
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
                    columns.add(name)
            for name, definition in (
                ("media_type", "TEXT NOT NULL DEFAULT 'movie'"),
                ("movie_id", "INTEGER"),
                ("series_id", "INTEGER"),
                ("season_number", "INTEGER"),
                ("target_episode_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE search_jobs ADD COLUMN {name} {definition}")
                    columns.add(name)
            series_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(series)").fetchall()
            }
            for name, definition in (
                ("search_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("ignore_episodes_with_files", "INTEGER NOT NULL DEFAULT 0"),
                ("episode_file_count", "INTEGER NOT NULL DEFAULT 0"),
                ("size_on_disk", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in series_columns:
                    connection.execute(f"ALTER TABLE series ADD COLUMN {name} {definition}")
            command_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(commands)").fetchall()
            }
            if "search_job_id" not in command_columns:
                connection.execute("ALTER TABLE commands ADD COLUMN search_job_id INTEGER")
            movie_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(movies)").fetchall()
            }
            if "movie_file_json" not in movie_columns:
                connection.execute("ALTER TABLE movies ADD COLUMN movie_file_json TEXT")

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

    def import_movie_file(self, movie_id: int, file_info: dict[str, Any]) -> dict[str, Any]:
        """Attach one already-verified local file to a movie record.

        The caller is responsible for checking the mirror/import result. Foxarr
        does not inspect the filesystem, move files, or call Jellyfin here.
        """
        if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id < 1:
            raise ValueError("movieId must be a positive integer")
        if not isinstance(file_info, dict):
            raise TypeError("movieFile must be an object")
        path = file_info.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must be an absolute path")
        try:
            size = int(file_info["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("size must be an integer") from error
        if size < 0:
            raise ValueError("size must not be negative")
        now = self._now()
        record = {
            "path": path,
            "relativePath": file_info.get("relativePath") or path,
            "size": size,
            "dateAdded": str(file_info.get("dateAdded") or now),
        }
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM movies WHERE id = ?", (movie_id,)
            ).fetchone()
            if exists is None:
                raise MovieNotFoundError(movie_id)
            connection.execute(
                "UPDATE movies SET has_file=1, movie_file_count=1, size_on_disk=?, "
                "movie_file_json=?, updated_at=? WHERE id=?",
                (size, json.dumps(record, ensure_ascii=False), now, movie_id),
            )
            jobs = connection.execute(
                "SELECT id FROM search_jobs WHERE media_type='movie' AND movie_id=? "
                "AND transmission_id IS NOT NULL ORDER BY id",
                (movie_id,),
            ).fetchall()
            for job in jobs:
                connection.execute(
                    "UPDATE search_jobs SET status='imported', updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
            row = connection.execute(
                "SELECT * FROM movies WHERE id = ?", (movie_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_movie(row)

    @staticmethod
    def lookup_series_item(tvdb_id: int) -> dict[str, Any]:
        """Return deterministic metadata suitable for Seerr's Sonarr probe."""
        return {
            "id": 0,
            "tvdbId": tvdb_id,
            "tvMazeId": tvdb_id,
            "imdbId": f"tt{tvdb_id:07d}",
            "title": f"Mock Series {tvdb_id}",
            "sortTitle": f"mock series {tvdb_id}",
            "year": 2026,
            "overview": "Series metadata placeholder for the Foxarr MVP.",
            "status": "continuing",
            "monitored": True,
            "seriesType": "standard",
            "seasonFolder": True,
            "qualityProfileId": 1,
            "rootFolderPath": "/tv",
            "seasons": [
                {"seasonNumber": 1, "monitored": True},
                {"seasonNumber": 2, "monitored": True},
            ],
            "images": [],
            "genres": ["Foxarr"],
            "tags": [],
            "hasFile": False,
            "isAvailable": True,
            "statistics": {"episodeFileCount": 0, "sizeOnDisk": 0},
        }

    @staticmethod
    def _parse_seasons(value: Any, *, default: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if value is None:
            value = default if default is not None else [{"seasonNumber": 1, "monitored": True}]
        if not isinstance(value, list):
            raise TypeError("seasons must be an array")
        seasons: list[dict[str, Any]] = []
        seen: set[int] = set()
        for season in value:
            if not isinstance(season, dict):
                raise TypeError("each season must be an object")
            try:
                number = int(season["seasonNumber"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("seasonNumber must be an integer") from error
            if number < 0:
                raise ValueError("seasonNumber must not be negative")
            if number in seen:
                continue
            seen.add(number)
            seasons.append({"seasonNumber": number, "monitored": bool(season.get("monitored", False))})
        return sorted(seasons, key=lambda item: item["seasonNumber"])

    def lookup_series(self, term: str) -> list[dict[str, Any]]:
        source, separator, raw_id = term.partition(":")
        if separator and source.lower() in {"tvdb", "tvmaze"}:
            try:
                external_id = int(raw_id)
            except ValueError:
                return []
            if external_id <= 0:
                return []
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM series WHERE tvdb_id = ?", (external_id,)
                ).fetchone()
            return [self._row_to_series(row)] if row is not None else [self.lookup_series_item(external_id)]

        normalized = term.strip().lower()
        if not normalized:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM series WHERE lower(title) LIKE ? ORDER BY id",
                (f"%{normalized}%",),
            ).fetchall()
        return [self._row_to_series(row) for row in rows]

    def create_or_update_series(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            tvdb_id = int(payload["tvdbId"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("tvdbId must be a positive integer") from error
        if tvdb_id <= 0:
            raise ValueError("tvdbId must be a positive integer")
        metadata = self.lookup_series_item(tvdb_id)
        tags = payload.get("tags", [])
        if not isinstance(tags, list):
            raise TypeError("tags must be an array")
        with self._connect() as connection:
            existing_row = connection.execute(
                "SELECT seasons_json, search_requested, ignore_episodes_with_files "
                "FROM series WHERE tvdb_id = ?", (tvdb_id,)
            ).fetchone()
        existing_seasons = json.loads(existing_row["seasons_json"]) if existing_row else None
        seasons = self._parse_seasons(payload.get("seasons"), default=existing_seasons)
        add_options = payload.get("addOptions")
        if add_options is not None and not isinstance(add_options, dict):
            raise TypeError("addOptions must be an object")
        search_requested = (
            int(bool(add_options.get("searchForMissingEpisodes", False)))
            if add_options is not None
            else int(existing_row["search_requested"]) if existing_row else 0
        )
        ignore_episodes_with_files = (
            int(bool(add_options.get("ignoreEpisodesWithFiles", False)))
            if add_options is not None
            else int(existing_row["ignore_episodes_with_files"]) if existing_row else 0
        )
        now = self._now()
        values = {
            "tvdb_id": tvdb_id,
            "tvmaze_id": payload.get("tvMazeId", metadata["tvMazeId"]),
            "title": str(payload.get("title") or metadata["title"]),
            "sort_title": str(payload.get("sortTitle") or payload.get("title") or metadata["sortTitle"]).lower(),
            "year": int(payload.get("year") or metadata["year"]),
            "overview": str(payload.get("overview") or metadata["overview"]),
            "status": str(payload.get("status") or metadata["status"]),
            "monitored": int(bool(payload.get("monitored", True))),
            "quality_profile_id": int(payload.get("qualityProfileId", 1)),
            "profile_id": int(payload.get("profileId", payload.get("qualityProfileId", 1))),
            "root_folder_path": str(payload.get("rootFolderPath", "/tv")),
            "series_type": str(payload.get("seriesType", "standard")),
            "season_folder": int(bool(payload.get("seasonFolder", True))),
            "tags_json": json.dumps(tags, ensure_ascii=False),
            "seasons_json": json.dumps(seasons, ensure_ascii=False),
            "search_requested": search_requested,
            "ignore_episodes_with_files": ignore_episodes_with_files,
        }
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM series WHERE tvdb_id = ?", (tvdb_id,)
            ).fetchone()
            if existing:
                series_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE series SET tvmaze_id=?, title=?, sort_title=?, year=?, overview=?,
                        status=?, monitored=?, quality_profile_id=?, profile_id=?,
                        root_folder_path=?, series_type=?, season_folder=?, tags_json=?,
                        seasons_json=?, search_requested=?, ignore_episodes_with_files=?,
                        updated_at=? WHERE id=?
                    """,
                    (*[values[key] for key in (
                        "tvmaze_id", "title", "sort_title", "year", "overview", "status",
                        "monitored", "quality_profile_id", "profile_id", "root_folder_path",
                        "series_type", "season_folder", "tags_json", "seasons_json",
                        "search_requested", "ignore_episodes_with_files"
                    )], now, series_id),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO series (
                        tvdb_id, tvmaze_id, title, sort_title, year, overview, status,
                        monitored, quality_profile_id, profile_id, root_folder_path,
                        series_type, season_folder, tags_json, seasons_json,
                        search_requested, ignore_episodes_with_files, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*[values[key] for key in (
                        "tvdb_id", "tvmaze_id", "title", "sort_title", "year", "overview", "status",
                        "monitored", "quality_profile_id", "profile_id", "root_folder_path",
                        "series_type", "season_folder", "tags_json", "seasons_json",
                        "search_requested", "ignore_episodes_with_files"
                    )], now, now),
                )
                series_id = int(cursor.lastrowid)
            for season in seasons:
                if season["monitored"]:
                    for episode_number in range(1, 4):
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO episodes (
                                series_id, season_number, episode_number, title, overview,
                                air_date, monitored, has_file
                            ) VALUES (?, ?, ?, ?, ?, ?, 1, 0)
                            """,
                            (series_id, season["seasonNumber"], episode_number,
                             f"S{season['seasonNumber']:02d}E{episode_number:02d}", None, "2026-01-01"),
                        )
            monitored_numbers = {s["seasonNumber"] for s in seasons if s["monitored"]}
            if monitored_numbers:
                placeholders = ",".join("?" for _ in monitored_numbers)
                connection.execute(
                    f"DELETE FROM episodes WHERE series_id=? AND season_number NOT IN ({placeholders})",
                    (series_id, *monitored_numbers),
                )
            else:
                connection.execute("DELETE FROM episodes WHERE series_id=?", (series_id,))
            row = connection.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        assert row is not None
        return self._row_to_series(row)

    def list_series(self, tvdb_id: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if tvdb_id is None:
                rows = connection.execute("SELECT * FROM series ORDER BY id").fetchall()
            else:
                rows = connection.execute("SELECT * FROM series WHERE tvdb_id=?", (tvdb_id,)).fetchall()
        return [self._row_to_series(row) for row in rows]

    def get_series(self, series_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
        if row is None:
            raise SeriesNotFoundError(series_id)
        return self._row_to_series(row)

    def delete_series(self, series_id: int) -> None:
        with self._connect() as connection:
            deleted = connection.execute("DELETE FROM series WHERE id=?", (series_id,)).rowcount
        if not deleted:
            raise SeriesNotFoundError(series_id)

    def list_episodes(self, series_id: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if series_id is None:
                rows = connection.execute("SELECT * FROM episodes ORDER BY series_id, season_number, episode_number").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM episodes WHERE series_id=? ORDER BY season_number, episode_number",
                    (series_id,),
                ).fetchall()
        return [self._row_to_episode(row) for row in rows]

    def import_episode_files(
        self,
        series_id: int,
        episode_files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Attach already-imported local files to Sonarr episode records.

        This is deliberately explicit: Foxarr does not inspect the filesystem,
        move files, or invoke Jellyfin. The caller supplies the verified local
        path and size after the mirror/import step has completed.
        """
        if not isinstance(series_id, int) or isinstance(series_id, bool) or series_id < 1:
            raise ValueError("seriesId must be a positive integer")
        if not isinstance(episode_files, list) or not episode_files:
            raise ValueError("episodeFiles must be a non-empty array")
        now = self._now()
        with self._connect() as connection:
            series = connection.execute(
                "SELECT * FROM series WHERE id = ?", (series_id,)
            ).fetchone()
            if series is None:
                raise SeriesNotFoundError(series_id)

            seen_episode_ids: set[int] = set()
            imported_ids: list[int] = []
            for item in episode_files:
                if not isinstance(item, dict):
                    raise TypeError("each episodeFile must be an object")
                try:
                    episode_id = int(item["episodeId"])
                    size = int(item["size"])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError("episodeId and size must be integers") from error
                path = item.get("path")
                if episode_id < 1 or size < 0:
                    raise ValueError("episodeId must be positive and size must not be negative")
                if not isinstance(path, str) or not path.startswith("/"):
                    raise ValueError("path must be an absolute path")
                if episode_id in seen_episode_ids:
                    raise ValueError("episodeIds must be unique")
                seen_episode_ids.add(episode_id)
                episode = connection.execute(
                    "SELECT * FROM episodes WHERE id = ? AND series_id = ?",
                    (episode_id, series_id),
                ).fetchone()
                if episode is None:
                    raise EpisodeNotFoundError(episode_id)
                file_record = {
                    "id": episode_id,
                    "path": path,
                    "relativePath": item.get("relativePath") or path,
                    "size": size,
                    "dateAdded": str(item.get("dateAdded") or now),
                }
                connection.execute(
                    "UPDATE episodes SET has_file=1, episode_file_json=? WHERE id=?",
                    (json.dumps(file_record, ensure_ascii=False), episode_id),
                )
                imported_ids.append(episode_id)

            stats = connection.execute(
                "SELECT COUNT(*) AS file_count, COALESCE(SUM(CAST(json_extract(episode_file_json, '$.size') AS INTEGER)), 0) AS total_size "
                "FROM episodes WHERE series_id = ? AND has_file = 1",
                (series_id,),
            ).fetchone()
            connection.execute(
                "UPDATE series SET episode_file_count=?, size_on_disk=?, updated_at=? WHERE id=?",
                (int(stats["file_count"]), int(stats["total_size"]), now, series_id),
            )

            jobs = connection.execute(
                "SELECT * FROM search_jobs WHERE media_type='series' AND series_id=? "
                "AND transmission_id IS NOT NULL ORDER BY id",
                (series_id,),
            ).fetchall()
            for job in jobs:
                target_ids = json.loads(job["target_episode_ids_json"])
                if not target_ids:
                    continue
                placeholders = ",".join("?" for _ in target_ids)
                present = connection.execute(
                    f"SELECT COUNT(*) AS count FROM episodes WHERE series_id=? AND id IN ({placeholders}) AND has_file=1",
                    (series_id, *target_ids),
                ).fetchone()
                job_status = "imported" if int(present["count"]) == len(target_ids) else "awaiting_external_import"
                connection.execute(
                    "UPDATE search_jobs SET status=?, updated_at=? WHERE id=?",
                    (job_status, now, job["id"]),
                )

            series_row = connection.execute(
                "SELECT * FROM series WHERE id = ?", (series_id,)
            ).fetchone()
            episode_rows = connection.execute(
                f"SELECT * FROM episodes WHERE id IN ({','.join('?' for _ in imported_ids)}) ORDER BY season_number, episode_number",
                imported_ids,
            ).fetchall()
            updated_jobs = connection.execute(
                "SELECT * FROM search_jobs WHERE media_type='series' AND series_id=? "
                "AND transmission_id IS NOT NULL ORDER BY id",
                (series_id,),
            ).fetchall()
        assert series_row is not None
        return {
            "series": self._row_to_series(series_row),
            "episodes": [self._row_to_episode(row) for row in episode_rows],
            "jobs": [self._row_to_search_job(row) for row in updated_jobs],
        }

    def get_episode(self, episode_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if row is None:
            raise EpisodeNotFoundError(episode_id)
        return self._row_to_episode(row)

    def monitor_episodes(self, episode_ids: list[int], monitored: bool) -> None:
        if not isinstance(episode_ids, list) or not all(isinstance(value, int) and value > 0 for value in episode_ids):
            raise TypeError("episodeIds must be an array of positive integers")
        with self._connect() as connection:
            for episode_id in episode_ids:
                connection.execute("UPDATE episodes SET monitored=? WHERE id=?", (int(monitored), episode_id))

    def create_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        search_job_id: int | None = None
        series_id = payload.get("seriesId")
        if name == "MissingEpisodeSearch":
            if series_id is None:
                raise ValueError("seriesId must be provided")
            try:
                series_id = int(series_id)
            except (TypeError, ValueError) as error:
                raise ValueError("seriesId must be a positive integer") from error
            if series_id <= 0:
                raise ValueError("seriesId must be a positive integer")
        if series_id is not None:
            try:
                series_id = int(series_id)
            except (TypeError, ValueError) as error:
                raise ValueError("seriesId must be a positive integer") from error
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM series WHERE id = ?", (series_id,)
                ).fetchone()
                if exists is None:
                    raise SeriesNotFoundError(series_id)
            if name == "MissingEpisodeSearch":
                search_job_id = self.create_series_search_job(series_id)
                payload = {**payload, "searchJobId": search_job_id}
        now = self._now()
        with self._connect() as connection:
            if name == "MissingEpisodeSearch":
                connection.execute(
                    "UPDATE series SET search_requested=1, updated_at=? WHERE id=?",
                    (now, series_id),
                )
            cursor = connection.execute(
                """
                INSERT INTO commands (
                    name, body_json, search_job_id, status, result,
                    queued_at, started_at, ended_at
                ) VALUES (?, ?, ?, 'completed', 'successful', ?, ?, ?)
                """,
                (name, json.dumps(payload, ensure_ascii=False), search_job_id, now, now, now),
            )
            command_id = int(cursor.lastrowid)
        return {
            "id": command_id, "name": name, "commandName": name, "body": payload,
            "status": "completed", "queued": now, "started": now, "ended": now,
            "result": "successful",
            "searchJobId": search_job_id,
        }

    def get_command(self, command_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise KeyError(command_id)
        body = json.loads(row["body_json"])
        return {
            "id": row["id"], "name": row["name"], "commandName": row["name"],
            "body": body, "status": row["status"],
            "queued": row["queued_at"], "started": row["started_at"],
            "ended": row["ended_at"], "result": row["result"],
            "searchJobId": row["search_job_id"],
        }

    def create_search_job(
        self,
        query: str,
        indexer_ids: list[int],
        limit: int,
        *,
        media_type: str = "movie",
        movie_id: int | None = None,
        series_id: int | None = None,
        season_number: int | None = None,
        target_episode_ids: list[int] | None = None,
        status: str = "running",
    ) -> int:
        if media_type not in {"movie", "series"}:
            raise ValueError("media_type must be movie or series")
        if status not in {"queued", "running"}:
            raise ValueError("search job status must be queued or running")
        if movie_id is not None:
            if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id < 1:
                raise ValueError("movie_id must be a positive integer")
            if media_type != "movie":
                raise ValueError("movie_id requires a movie search job")
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM movies WHERE id = ?", (movie_id,)
                ).fetchone()
            if exists is None:
                raise MovieNotFoundError(movie_id)
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO search_jobs (
                    query, indexer_ids_json, status, result_count, results_json,
                    error, dry_run, created_at, updated_at, media_type, movie_id, series_id,
                    season_number, target_episode_ids_json
                ) VALUES (?, ?, ?, 0, '[]', NULL, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query,
                    json.dumps(indexer_ids),
                    status,
                    now,
                    now,
                    media_type,
                    movie_id,
                    series_id,
                    season_number,
                    json.dumps(target_episode_ids or []),
                ),
            )
            return int(cursor.lastrowid)

    def create_series_search_job(
        self, series_id: int, season_number: int | None = None
    ) -> int:
        with self._connect() as connection:
            series = connection.execute(
                "SELECT id, title FROM series WHERE id = ?", (series_id,)
            ).fetchone()
            if series is None:
                raise SeriesNotFoundError(series_id)
            if season_number is None:
                episodes = connection.execute(
                    "SELECT id FROM episodes WHERE series_id = ? AND monitored = 1 "
                    "AND has_file = 0 ORDER BY season_number, episode_number",
                    (series_id,),
                ).fetchall()
            else:
                episodes = connection.execute(
                    "SELECT id FROM episodes WHERE series_id = ? AND season_number = ? "
                    "AND monitored = 1 AND has_file = 0 ORDER BY episode_number",
                    (series_id, season_number),
                ).fetchall()
        target_episode_ids = [int(row["id"]) for row in episodes]
        query = str(series["title"])
        if season_number is not None:
            query = f"{query} S{season_number:02d}"
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM search_jobs WHERE media_type='series' AND series_id=? "
                "AND season_number IS ? AND status IN ('queued', 'running') "
                "ORDER BY id DESC LIMIT 1",
                (series_id, season_number),
            ).fetchone()
        if existing is not None:
            return int(existing["id"])
        return self.create_search_job(
            query,
            [],
            20,
            media_type="series",
            series_id=series_id,
            season_number=season_number,
            target_episode_ids=target_episode_ids,
            status="queued",
        )

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

    def start_search_job(self, job_id: int) -> dict[str, Any]:
        """Move a queued search job to running without performing the search."""
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"queued", "running"}:
                raise ValueError("only queued or running search jobs can be started")
            connection.execute(
                "UPDATE search_jobs SET status='running', updated_at=? WHERE id=?",
                (now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM search_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert updated is not None
        return self._row_to_search_job(updated)

    def get_search_job(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM search_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_search_job(row)

    def list_download_jobs(self, media_type: str | None = None) -> list[dict[str, Any]]:
        """Return search jobs that have a Transmission torrent for queue reads."""
        with self._connect() as connection:
            if media_type is None:
                rows = connection.execute(
                    "SELECT * FROM search_jobs WHERE transmission_id IS NOT NULL ORDER BY id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM search_jobs WHERE transmission_id IS NOT NULL "
                    "AND media_type = ? ORDER BY id",
                    (media_type,),
                ).fetchall()
        return [self._row_to_search_job(row) for row in rows]

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
            "mediaType": row["media_type"],
            "movieId": row["movie_id"],
            "seriesId": row["series_id"],
            "seasonNumber": row["season_number"],
            "targetEpisodeIds": json.loads(row["target_episode_ids_json"]),
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
            "movieFile": (
                json.loads(row["movie_file_json"])
                if row["movie_file_json"]
                else None
            ),
        }

    @staticmethod
    def _row_to_series(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tvdbId": row["tvdb_id"],
            "tvMazeId": row["tvmaze_id"],
            "imdbId": f"tt{row['tvdb_id']:07d}",
            "title": row["title"],
            "sortTitle": row["sort_title"],
            "year": row["year"],
            "overview": row["overview"],
            "status": row["status"],
            "monitored": bool(row["monitored"]),
            "seriesType": row["series_type"],
            "seasonFolder": bool(row["season_folder"]),
            "qualityProfileId": row["quality_profile_id"],
            "rootFolderPath": row["root_folder_path"],
            "seasons": json.loads(row["seasons_json"]),
            "images": [],
            "genres": ["Foxarr"],
            "tags": json.loads(row["tags_json"]),
            "added": row["created_at"],
            "hasFile": bool(row["episode_file_count"]),
            "isAvailable": True,
            "statistics": {
                "episodeFileCount": row["episode_file_count"],
                "sizeOnDisk": row["size_on_disk"],
            },
            "originalTitle": row["title"],
            "searchRequested": bool(row["search_requested"]),
        }

    @staticmethod
    def _row_to_episode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "seriesId": row["series_id"],
            "seasonNumber": row["season_number"],
            "episodeNumber": row["episode_number"],
            "title": row["title"],
            "overview": row["overview"],
            "airDate": row["air_date"],
            "monitored": bool(row["monitored"]),
            "hasFile": bool(row["has_file"]),
            "episodeFile": json.loads(row["episode_file_json"]) if row["episode_file_json"] else None,
        }
