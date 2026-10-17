# Notice for the network fixtures in this directory

Contains information from OpenStreetMap, © OpenStreetMap contributors, available under the Open
Database Licence 1.0 (https://opendatacommons.org/licenses/odbl/).

This repository's Apache-2.0 licence does not cover the network files in this directory
(`*.json`): they are extracts of OpenStreetMap data and stay under the ODbL. The query that makes
them asks Overpass for no `meta`, so a file holds no user names, user ids or changeset ids.

Each fixture is listed here with where and when it came from, so a run that cites its sha256 says
which copy of the map it measured. The file itself is captured after the deployment (the
[COOL benchmark runsheet](../../docs/cool-benchmark-runsheet.md), step 5, says how: the deployed
city's network object, `vision/<topic>/sydney/network.json`, or `fetch-network`), and is committed
only if `scripts/pii_denylist_check.py --staged` passes on it; otherwise it stays
out of git, its row here is still filled in, and every run file that read it cites its sha256
(`inputs.networks`). Fill the row in when the file lands, from the file's own `fetched_at`,
`endpoint` and `query_url` fields and `sha256sum`.

| File | Extract date (`fetched_at`) | Endpoint | Query URL | sha256 |
|---|---|---|---|---|
| `sydney.json` | 2026-10-16T23:09:21.194021+00:00 | https://overpass-api.de/api/interpreter | `https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A25%5D%5Bbbox%3A-34.16%2C150.86%2C-33.58%2C151.56%5D%3B%28way%5B%22railway%22~%22%5E%28rail%7Clight_rail%7Csubway%29%24%22%5D%3Bnode%5B%22railway%22~%22%5E%28station%7Chalt%29%24%22%5D%3B%29%3Bout%20geom%3B` | `bfc3c7f868f52f755eb3c1a8836729bf4dcc5ee079cf466cf51968921fd77eb4` |
| `melbourne.json` | 2026-10-16T23:09:21.194021+00:00 | https://overpass-api.de/api/interpreter | `https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A25%5D%5Bbbox%3A-37.9%2C144.8%2C-37.55%2C145.3%5D%3B%28way%5B%22railway%22~%22%5E%28rail%7Clight_rail%7Csubway%29%24%22%5D%3Bnode%5B%22railway%22~%22%5E%28station%7Chalt%29%24%22%5D%3B%29%3Bout%20geom%3B` | `24c78fa8d7d06716a79d0aa9b15eb0b9679f0bc03639a86ddd2a8c8d8d77f867` |
| `singapore.json` | 2026-10-16T23:12:57.075350+00:00 | https://overpass-api.de/api/interpreter | `https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A25%5D%5Bbbox%3A1.09%2C103.56%2C1.61%2C104.08%5D%3B%28way%5B%22railway%22~%22%5E%28rail%7Clight_rail%7Csubway%29%24%22%5D%3Bnode%5B%22railway%22~%22%5E%28station%7Chalt%29%24%22%5D%3B%29%3Bout%20geom%3B` | `f0bb671b438f6cc42c438a769e974ede919315531664ab223056b5a5d2f83ce1` |
| `boston.json` | 2026-10-16T23:14:41.854480+00:00 | https://overpass-api.de/api/interpreter | `https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A25%5D%5Bbbox%3A42.15%2C-71.3%2C42.42%2C-70.9%5D%3B%28way%5B%22railway%22~%22%5E%28rail%7Clight_rail%7Csubway%29%24%22%5D%3Bnode%5B%22railway%22~%22%5E%28station%7Chalt%29%24%22%5D%3B%29%3Bout%20geom%3B` | `c360e8a9dc01333b6fa6807ac6e390b536298b2865922b43095716d35397165a` |
