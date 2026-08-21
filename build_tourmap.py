#!/usr/bin/env python3
"""
build_tourmap.py — build a map-ready JSON list of tour routes.

Inputs
  --tours  tours_2026.json          Existing tour/program/day route list. When
                                     supplied, --csv is not required.
  --csv    tourprogram_routes.csv   Alternative Salesforce tour-program export
                                     (day rows -> route options).
  --index  index.json               RWGPS route index: { "<id>": {name, distance_m, file} }.
                                     Defaults to <cache>/index.json when --cache
                                     is supplied.
  --cache  DIR                       (optional) folder of cached <id>.json route files.
                                     When given, lat/lon/bbox are filled from each route's track.
  --year   2026                      season filter (matches rows whose Season_Year contains it)
  --published-only                   keep only Publish_Website == true rows
  --type   route                     value written to the "type" field on every entry
  --out    tours_<year>.json

Output: a flat JSON array, one object per plottable route option:
  { id, type, tour, program, day, option, name, distance_km, season,
    published, lat, lon, bbox, source_file }

Why a flat array: it drops straight into a map layer (one marker per id).
Group client-side by "tour" if you want per-tour clustering.

Notes on the CSV: it is a corrupted Salesforce export (stray quotes, and commas
inside tour/day free-text). This parser tolerates all of that by (a) stripping
quotes, (b) anchoring on the true/false Publish token, and (c) extracting route
IDs by SHAPE (5+ digit runs, pipe-composites) rather than by column position,
so commas in free-text fields cannot misalign a route ID into a distance slot.
"""
import argparse, json, re, sys
from pathlib import Path

ROUTE_ID = re.compile(r'\d{5,}')          # RWGPS ids are 8 digits; distances are <=3
YEAR      = re.compile(r'20\d\d')


def parse_csv(path):
    raw = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").rstrip("\n").split("\n")
    out = []
    for ln in raw[1:]:
        if not ln.strip():
            continue
        toks = ln.replace('"', '').split(",")
        pub = [i for i, t in enumerate(toks) if t.strip() in ("true", "false")]
        if not pub or pub[0] < 3:
            continue
        p = pub[0]
        tour    = ",".join(toks[1:p-2]).strip()
        program = toks[p-2].strip()
        season  = ";".join(dict.fromkeys(YEAR.findall(toks[p-1])))
        published = toks[p].strip() == "true"
        tail = toks[p+1:]
        day  = tail[0].strip() if tail else ""
        # route IDs = shape-matched tokens after publish, in order. First two are
        # option 1 / option 2. Pipe-composites keep their segments joined.
        route_opts = []
        for t in tail:
            t = t.strip()
            if not t:
                continue
            # a token is a route field if it is all digits+pipes and has a 5+ digit part
            if re.fullmatch(r'[\d|]+', t) and ROUTE_ID.search(t):
                route_opts.append(t)
        out.append(dict(sf_id=toks[0].strip(), tour=tour, program=program,
                        season=season, published=published, day=day,
                        route_opts=route_opts[:2]))
    return out


def parse_tours_json(path):
    """Read the flat tours_<year>.json produced by this script.

    It can therefore be reused as the tour/program/day selection source while
    the RWGPS index and cache provide fresh metadata and coordinates.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        # Be tolerant of a future wrapped format without weakening validation.
        data = data.get("entries", data.get("routes"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array of route entries")

    required = ("id", "tour", "program", "day")
    out = []
    for n, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: entry {n} is not a JSON object")
        missing = [key for key in required if key not in entry]
        if missing:
            raise ValueError(f"{path}: entry {n} is missing {', '.join(missing)}")
        out.append(entry)
    return out


def resolve_cached_file(cache_dir, rid, source_file=None):
    """Resolve a route file from its index filename, then fall back to <id>.json."""
    root = Path(cache_dir)
    candidates = []
    if source_file:
        source = Path(str(source_file))
        candidates.append(source if source.is_absolute() else root / source)
    candidates.append(root / f"{rid}.json")

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen and candidate.is_file():
            return candidate
        seen.add(key)
    return None


def load_route_coords(cache_dir, rid, source_file=None):
    """Best-effort lat/lon/bbox from a cached RWGPS route file. Returns (lat,lon,bbox) or (None,None,None).
    Tries the common cached shapes; if none match, returns None so the caller can flag it."""
    fp = resolve_cached_file(cache_dir, rid, source_file)
    if fp is None:
        return None, None, None
    try:
        d = json.loads(fp.read_text(encoding="utf-8-sig"))
    except Exception:
        return None, None, None
    # unwrap a top-level {"route": {...}} if present
    r = d.get("route", d) if isinstance(d, dict) else d
    pts = None
    for key in ("track_points", "trackpoints", "points"):
        if isinstance(r, dict) and isinstance(r.get(key), list) and r[key]:
            pts = r[key]
            break
    lats, lons = [], []
    if pts:
        for pt in pts:
            if not isinstance(pt, dict):
                continue
            la = pt.get("y", pt.get("lat"))
            lo = pt.get("x", pt.get("lon", pt.get("lng")))
            if la is not None and lo is not None:
                lats.append(float(la)); lons.append(float(lo))
    if lats and lons:
        bbox = [min(lons), min(lats), max(lons), max(lats)]
        # marker = midpoint of the track (index-wise), a reasonable "planted" point
        mid = len(lats) // 2
        return lats[mid], lons[mid], bbox
    # fallback: an explicit bounding box on the route object
    for bk in ("bounding_box", "bbox"):
        b = r.get(bk) if isinstance(r, dict) else None
        if isinstance(b, dict) and {"ne", "sw"} <= set(b):
            ne, sw = b["ne"], b["sw"]
            try:
                bbox = [float(sw["lng"]), float(sw["lat"]), float(ne["lng"]), float(ne["lat"])]
                return (bbox[1]+bbox[3])/2, (bbox[0]+bbox[2])/2, bbox
            except Exception:
                pass
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--tours", help="existing tours_<year>.json selection file")
    source.add_argument("--csv", help="Salesforce tour-program CSV selection file")
    ap.add_argument("--index", default=None,
                    help="RWGPS index; defaults to <cache>/index.json")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--year", default="2026")
    ap.add_argument("--published-only", action="store_true")
    ap.add_argument("--type", default="route")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    index_path = Path(a.index) if a.index else (
        Path(a.cache) / "index.json" if a.cache else Path("index.json")
    )
    if not index_path.is_file():
        ap.error(f"RWGPS index not found: {index_path}")
    idx = json.loads(index_path.read_text(encoding="utf-8-sig"))
    if not isinstance(idx, dict):
        ap.error(f"RWGPS index must be a JSON object keyed by route ID: {index_path}")

    if a.tours:
        try:
            tour_entries = parse_tours_json(a.tours)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            ap.error(str(exc))
        rows = None
    else:
        csv_path = a.csv or "tourprogram_routes.csv"
        if not Path(csv_path).is_file():
            ap.error(
                f"tour selection file not found: {csv_path}. "
                "Use --tours tours_2026.json or --csv FILE."
            )
        tour_entries = None
        rows = parse_csv(csv_path)

    entries, missing_from_index, missing_coords = [], set(), set()
    if tour_entries is not None:
        candidates = tour_entries
    else:
        candidates = []
        for r in rows:
            for opt_n, rid_field in enumerate(r["route_opts"], start=1):
                for rid in rid_field.split("|"):    # explode pipe-composites
                    rid = rid.strip()
                    if rid:
                        candidates.append({
                            "id": rid,
                            "type": a.type,
                            "tour": r["tour"],
                            "program": r["program"],
                            "day": r["day"],
                            "option": opt_n,
                            "season": r["season"],
                            "published": r["published"],
                        })

    for original in candidates:
        season = str(original.get("season") or "")
        if a.year not in season.split(";"):
            continue
        published = original.get("published", False)
        if a.published_only and not published:
            continue

        rid = str(original["id"]).strip()
        meta = idx.get(rid)
        if meta is None:
            missing_from_index.add(rid)
            meta = {}

        name = meta.get("name") or original.get("name")
        distance_m = meta.get("distance_m")
        dist_km = (round(distance_m / 1000, 1) if distance_m is not None
                   else original.get("distance_km"))
        src = meta.get("file") or original.get("source_file") or f"{rid}.json"
        lat = lon = bbox = None
        if a.cache:
            lat, lon, bbox = load_route_coords(a.cache, rid, src)
            if lat is None:
                missing_coords.add(rid)

        entry = dict(original)
        entry.update({
            "id": int(rid) if rid.isdigit() else rid,
            "type": original.get("type", a.type),
            "name": name,
            "distance_km": dist_km,
            "season": season,
            "published": published,
            "lat": lat,
            "lon": lon,
            "bbox": bbox,
            "source_file": str(src),
        })
        entries.append(entry)

    out = a.out or (f"tourmap_{a.year}.json" if a.tours else f"tours_{a.year}.json")
    out_path = Path(out)
    if a.tours and out_path.resolve() == Path(a.tours).resolve():
        ap.error("--out must differ from --tours so the source file is not overwritten")
    out_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    tours = sorted({e["tour"] for e in entries})
    print(f"wrote {out_path}", file=sys.stderr)
    print(f"  entries (route options): {len(entries)}", file=sys.stderr)
    print(f"  distinct tours: {len(tours)}", file=sys.stderr)
    print(f"  distinct route ids: {len({e['id'] for e in entries})}", file=sys.stderr)
    print(f"  route ids not in index.json: {len(missing_from_index)}", file=sys.stderr)
    if a.cache:
        with_coords = sum(1 for e in entries if e['lat'] is not None)
        print(f"  entries with coordinates: {with_coords}/{len(entries)}", file=sys.stderr)
        print(f"  route ids with no coords found: {len(missing_coords)}", file=sys.stderr)
    else:
        print("  coordinates: NONE (run again with --cache DIR to fill lat/lon/bbox)", file=sys.stderr)


if __name__ == "__main__":
    main()
