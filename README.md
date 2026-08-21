# Boat Bike Tours — Knooppunten route-notes toolset

A small set of command-line tools that turn cycling routes into guest-ready
route notes, built around the Dutch knooppunten (numbered cycle-node) network.
Everything is plain Python 3 (standard library only) plus one self-contained
HTML tool. No frameworks to install.

Two guest modes run through all of it:
- **digitally-dependent** — the rider follows a GPS track; notes stay light.
- **digitally-independent** — signs & paper only; notes carry full detail.

---

## The tools

**knooppunten-svg-export.html** — open in any browser. Type a knooppunten
sequence and it renders a clean printable SVG strip (red numbered nodes, arrows,
ferry marker, start/finish dock glyphs) for A4/A3/A5. Fully standalone.

**gpx_to_knooppunten.py** — reads a GPX track, snaps it to OpenStreetMap's
rcn_ref nodes, and prints the knooppunten sequence + distance. Has an offline
node cache so it only hits the network once per area.

**gpx_to_routenotes.py** — text route notes from a GPX track: `--mode dependent`
(light) or `--mode independent` (full), with heading anchors and honest
off-network gap markers. Builds on the node reader.

**rwgps.py** — Ride with GPS provider. Pulls your routes and, crucially, the
author's **course points** (the real turn cues the GPX export drops). Turns
them into a clean cue sheet.

**rwgps_pull.py** — batch-fetches routes to a local cache (`route_cache/`) with
filters, a `--dry-run` preview, `--limit`, and resume-on-rerun. The one online
step; everything downstream runs offline against the cache.

**route_sheet.py** — the complete tool. Reads one cached route and merges every
source into a two-mode sheet: author cues (backbone), knooppunten (pulled from
the cue text), POIs (as stops), and distance. Content-merges the cues into a
clean node-to-node cuesheet.

---

## How they chain

```
RWGPS account
   │  rwgps_pull.py  (online, once, with filters)
   ▼
route_cache/*.json  ──►  route_sheet.py  ──►  guest route sheet (2 modes)
                          (offline, repeatable)

GPX file  ──►  gpx_to_knooppunten.py / gpx_to_routenotes.py
               (for tracks not coming from RWGPS)
```

---

## Setup

Python 3.8+ required. Put all files in one folder (the tools import each other).

**RWGPS credentials** (for rwgps.py / rwgps_pull.py):
In RideWithGPS → Account → Settings → Developers, create an API client
(gives an api_key), then generate an Auth Token on that client. Then set:

```
# Windows (cmd)
set RWGPS_API_KEY=your-key
set RWGPS_AUTH_TOKEN=your-token

# Mac/Linux
export RWGPS_API_KEY=your-key
export RWGPS_AUTH_TOKEN=your-token
```

Auth is via request headers (x-rwgps-api-key / x-rwgps-auth-token). You can only
read routes you own.

---

## Typical run

```
# 1. preview which routes match your filters (downloads nothing)
python rwgps_pull.py --dry-run --updated-since 2026-01-01

# 2. cache the matching routes (skips any already cached)
python rwgps_pull.py --updated-since 2026-01-01

# 3. build a sheet from one cached route
python route_sheet.py route_cache/29122506.json                 # independent
python route_sheet.py route_cache/29122506.json --mode dependent
```

Run any tool with `-h` for its full option list.

---

## Notes / caveats

- The tools that fetch (node lookups, RWGPS) need internet the first time; after
  caching they run fully offline. A blocked work network is fine once cached.
- `route_sheet.py` content-merge rules default ON: same-junction collapse,
  fold nameless "continue straight". Proximity is a low de-dupe (`--dedupe-m`,
  default 10 m) — tune it once you've run several tracks.
- POIs: name shown inline, full descriptions collected in a STOPS list.

## Make notes in one command (make_notes.py)

Once routes are cached, generate finished notes (a .txt and .html per route):

```
python make_notes.py --cache route_cache --out notes     # all cached routes
python make_notes.py 56125488.json                        # a single route
python make_notes.py 56125488.json --mode dependent
```

Highlights: put a sidecar file `<id>.pois.json` next to a route (same name, .pois.json)
and its POIs are woven into the directions automatically. That sidecar is the
hand-off point a future automated highlights feed would write.

See STATUS.md for what's done, in progress, and parked.
