#!/usr/bin/env python3
"""
gpx_to_knooppunten.py
Read the fietsknooppunten (cycle node-network) sequence from any GPX track by
snapping it to OpenStreetMap's node network (nodes tagged rcn_ref / network=rcn).

WHY THIS IS RELIABLE
  Each knooppunt is a fixed OSM point tagged rcn_ref=<number>. We keep only the
  nodes the track physically passes through (within --threshold metres) and order
  them along the track. Off-network stretches simply return no nodes (an honest
  gap) rather than a guess.

OFFLINE CACHE
  The first time you run online, the rcn_ref nodes for the route's area are saved
  to a local cache file (default: rcn_cache.json). After that, any route inside an
  already-fetched area runs with no internet at all — handy when a work network
  blocks overpass-api.de. Populate the cache once at home, then run --offline
  anywhere.

USAGE
  python3 gpx_to_knooppunten.py route.gpx
  python3 gpx_to_knooppunten.py route.gpx --threshold 25 --json out.json
  python3 gpx_to_knooppunten.py route.gpx --offline           # cache only, no network
  python3 gpx_to_knooppunten.py route.gpx --refresh           # force a fresh fetch
  python3 gpx_to_knooppunten.py route.gpx --cache mine.json    # custom cache file
  python3 gpx_to_knooppunten.py route.gpx --no-cache          # don't read/write cache

REQUIREMENTS
  Python 3.8+ (standard library only). Needs internet access to Overpass the first
  time an area is fetched; runs offline from cache thereafter.
"""

import argparse, json, math, os, sys
import urllib.request, urllib.parse, xml.etree.ElementTree as ET

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}
DEFAULT_CACHE = "rcn_cache.json"

# ---------- geometry helpers (local equirectangular projection; fine at NL scale) ----------
def to_xy(lat, lon, lat0):
    R = 6371000.0
    x = math.radians(lon) * R * math.cos(math.radians(lat0))
    y = math.radians(lat) * R
    return x, y

def seg_project(px, py, ax, ay, bx, by):
    """Return (perp_dist_m, t) where t in [0,1] is position of the foot on segment AB."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    fx, fy = ax + t * dx, ay + t * dy
    return math.hypot(px - fx, py - fy), t

# ---------- read GPX track ----------
def read_track(path):
    root = ET.parse(path).getroot()
    pts = [(float(t.get("lat")), float(t.get("lon")))
           for t in root.iter("{http://www.topografix.com/GPX/1/1}trkpt")]
    if len(pts) < 2:
        sys.exit("No track (<trkpt>) found in GPX.")
    lat0 = sum(p[0] for p in pts) / len(pts)
    xy = [to_xy(la, lo, lat0) for la, lo in pts]
    cum = [0.0]
    for i in range(1, len(xy)):
        cum.append(cum[-1] + math.hypot(xy[i][0] - xy[i-1][0], xy[i][1] - xy[i-1][1]))
    return pts, xy, cum, lat0

# ---------- Overpass fetch + offline cache ----------
def bbox_for(pts, pad=0.01):  # pad ~1 km
    lats = [p[0] for p in pts]; lons = [p[1] for p in pts]
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)

def bbox_contains(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])

def fetch_overpass(bbox):
    s, w, n, e = bbox
    q = f'[out:json][timeout:60];node["rcn_ref"]({s},{w},{n},{e});out;'
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"User-Agent": "knooppunten-reader/1.1"})
            with urllib.request.urlopen(req, timeout=90) as r:
                js = json.load(r)
            out = []
            for el in js.get("elements", []):
                ref = el.get("tags", {}).get("rcn_ref")
                if ref:
                    out.append([el["id"], ref, el["lat"], el["lon"]])
            return out
        except Exception as e:
            last = e
    raise RuntimeError(f"could not reach Overpass ({last})")

def load_cache(path):
    if path and os.path.exists(path):
        try:
            js = json.load(open(path, encoding="utf-8"))
            return {"bboxes": [tuple(b) for b in js.get("bboxes", [])],
                    "nodes": {int(k): v for k, v in js.get("nodes", {}).items()}}
        except Exception:
            pass  # unreadable cache is treated as empty rather than fatal
    return {"bboxes": [], "nodes": {}}

def save_cache(path, cache):
    # drop bboxes fully contained by another, then dedupe
    boxes = cache["bboxes"]
    kept = [b for i, b in enumerate(boxes)
            if not any(j != i and bbox_contains(o, b) for j, o in enumerate(boxes))]
    uniq = []
    for b in kept:
        if b not in uniq:
            uniq.append(b)
    json.dump({"bboxes": [list(b) for b in uniq],
               "nodes": {str(k): v for k, v in cache["nodes"].items()}},
              open(path, "w", encoding="utf-8"))

def get_nodes(pts, args):
    """Return (nodes_in_bbox, source_label). nodes are [id, ref, lat, lon]."""
    bbox = bbox_for(pts)
    cache = load_cache(args.cache) if args.cache else {"bboxes": [], "nodes": {}}
    covered = any(bbox_contains(b, bbox) for b in cache["bboxes"])

    if args.offline or (covered and not args.refresh):
        if not covered:
            where = f" ({args.cache})" if args.cache else ""
            sys.exit("Offline: this area is not in the cache yet" + where + ".\n"
                     "Run once on an internet connection (without --offline) to populate\n"
                     "the cache, then re-run offline.")
        nodes = [[nid, ref, lat, lon]
                 for nid, (ref, lat, lon) in cache["nodes"].items()
                 if bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]]
        return nodes, f"cache ({args.cache})"

    # online path
    try:
        fetched = fetch_overpass(bbox)
    except RuntimeError as e:
        hint = ("A cache exists but doesn't cover this area. "
                if cache["nodes"] else
                "Node data is public — run once on home wifi / a phone hotspot. ")
        sys.exit(f"Could not reach Overpass (network blocked?): {e}\n{hint}"
                 "Distance and any offline steps still work.")
    if args.cache:
        for nid, ref, lat, lon in fetched:
            cache["nodes"][nid] = [ref, lat, lon]
        cache["bboxes"].append(bbox)
        save_cache(args.cache, cache)
        return fetched, f"Overpass (cached to {args.cache})"
    return fetched, "Overpass"

# ---------- snap nodes to the track ----------
def snap(nodes, xy, cum, lat0, threshold):
    hits = []
    for nid, ref, nlat, nlon in nodes:
        nx, ny = to_xy(nlat, nlon, lat0)
        best_d, best_along = 1e18, 0.0
        for i in range(len(xy) - 1):
            d, t = seg_project(nx, ny, xy[i][0], xy[i][1], xy[i+1][0], xy[i+1][1])
            if d < best_d:
                best_d = d
                best_along = cum[i] + t * (cum[i+1] - cum[i])
        if best_d <= threshold:
            hits.append({"kp": ref, "km": round(best_along/1000, 1),
                         "offset_m": round(best_d, 1)})
    hits.sort(key=lambda h: h["km"])
    seq = []
    for h in hits:  # drop consecutive duplicates (a node touched twice in a row)
        if not seq or seq[-1]["kp"] != h["kp"]:
            seq.append(h)
    return seq

def main():
    ap = argparse.ArgumentParser(description="Read knooppunten sequence from a GPX track via OSM.")
    ap.add_argument("gpx")
    ap.add_argument("--threshold", type=float, default=30.0,
                    help="max metres between node and track to count as 'on route' (default 30)")
    ap.add_argument("--json", help="also write the result to this JSON file")
    ap.add_argument("--cache", default=DEFAULT_CACHE,
                    help=f"node cache file (default {DEFAULT_CACHE})")
    ap.add_argument("--no-cache", action="store_true", help="do not read or write the node cache")
    ap.add_argument("--offline", action="store_true", help="use only cached nodes; never contact Overpass")
    ap.add_argument("--refresh", action="store_true", help="force a fresh Overpass fetch and update the cache")
    args = ap.parse_args()
    if args.no_cache:
        args.cache = None

    pts, xy, cum, lat0 = read_track(args.gpx)
    total_km = cum[-1] / 1000
    nodes, source = get_nodes(pts, args)
    seq = snap(nodes, xy, cum, lat0, args.threshold)

    print(f"\nTrack: {args.gpx}")
    print(f"Distance: {total_km:.1f} km   |   knooppunten on route: {len(seq)}   |   POI waypoints: 0")
    print(f"Nodes in area: {len(nodes)}   |   source: {source}\n")
    if not seq:
        print("No knooppunten on this track — likely off-network or no node network here.")
    else:
        print("KP @ km   (offset = distance from track; >18 m worth a glance):")
        for h in seq:
            flag = "  <-- verify (node sits a bit off the line)" if h["offset_m"] > 18 else ""
            print(f"  {h['kp']:>3}  @ {h['km']:>5.1f} km   ({h['offset_m']:>3.0f} m){flag}")
        print("\nSequence: " + " · ".join(h["kp"] for h in seq))

    if args.json:
        json.dump({"gpx": args.gpx, "distance_km": round(total_km, 1),
                   "knooppunten": seq}, open(args.json, "w", encoding="utf-8"), indent=2)
        print(f"\nWrote {args.json}")

if __name__ == "__main__":
    main()
