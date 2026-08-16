# Foxarr

> A lightweight Radarr-compatible media request adapter for Seerr.

Foxarr is an open-source bridge for self-hosted media servers. It is designed to let [Seerr](https://seerr.dev/) submit movie requests through a small Radarr-compatible API, then search configured indexers through [Prowlarr](https://prowlarr.com/) and send selected torrents to a download client such as [Transmission](https://transmissionbt.com/).

```text
Seerr → Foxarr → Prowlarr → download client → media library
```

## Status

🚧 **Early development.** The repository now contains a movie-only Radarr-compatible
MVP. It accepts Seerr movie requests and persists them in SQLite. Prowlarr
search is currently available only as an explicit read-only dry-run operation;
Foxarr does not download files.

Foxarr is **not** a Radarr fork. It will implement only the Radarr API surface required by Seerr and delegate search and download work to configurable providers.

## Current movie MVP

The service implements the minimum movie surface captured from Seerr 3.4.1:

```text
GET  /api/v3/system/status
GET  /api/v3/qualityProfile
GET  /api/v3/rootfolder
GET  /api/v3/tag
GET  /api/v3/movie/lookup
POST /api/v3/movie
GET  /api/v3/movie
GET  /api/v3/movie/{id}
```

Movie creation is idempotent by `tmdbId`. The MVP always reports
`hasFile: false`; `addOptions.searchForMovie` is recorded but does not trigger
external work.

The explicit, read-only Prowlarr dry-run endpoints are:

```text
POST /api/internal/dry-run/search
GET  /api/internal/dry-run/search/{job_id}
```

Example request:

```json
{
  "query": "Матрица",
  "indexerIds": [1],
  "limit": 5
}
```

The job stores safe release metadata in SQLite. It never calls Transmission
and does not persist download or magnet URLs. Search is explicit; a normal
Seerr movie request does not start it automatically.

When `indexerIds` is an empty array, Foxarr reads the enabled indexers from
Prowlarr and queries them independently in parallel. Results are merged and
deduplicated by `guid`; a slow or failed indexer is recorded as a partial
search error and does not discard successful results from other indexers. The
job fails only when every indexer search fails.

When `indexerIds` is an empty array, Foxarr reads the enabled indexers from
Prowlarr and queries them independently in parallel. Results are merged and
deduplicated by `guid`; a slow or failed indexer is recorded as a partial
search error and does not discard successful results from other indexers. The
job fails only when every indexer search fails.

After a completed dry-run job, release selection is also explicit and local:

```text
POST /api/internal/dry-run/search/{job_id}/select
```

The endpoint ranks only the safe metadata already stored in the job and saves
the selected result with `status: selected`. It supports hard constraints and
soft preferences for:

```text
minSize / maxSize              bytes
minResolution / maxResolution 480p, 720p, 1080p, 2160p
allowedResolutions             exact resolution allow-list
allowedVideoCodecs             h264, hevc, av1, vp9
allowedSources                 web-dl, webrip, bluray, bdremux, remux
allowedHdr                     sdr, hdr, hdr10, hdr10+, dv
allowedAudioCodecs             aac, ac3, eac3, dts, dts-hd, truehd, atmos
minSeeders / preferredLanguages / preferredQuality
```

The corresponding `preferred*` fields (`preferredResolutions`,
`preferredVideoCodecs`, `preferredSources`, `preferredHdr`, and
`preferredAudioCodecs`) affect ranking but do not reject a release. Technical
metadata is read from explicit Prowlarr fields when available and otherwise
parsed from the release title. When a hard technical filter is enabled and a
release does not expose that attribute, Foxarr rejects it rather than guessing.

Profiles can be supplied through `FOXARR_SELECTION_PROFILES_JSON`. A profile
can be selected by passing `qualityProfileId` to the internal selection
endpoint; request criteria override the profile's defaults. Example:

```json
{
  "1": {
    "name": "1080p HEVC",
    "criteria": {
      "maxSize": 15000000000,
      "allowedResolutions": ["1080p"],
      "allowedVideoCodecs": ["hevc"],
      "allowedSources": ["web-dl", "bluray"],
      "allowedAudioCodecs": ["eac3", "dts-hd"]
    }
  }
}
```

The deployed default profile is currently `Any <= 8 GB, 5+ seeders`: a
release must be no larger than 8,000,000,000 bytes and have at least five
seeders. Quality, codec, HDR, source, and audio restrictions remain available
for a future stricter profile.

It has no download client integration and cannot start a search by itself.

For the next integration stage, a selected job can produce a side-effect-free
download plan:

```text
POST /api/internal/dry-run/search/{job_id}/plan
```

The plan contains a preview of Transmission's `torrent-add` RPC, target
directory, labels, and `execution: not_submitted`. The real download URL is
deliberately not stored or resolved by this endpoint. Transmission RPC
handshake and error handling are covered by local mock-transport tests, but no
Transmission service is configured or called by Foxarr yet.

The selected release can also be resolved by its stored Prowlarr `guid` and
turned into a redacted submit preview:

```text
POST /api/internal/dry-run/search/{job_id}/submit-preview
```

Foxarr re-queries Prowlarr, uses the download URL only in process memory, and
returns only `urlKind` plus a redacted `torrent-add` preview. The URL is not
returned, logged, or written to SQLite. `execution` remains
`not_submitted`.

Transmission download directories are remote paths on the Transmission host,
not paths inside the Foxarr container:

```text
FOXARR_TRANSMISSION_MOVIE_DIR=/home/blackfox/data/film
FOXARR_TRANSMISSION_SERIES_DIR=/home/blackfox/data/serial
```

Foxarr monitors only Transmission state. The separate media-mirror service is
outside Foxarr: it copies completed files from those directories to the media
library, after which Jellyfin and Seerr perform their own availability sync.
Foxarr lifecycle state can therefore be:

```text
selected → download_planned → paused → downloading
→ transmission_completed → awaiting_external_import
```

The internal status snapshot endpoint is:

```text
POST /api/internal/dry-run/search/{job_id}/transmission
```

It stores only torrent id, status, progress, remote directory, and an error
string. It does not inspect Contabo media paths and does not call mirror or
Jellyfin.

## Planned integrations

- Radarr-compatible API for Seerr movie requests
- SQLite-backed request and job state
- Prowlarr JSON API integration (read-only dry-run first)
- Release selection by quality, size, seeders, and language
- Transmission RPC integration
- Dry-run mode that searches and logs without downloading
- Idempotent request handling and structured status logging
- Docker Compose deployment and environment-based configuration

## Design goals

- Keep the service small and easy to self-host
- Never log API keys, cookies, or signed download URLs
- Make provider integrations replaceable
- Prefer explicit configuration over hidden magic
- Be useful with a single indexer as well as a larger *arr-style setup

## Development

The implementation is intentionally being built in small, testable steps. Contributions, issue reports, and design feedback are welcome.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

Run locally:

```bash
uvicorn foxarr.app:app --reload --port 7878
```

Or with Docker:

```bash
docker compose up --build
```

The SQLite database is stored in `/data/foxarr.db` in the container. Published
ports default to loopback only.

## License

MIT. See [LICENSE](LICENSE).
