# Notice for the network fixtures in this directory

Contains information from OpenStreetMap, © OpenStreetMap contributors, available under the Open
Database Licence 1.0 (https://opendatacommons.org/licenses/odbl/).

This repository's Apache-2.0 licence does not cover the network files in this directory
(`*.json`): they are extracts of OpenStreetMap data and stay under the ODbL. The query that makes
them asks Overpass for no `meta`, so a file holds no user names, user ids or changeset ids.

Each fixture is listed here with where and when it came from, so a run that cites its sha256 says
which copy of the map it measured. The file itself is captured after the deployment (the
[COOL benchmark runsheet](../../docs/cool-benchmark-runsheet.md), step 5, says how: the deployed city's network object, `vision/<topic>/sydney/network.json`,
or `fetch-network`),
and is committed only if `scripts/pii_denylist_check.py --staged` passes on it; otherwise it stays
out of git, its row here is still filled in, and every run file that read it cites its sha256
(`inputs.networks`). Fill the row in when the file lands, from the file's own `fetched_at`,
`endpoint` and `query_url` fields and `sha256sum`.

| File | Extract date (`fetched_at`) | Endpoint | Query URL | sha256 |
|---|---|---|---|---|
| `sydney.json` | (the file's `fetched_at`) | (the file's `endpoint`) | (the file's `query_url`) | (`sha256sum sydney.json`) |
