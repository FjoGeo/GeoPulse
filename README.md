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
