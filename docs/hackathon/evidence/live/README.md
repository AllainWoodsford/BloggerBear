# Live evidence

Each directory here, `<UTC date>/`, is one run of `scripts/evidence_export.py export` against the
deployed dev system: what it measured, decided and did, read from its logs, its running states and
its tables, with `provenance.json` saying where every line came from and which code produced it.
Only code-derived fields are written (codes, ids, times, numbers); model and third-party text only
when the export was run with `--with-notes`, and then labelled as unverified.
docs/hackathon/evidence/README.md says what each file holds and how to run the export.

`labels.csv`, when present, is the operator's agreement with the agent on each triaged live
decision (`labels-template.csv`, filled in); `python scripts/vision_eval.py index --check` checks it.

## Credits

- Contains modified Copernicus Sentinel data (2024-2026), processed by ESA and distributed on AWS
  Open Data (`sentinel-cogs`): the scenes the decisions name and the figures under `figures/`.
- Rail network data © OpenStreetMap contributors, available under the Open Database Licence 1.0
  (https://opendatacommons.org/licenses/odbl/): the decisions name stations by their OpenStreetMap
  ids, and the figures draw the network.
