# GeoPulse
Spatial Event &amp; Infrastructure Ingestion Platform

## Local database

Start PostgreSQL with PostGIS enabled:

```sh
docker compose -f infrastructure/docker-compose.yaml up -d
```

The Compose file uses the PostgreSQL 19 release-candidate PostGIS image
(`19rc1-3.6`). By default, the database and user are both `geopulse`, with
password `geopulse`. Configure `POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, or `POSTGRES_PORT` in the environment to override these
defaults. The database is available on `localhost:5432` and its data persists
in the `postgres_data` volume.

## USGS earthquake ingestion

Install the Python dependency and run the loader from the repository root:

```sh
python -m pip install -r python/requirements.txt
python python/dataingestion.py
```

The script reads configuration directly from `python/.env`. Set `PGHOST`,
`PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `API_URL`, `PAGE_SIZE`,
`REQUEST_TIMEOUT_SECONDS`, and `MAX_REQUEST_ATTEMPTS` there for local
configuration; `DATABASE_URL` can be used instead of the individual
PostgreSQL connection settings. The script does not read process environment
variables. The `.env` file is git-ignored and should not be committed.

The script queries the USGS FDSN Event API for events with a significance
score of at least 600, starting at year 0001 to cover the full available
catalog. It pages through results in origin-time order, then creates the
PostGIS `earthquakes` table and inserts or updates events by their USGS event
ID. The table includes the original event properties as JSONB and a WGS 84
point geometry.

The connection defaults match the local Compose service (`localhost:5432`,
database/user/password `geopulse`). Set `USGS_MIN_SIG` (0-1000, default `600`)
or `USGS_STARTTIME` (ISO-8601 UTC, default `0001-01-01T00:00:00Z`) to
customize the query. The script can be rerun safely; existing event IDs are
updated.
