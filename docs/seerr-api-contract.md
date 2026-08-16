# Seerr 3.4.1 API contract used by Foxarr

> Working contract captured from a live Seerr 3.4.1 integration test against
> `foxarr-mock` on 2026-08-16. This records observed traffic, not the complete
> Radarr/Sonarr API.

## Test setup

- Seerr: 3.4.1, API `/api/v3`
- Mock Radarr: `serverId=1`, `mock-radarr:7878`
- Mock Sonarr: `serverId=1`, `mock-sonarr:8989`
- Real *arr instances: `serverId=0`, not used by the tests
- Mock services: `syncEnabled=false`; `preventSearch=true` except for the
  explicitly isolated search-enabled test
- No Transmission or Jellyfin operation was performed

## Connection check

Observed for both Radarr and Sonarr:

```text
GET /api/v3/system/status
GET /api/v3/qualityProfile
GET /api/v3/rootfolder
GET /api/v3/tag
```

The path is case-sensitive for the mock contract: Seerr uses
`/qualityProfile` with an uppercase `P`. An empty JSON array is accepted for
`/tag`. Sonarr 4 also reports `languageProfiles: null` to Seerr.

A selectable profile needs an integer `id` and a `name`. A selectable root
folder needs at least `id`, `path`, `accessible`, `freeSpace`, and
`totalSpace`.

## Movie flow

Test: Fight Club, TMDB `550`.

```text
GET  /api/v3/movie/lookup?term=tmdb:550
POST /api/v3/movie
GET  /api/v3/movie
GET  /api/v3/movie/1001
```

Observed create payload:

```json
{
  "title": "Бойцовский клуб",
  "qualityProfileId": 1,
  "profileId": 1,
  "titleSlug": "550",
  "minimumAvailability": "released",
  "tmdbId": 550,
  "year": 1999,
  "rootFolderPath": "/movies",
  "monitored": true,
  "tags": [],
  "addOptions": { "searchForMovie": false }
}
```

The response must contain a stable positive internal `id`. For a movie without
an imported file, the status response used by Seerr is:

```json
{
  "id": 1001,
  "tmdbId": 550,
  "title": "Mock Movie 550",
  "monitored": true,
  "hasFile": false,
  "statistics": {
    "movieFileCount": 0,
    "sizeOnDisk": 0
  }
}
```

The minimal observed movie surface is:

```text
GET  /api/v3/system/status
GET  /api/v3/qualityProfile
GET  /api/v3/rootfolder
GET  /api/v3/tag
GET  /api/v3/movie/lookup
GET  /api/v3/movie
GET  /api/v3/movie/{id}
POST /api/v3/movie
```

Movie search was disabled in this capture. The search-enabled movie command
therefore remains unverified.

## Series flow: new series

Test: Game of Thrones, TMDB `1399`, resolved by Seerr to TVDB `121361`.
Only season 1 was requested initially.

```text
GET  /api/v3/series/lookup?term=tvdb:121361
POST /api/v3/series
```

Observed create payload:

```json
{
  "tvdbId": 121361,
  "title": "Игра Престолов",
  "qualityProfileId": 1,
  "seasons": [
    { "seasonNumber": 1, "monitored": true },
    { "seasonNumber": 2, "monitored": false }
  ],
  "tags": [],
  "seasonFolder": true,
  "monitored": true,
  "rootFolderPath": "/tv",
  "seriesType": "standard",
  "addOptions": {
    "ignoreEpisodesWithFiles": true,
    "searchForMissingEpisodes": false
  }
}
```

The `seasons[]` monitoring selection must be preserved. It is not equivalent
to monitoring every season. A successful response must contain a stable
positive series `id`, `tvdbId`, title, and season state.

## Series flow: existing series and search

For the isolated search-enabled test, mock Sonarr was temporarily configured
with `preventSearch=false`, then restored to `true` afterwards.

When lookup returned the existing series with `id=1001`, Seerr sent:

```text
GET  /api/v3/series/lookup?term=tvdb:121361
PUT  /api/v3/series
GET  /api/v3/episode?seriesId=1001
POST /api/v3/command
```

The exact observed command body was:

```json
{
  "name": "MissingEpisodeSearch",
  "seriesId": 1001
}
```

The command response needs a positive `id` and a successful status/result. The
mock completes it immediately for the contract test.

The exact `PUT /series` body is the existing Sonarr series object with the
newly requested seasons monitored. In the capture Seerr changed the requested
set from season 1 to seasons 1 and 2. Seerr first fetched episodes and then
issued `MissingEpisodeSearch`.

## Episode and status reads

Observed/required read surface for the series test:

```text
GET /api/v3/series
GET /api/v3/series/{id}
GET /api/v3/episode?seriesId={id}
GET /api/v3/episode/{id}
```

The mock episode shape is:

```json
{
  "id": 1002,
  "seriesId": 1001,
  "seasonNumber": 1,
  "episodeNumber": 1,
  "title": "S01E01",
  "monitored": true,
  "hasFile": false,
  "episodeFile": null
}
```

Only monitored seasons generate episode records in the minimal mock model. A
series with only season 1 selected therefore returns no season 2 episodes.

The mock also implements the write used by Seerr's existing-series branch:

```text
PUT /api/v3/episode/monitor
```

with:

```json
{
  "episodeIds": [1002, 1003],
  "monitored": true
}
```

## Implementation rules for Foxarr

1. Keep external metadata identifiers separate from stable Foxarr ids:
   TMDB/TVDB ids are not internal movie/series/episode/command ids.
2. Preserve `seasons[]` monitoring selection on create and update.
3. Make repeated `POST /series` idempotent and apply the season selection to
   the existing record instead of returning stale state.
4. Return `hasFile` and relevant statistics even when no file exists.
5. Implement `/qualityProfile` with the exact casing and implement `/tag`.
6. Keep search explicit. `searchForMovie` and
   `searchForMissingEpisodes` can be false.
7. Implement additional endpoints only when an observed Seerr flow requires
   them; Foxarr is not intended to be a full *arr clone.

## Remaining capture work

- Capture movie search with `addOptions.searchForMovie=true`.
- Capture queue/download synchronization reads.
- Test the transition from `hasFile=false` to an imported file and document
  the exact fields Seerr needs to mark media available.
