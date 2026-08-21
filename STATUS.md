# STATUS — Knooppunten toolset
Paste this back at the start of a session to get oriented fast. Last updated: 2026-07-18.
## Project in one line
CLI tools for Boat Bike Tours that turn routes into guest route notes on the Dutch
knooppunten network. ONE complete mode — every guest has GPS + paper; digital dependency
is the guest's in-field choice, not a build mode (pivot 2026-07-18).
## THE ONE COMMAND (make new notes)
  python make_notes.py --cache route_cache --out notes      # every cached route -> txt + html
  python make_notes.py 56125488.json                        # one route
Sidecar highlights: put <id>.pois.json next to a route and it's woven in automatically.
## Shipped & working
- knooppunten-svg-export.html — printable SVG node strip.
- gpx_to_knooppunten.py — GPX -> node sequence + distance; offline node cache.
- gpx_to_routenotes.py — text notes from a GPX; heading anchors; gap markers.
- rwgps.py — RWGPS provider; pulls routes + author course points; header auth confirmed.
- rwgps_pull.py — batch puller: filters, --dry-run, --limit, resume. ~3921 routes cached.
- route_sheet.py — COMPLETE tool. Merges author cues + nodes-from-cues + POIs + distance into a
  single complete sheet, text and HTML (--html), external highlights via --pois.
- make_notes.py — orchestrator: batch cached routes -> finished txt + html, auto-detect sidecar POIs.
- poi_master.py — POI cache. One master_pois.json (multilingual en/nl/de). Commands:
    merge <master> <gpx...>        add POIs from GPX waypoints (dedup, stable id, EN-only=to-translate)
    pick  <master> <gpx> --emit    write <id> sidecar (flat, single --lang, Marc's shape) for near-track POIs
    view  <master> --out csv       flatten to a filterable CSV (todo col flags coords/translate gaps)
## route_sheet merge rules (locked)
- dedupe: --dedupe-m default 10 m (near-coincident duplicates only; TUNE later).
- same-junction collapse ON: a run to one node -> "Follow signs to junction X via <street> (exit …) toward Y".
- street collapse ON (off-network analog): a turn onto a road absorbs "keep/continue" filler ->
  "Turn onto <street> (follow N km)". Only keep/continue folds; ferries/attentions/turns keep their line.
- => HYBRID works automatically: junction-collapse on signed parts, street-collapse on off-network gaps.
- fold nameless "continue straight" ON. Bare turns kept. Leading zeros normalized.
- POIs woven INTO the directions flow by distance (★ / cards), not a separate list.
## POI master flow
- master_pois.json is source of truth + translation store. Routes never own POIs; they pull.
- master -> `pick --emit` -> <id> sidecar (single language, keys: name/type/lat/lon/desc) -> make_notes weaves in.
- hand-authored sidecars (e.g. 56125488) and pick-emitted ones are byte-identical; both valid.
- coord convention: `lon` (not lng), matching the sidecars.
## Parked / open (not blocked)
- Calibrate --dedupe-m against more tracks.
- Translation: deterministic glossary for cues (BUILT) + DeepL/human-filled for POI desc; protect proper nouns.
- Geocode curated master POIs (11 German Rhine/Mosel entries have null coords) — Overpass or own machine.
- Cosmetic: ferry line shows "(follow N km)" — could suppress on ferry/attention cues.
- Ferry POIs: Königswinter–Andernach uses a Rhine ferry; needs a ferry POI + "board the ferry" cue, not a turn.
- RWGPS "Experiences" NOT in API — final assembly is manual in the dashboard.
- Confirm sidecar filename the detector expects: <id>.pois.json (dot) vs <id>_pois.json (underscore).
## Key facts
- Sandbox can't reach RWGPS/Overpass; live fetches run on  machine. Tools built to spec + tested offline.
- RWGPS auth = headers x-rwgps-api-key / x-rwgps-auth-token. Own routes only.
- Course points weave junctions into cue text ("...to junction 56") -> nodes parsed from cues; no OSM needed.
- Cached route files are named <id>.json (no prefix). Active set = routes named/updated 2026.
## Token-saving working style
- Use this STATUS.md instead of re-reading transcripts.
- Targeted edits, show only changed lines.
- Batch decisions into one proposal.
- Mass processing runs on local machine.
