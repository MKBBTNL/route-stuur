#!/usr/bin/env python3
"""
rwgps_pull.py — batch-fetch RideWithGPS routes to a local cache, with filters.

The pipeline is "pull once, process forever offline": this is the only online
step. It walks your route library (or one collection, or an explicit id list),
keeps the routes that match your filters, and saves each one's raw JSON to a
folder. Downstream tools (edit / translate / format) then run entirely off that
folder — no more API calls, no rate-limit risk, instant re-runs.

Re-running is safe: routes already cached are skipped (use --refresh to redo),
so a big pull that dies partway just resumes.

Needs the same credentials as rwgps.py (RWGPS_API_KEY / RWGPS_AUTH_TOKEN) and
must sit in the same folder (it imports rwgps.py for the tested API + auth).

FILTERS
  Source (pick one; default = your whole library):
    --collection ID        only routes in that RWGPS collection (i.e. one tour)
    --ids-file FILE        explicit route ids, one per line
  Server-side (fast, applied by RWGPS):
    --name TEXT            route name contains TEXT
    --distance-min KM  --distance-max KM
    --climb-min M      --climb-max M
  Client-side (applied to the route summaries):
    --country CC           e.g. nl
    --region TEXT          matches locality or province (substring)
    --updated-since DATE   YYYY-MM-DD  (great for "active this season")
    --created-since DATE   YYYY-MM-DD
    --terrain / --difficulty / --track-type TEXT
    --include-archived     keep archived routes (default: skip them)
    --archived-only        keep ONLY archived routes

USAGE
  python3 rwgps_pull.py --dry-run --updated-since 2026-01-01        # preview matches
  python3 rwgps_pull.py --collection 123456                         # one tour -> cache
  python3 rwgps_pull.py --name "Hoorn" --distance-max 60 --out route_cache
  python3 rwgps_pull.py --ids-file active_tours.txt
"""

import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rwgps

# ---------- pure, testable filtering ----------
def server_params(args):
    p = {}
    if args.name:              p["filter_name"] = args.name
    if args.distance_min is not None: p["distance_min"] = int(args.distance_min * 1000)
    if args.distance_max is not None: p["distance_max"] = int(args.distance_max * 1000)
    if args.climb_min is not None:    p["elevation_gain_min"] = args.climb_min
    if args.climb_max is not None:    p["elevation_gain_max"] = args.climb_max
    return p

def keep(summary, args):
    s = summary
    archived = s.get("archived") is True
    if args.archived_only and not archived: return False
    if not args.include_archived and not args.archived_only and archived: return False
    if args.country and (s.get("country_code") or "").lower() != args.country.lower():
        return False
    if args.region:
        hay = f"{s.get('locality') or ''} {s.get('administrative_area') or ''}".lower()
        if args.region.lower() not in hay: return False
    if args.updated_since and (s.get("updated_at") or "")[:10] < args.updated_since:
        return False
    if args.created_since and (s.get("created_at") or "")[:10] < args.created_since:
        return False
    for attr, key in (("terrain","terrain"),("difficulty","difficulty"),("track_type","track_type")):
        want = getattr(args, attr)
        if want and (s.get(key) or "").lower() != want.lower(): return False
    return True

def select(summaries, args):
    return [s for s in summaries if keep(s, args)]

# ---------- online gathering (built to the tested API; run at home) ----------
def gather_summaries(args):
    if args.ids_file:
        ids = [ln.strip() for ln in open(args.ids_file) if ln.strip() and not ln.startswith("#")]
        # ids given explicitly -> minimal summaries, filters skipped
        return [{"id": int(i)} for i in ids], True
    if args.collection:
        cj = rwgps.api_get(f"collections/{args.collection}", args)
        coll = cj.get("collection", cj)
        return coll.get("routes", []), False
    # whole library, paginated
    out, page, size = [], 1, 100
    sp = server_params(args)
    while True:
        js = rwgps.api_get("routes", args, {**sp, "page": page, "page_size": size})
        batch = js.get("routes", [])
        out.extend(batch)
        if len(batch) < size: break
        page += 1
    return out, False

def main():
    ap = argparse.ArgumentParser(description="Batch-fetch RWGPS routes to a local cache, with filters.")
    ap.add_argument("--out", default="route_cache", help="cache folder (default route_cache)")
    ap.add_argument("--dry-run", action="store_true", help="list matches, download nothing")
    ap.add_argument("--refresh", action="store_true", help="re-fetch routes already cached")
    ap.add_argument("--limit", type=int, help="stop after this many NEW downloads (good for a first trial run)")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between detail fetches (be polite)")
    # source
    ap.add_argument("--collection"); ap.add_argument("--ids-file")
    # server-side
    ap.add_argument("--name")
    ap.add_argument("--distance-min", type=float); ap.add_argument("--distance-max", type=float)
    ap.add_argument("--climb-min", type=float);    ap.add_argument("--climb-max", type=float)
    # client-side
    ap.add_argument("--country"); ap.add_argument("--region")
    ap.add_argument("--updated-since"); ap.add_argument("--created-since")
    ap.add_argument("--terrain"); ap.add_argument("--difficulty"); ap.add_argument("--track-type")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--archived-only", action="store_true")
    # creds passthrough
    ap.add_argument("--apikey"); ap.add_argument("--auth-token", dest="auth_token")
    args = ap.parse_args()

    summaries, explicit = gather_summaries(args)
    kept = summaries if explicit else select(summaries, args)
    print(f"Matched {len(kept)} of {len(summaries)} routes.")
    if args.limit:
        print(f"(--limit {args.limit}: will stop after {args.limit} new download(s).)")

    if args.dry_run:
        for s in kept[:200]:
            km = (s.get("distance") or 0) / 1000
            print(f"  {s.get('id'):>10}  {km:6.1f} km  {s.get('name','(name in detail)')}")
        if len(kept) > 200: print(f"  … and {len(kept)-200} more")
        print("\n[dry run — nothing downloaded]")
        return

    os.makedirs(args.out, exist_ok=True)
    index_path = os.path.join(args.out, "index.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    fetched = skipped = failed = 0
    for i, s in enumerate(kept, 1):
        rid = s["id"]; path = os.path.join(args.out, f"{rid}.json")
        if os.path.exists(path) and not args.refresh:
            skipped += 1; continue
        try:
            rj = rwgps.get_route(rid, args)
        except SystemExit:                      # get_route exits on hard errors; keep the batch alive
            failed += 1; print(f"  ! {rid} failed"); continue
        json.dump(rj, open(path, "w"))
        r = rj.get("route", rj)
        index[str(rid)] = {"name": r.get("name", ""), "distance_m": r.get("distance"),
                           "file": f"{rid}.json"}
        fetched += 1
        if fetched % 25 == 0: print(f"  … {fetched} fetched ({i}/{len(kept)})")
        if args.limit and fetched >= args.limit:
            print(f"  reached --limit {args.limit}; stopping (re-run without --limit for the rest).")
            break
        time.sleep(args.sleep)
    json.dump(index, open(index_path, "w"), indent=2)
    print(f"\nDone. fetched {fetched}, skipped {skipped} (already cached), failed {failed}.")
    print(f"Cache: {args.out}/  ({len(index)} routes total, index.json written)")

if __name__ == "__main__":
    main()
