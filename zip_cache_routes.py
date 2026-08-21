#!/usr/bin/env python3
"""
zip_cache_routes.py — pull just the route files you need out of the big cache.

Reads the route IDs out of tours_2026.json (or any JSON with a route_ids list,
or an explicit --ids list), finds each <id>.json anywhere under your cache
folder, grabs its <id>.pois.json sidecar if present, and zips the lot.

Upload the resulting zip and the route geometry (and POIs) can be read straight out.

Usage
  python zip_cache_routes.py --cache /path/to/cache
  python zip_cache_routes.py --cache ./cache --source tours_2026.json --out routes_2026.zip
  python zip_cache_routes.py --cache ./cache --ids 56125488 56125478 44712627

Foolproof notes
  - --cache can be flat or nested; the whole tree is searched.
  - Missing IDs are reported, not fatal — you still get a zip of what was found.
  - Nothing in the cache is moved or changed; files are only read and copied in.
"""
import argparse, json, sys, zipfile, re
from pathlib import Path


def ids_from_source(path):
    """Pull a flat set of route IDs from a tours JSON (either the flat array with
    'id'/'route_ids', or a GeoJSON FeatureCollection with properties.route_ids)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = set()
    def add(v):
        s = str(v).strip()
        for part in re.split(r"\|", s):        # explode any pipe-composites
            part = part.strip()
            if part.isdigit():
                ids.add(part)
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        for f in data.get("features", []):
            for rid in f.get("properties", {}).get("route_ids", []):
                add(rid)
    elif isinstance(data, list):
        for e in data:
            if isinstance(e, dict):
                if "route_ids" in e:
                    for rid in e["route_ids"]:
                        add(rid)
                elif "id" in e:
                    add(e["id"])
    return ids


def build_cache_index(cache_dir):
    """Map basename (e.g. '56125488.json') -> full path, for the whole tree."""
    idx = {}
    for p in Path(cache_dir).rglob("*"):
        if p.is_file():
            idx.setdefault(p.name, p)   # first hit wins if dupes exist
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True, help="root folder of the cached route files")
    ap.add_argument("--source", default="tours_2026.json",
                    help="JSON to read route IDs from (ignored if --ids given)")
    ap.add_argument("--ids", nargs="*", help="explicit list of route IDs (overrides --source)")
    ap.add_argument("--out", default="routes_2026.zip")
    a = ap.parse_args()

    cache = Path(a.cache)
    if not cache.is_dir():
        sys.exit(f"cache folder not found: {cache}")

    if a.ids:
        ids = {str(x).strip() for x in a.ids if str(x).strip().isdigit()}
    else:
        if not Path(a.source).exists():
            sys.exit(f"source not found: {a.source} (or pass --ids)")
        ids = ids_from_source(a.source)
    if not ids:
        sys.exit("no route IDs to look for")

    print(f"looking for {len(ids)} route IDs in {cache} ...", file=sys.stderr)
    index = build_cache_index(cache)

    found, missing, sidecars = [], [], 0
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for rid in sorted(ids):
            route_name = f"{rid}.json"
            src = index.get(route_name)
            if src is None:
                missing.append(rid)
                continue
            z.write(src, arcname=route_name)
            found.append(rid)
            sidecar = index.get(f"{rid}.pois.json")
            if sidecar is not None:
                z.write(sidecar, arcname=f"{rid}.pois.json")
                sidecars += 1

    print(f"wrote {a.out}", file=sys.stderr)
    print(f"  route files zipped: {len(found)}/{len(ids)}", file=sys.stderr)
    print(f"  POI sidecars included: {sidecars}", file=sys.stderr)
    if missing:
        print(f"  NOT found in cache ({len(missing)}): {', '.join(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
