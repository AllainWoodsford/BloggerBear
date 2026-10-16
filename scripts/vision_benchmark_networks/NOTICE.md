# Notice for the network fixtures in this directory

Contains information from OpenStreetMap, © OpenStreetMap contributors, available under the Open
Database Licence 1.0 (https://opendatacommons.org/licenses/odbl/).

This repository's Apache-2.0 licence does not cover the network files in this directory
(`*.json`): they are extracts of OpenStreetMap data and stay under the ODbL. The query that makes
them asks Overpass for no `meta`, so a file holds no user names, user ids or changeset ids.

Each fixture is listed here with where and when it came from, so a run that cites its sha256 says
which copy of the map it measured. The file itself is captured after the deployment (the
runsheet's COOL steps say how: the deployed running state's cached network, or `fetch-network`),
and is committed only if `scripts/pii_denylist_check.py --staged` passes on it; otherwise it stays
out of git and the manifest pins its sha256. Fill the row in when the file lands, from the file's
own `fetched_at`, `endpoint` and `query_url` fields and `sha256sum`.

| File | Extract date (`fetched_at`) | Endpoint | Query URL | sha256 |
|---|---|---|---|---|
| `sydney.json` | (the file's `fetched_at`) | (the file's `endpoint`) | (the file's `query_url`) | (`sha256sum sydney.json`) |
