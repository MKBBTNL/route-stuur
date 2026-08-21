#!/usr/bin/env python3
"""
poi_osm_check.py - let OSM corroborate POIs already in the master, not just
supply new candidates (harvest_osm_pois.py already does that half). For each
POI in scope, asks Overpass "is anything at all still tagged here" and
"does anything nearby carry a disused:* tag" -- a free, automatic signal to
help prioritise poi_verify.py's human-check worklist, using the same
check_date / disused:* conventions OSM mappers already use (see the OSM
wiki: Key:check_date, Key:disused:).

RESTRUCTURE UPDATE -- folded into the master schema: results now live as
p["osm_check"] directly on each POI in the master file -- {checked_on,
found, disused_nearby, cluster_name, nearest_m, note} -- instead of a
separate osm_corroboration.json sidecar. This is safe to fold in because
it's a SIBLING field to `verify`, not nested inside it: a bad OSM run can
still never touch a human's actual verification, it can only overwrite its
own osm_check field. --resume now checks each POI's own
osm_check.checked_on instead of a separate file.

Already have an old osm_corroboration.json from before this change? Merge
it into the master once, with no Overpass calls at all, then retire the
sidecar file:

    python poi_osm_check.py master_deduped.categorized.json --import-sidecar osm_corroboration.json

Known gaps, left as the natural next steps rather than guessed at now:
  - doesn't match by *type* yet -- it only checks "is anything tagged here",
    so a cafe replaced by a bike shop would still show found=True. The fix
    is reusing poi_curate.py's CATEGORY_MAP / harvest_osm_pois.py's TAGS to
    require a matching tag, not just presence.
  - radius/batch-size are starting guesses; tune once you've run it a few
    times against real areas.

Needs internet (Overpass) -- run this locally, same as harvest_osm_pois.py;
the sandbox that wrote this script can't reach Overpass to test it live.
The public Overpass instances rate-limit (429) and time out (504) under
load -- a real 2,521-POI run hit both. This tries two mirrors with backoff
per batch, saves progress after every batch (not just at the end), and
--resume skips whatever's already checked so a re-run only fetches the
gaps instead of redoing everything.

    python poi_osm_check.py master_deduped.categorized.json
    python poi_osm_check.py master_deduped.categorized.json --categories route_critical,safety,commercial --radius 40
    python poi_osm_check.py master_deduped.categorized.json --resume   # fill in gaps from a failed run
    python poi_osm_check.py master_deduped.categorized.json --out master_test.json   # write elsewhere, don't touch the real master

Attribution: data (c) OpenStreetMap contributors, ODbL -- credit it on guest docs.
Stdlib only.
"""
import argparse, json, math, os, time, urllib.request, urllib.parse
from datetime import date

# public Overpass instances -- your first full run hit a lot of 429 (rate
# limited) and 504 (gateway timeout) on the single default endpoint. Same
# mirror list gpx_to_knooppunten.py already uses.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
DEFAULT_CATEGORIES = "route_critical,safety,commercial"  # skip infrastructure/informational by
                                                          # default -- lowest churn, least worth the Overpass load

# real run (2,521 POIs) showed disused_nearby is trustworthy for a single named
# business ("Cafe Bad Breisig") but noisy for a POI that stands for several
# businesses at once ("Mehrere Cafes Koudum", "Restaurants IJlst", "Supermarket,
# Bakery, Coffee") -- one of several closing is normal churn, not "this POI is
# gone". Heuristic, not exhaustive -- restructure/extend freely.
import re as _re
CLUSTER_LANG_WORDS = ("mehrere", "multiple", "several", "diverse", "various", "verschillende")
CLUSTER_BIZ_WORDS = ("cafe", "café", "restaurant", "bar", "shop", "hotel",
                     "supermarket", "bakery", "winery", "store", "coffee")

def looks_like_cluster(name):
    n = (name or "").lower()
    if any(w in n for w in CLUSTER_LANG_WORDS):
        return True
    parts = _re.split(r"[,&]| and ", n)
    if sum(1 for part in parts if any(w in part for w in CLUSTER_BIZ_WORDS)) >= 2:
        return True
    return any(_re.search(rf"\b{w}s\b", n) for w in CLUSTER_BIZ_WORDS)


def hav(a, b):
    R = 6371000.0
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def build_query(points, radius):
    body = "".join(f'node(around:{radius},{la:.6f},{lo:.6f});\n' for la, lo in points)
    return f"[out:json][timeout:120];(\n{body});out body;"


def fetch(query, retries=2, backoff=5.0):
    """Try every mirror; on failure (rate limit, timeout, ...) back off and
    retry the round. Raises only after all mirrors have failed `retries`
    rounds in a row."""
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for attempt in range(retries + 1):
        for url in OVERPASS_MIRRORS:
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "poi-osm-check/0.3"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.load(r)
            except Exception as e:
                last = e
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))  # 5s, then 10s
    raise RuntimeError(f"all {len(OVERPASS_MIRRORS)} mirrors failed after {retries + 1} attempts ({last})")


def load_master(path):
    master = json.load(open(path, encoding="utf-8"))
    by_id = {p["id"]: p for p in master if "id" in p}
    return master, by_id


def save_master(path, master):
    json.dump(master, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def cmd_import_sidecar(a):
    """One-time merge of an old osm_corroboration.json sidecar into the
    master's osm_check field -- no Overpass calls, just reshaping data
    that's already been fetched.

    Also RECONCILES the cluster-name filter: every real sidecar this was
    tested against (2,521/2,521 entries) predates looks_like_cluster()
    entirely -- none carry a cluster_name key, meaning their disused_nearby
    flags were never actually suppressed for multi-business names, even
    though that suppression was analysed separately. This recomputes it
    here using each POI's real name from the master, so the fold-in is a
    genuine correction, not just a reshaping of stale data."""
    master, by_id = load_master(a.master)
    sidecar = json.load(open(a.import_sidecar, encoding="utf-8"))
    merged, missing, reconciled = 0, 0, 0
    for pid, result in sidecar.items():
        p = by_id.get(pid)
        if p is None:
            missing += 1
            continue
        result = dict(result)
        if "cluster_name" not in result:
            name = (p.get("name") or {}).get("en") or (p.get("name") or {}).get("nl") or (p.get("name") or {}).get("de") or ""
            cluster = looks_like_cluster(name)
            was_disused = bool(result.get("disused_nearby"))
            result["cluster_name"] = cluster
            if was_disused and cluster:
                result["disused_nearby"] = False
                result["note"] = ("disused tag found nearby but suppressed on import -- name looks like a "
                                  "multi-business cluster, one closing there is normal churn")
                reconciled += 1
        p["osm_check"] = result
        merged += 1
    out_path = a.out or a.master
    save_master(out_path, master)
    print(f"imported {a.import_sidecar}: {merged}/{len(sidecar)} entries merged into {out_path}'s osm_check field")
    if reconciled:
        print(f"  {reconciled} disused_nearby flag(s) reconciled against the current cluster-name filter "
              f"(this sidecar predated it entirely) -- see osm_check.note on each")
    if missing:
        print(f"  {missing} sidecar id(s) not found in the master (likely deduped away since the sidecar was written)")
    print(f"the sidecar file itself is untouched -- safe to keep as a backup or delete once you've checked this worked.")


def cmd_check(a):
    master, by_id = load_master(a.master)
    cats = set(c.strip() for c in a.categories.split(",") if c.strip())
    scope = [p for p in master
             if p.get("lat") is not None and p.get("lon") is not None
             and (not cats or p.get("category") in cats)
             and (p.get("verify", {}) or {}).get("status") != "retired"]
    print(f"{len(scope)}/{len(master)} POIs in scope (categories: {sorted(cats) or 'all'})")

    if a.limit and len(scope) > a.limit:
        # quick-look sample, spread evenly across categories rather than just
        # the first N (which would likely be dominated by one source route) --
        # for seeing the *shape* of results fast, before running the full scope.
        by_cat = {}
        for p in scope:
            by_cat.setdefault(p.get("category"), []).append(p)
        per_cat = max(1, a.limit // max(1, len(by_cat)))
        sampled = [p for items in by_cat.values() for p in items[:per_cat]][:a.limit]
        print(f"--limit {a.limit}: sampling {len(sampled)} POIs across "
              f"{len(by_cat)} categories for a quick first look (nothing else touched)")
        scope = sampled

    if a.resume:
        before = len(scope)
        scope = [p for p in scope if not (p.get("osm_check") or {}).get("checked_on")]
        already = before - len(scope)
        print(f"--resume: {already} already have an osm_check in the master, "
              f"{already} skipped, {len(scope)} left to fetch")
        if not scope:
            print("nothing left to fetch -- done.")
            return

    out_path = a.out or a.master
    checked_this_run = 0
    today = date.today().isoformat()
    for i in range(0, len(scope), a.batch_size):
        batch = scope[i:i + a.batch_size]
        points = [(p["lat"], p["lon"]) for p in batch]
        q = build_query(points, a.radius)
        try:
            js = fetch(q)
        except Exception as e:
            print(f"  batch {i // a.batch_size}: Overpass fetch failed ({e}) -- skipped, try again later")
            continue
        elements = [e for e in js.get("elements", []) if e.get("type") == "node" and e.get("lat") is not None]
        batch_found = 0
        for p in batch:
            near = [e for e in elements if hav((p["lat"], p["lon"]), (e["lat"], e["lon"])) <= a.radius]
            disused_raw = any(any(k.startswith("disused:") for k in e.get("tags", {})) for e in near)
            name = (p.get("name") or {}).get("en") or (p.get("name") or {}).get("nl") or (p.get("name") or {}).get("de") or ""
            cluster = looks_like_cluster(name)
            note = ""
            if not near:
                note = "no OSM node within radius -- not proof it's gone, may just be unmapped"
            elif disused_raw and cluster:
                note = "disused tag found nearby but suppressed -- name looks like a multi-business " \
                       "cluster, one closing there is normal churn"
            p["osm_check"] = {
                "checked_on": today,
                "found": bool(near),
                "disused_nearby": disused_raw and not cluster,
                "cluster_name": cluster,
                "nearest_m": round(min((hav((p["lat"], p["lon"]), (e["lat"], e["lon"])) for e in near), default=-1)),
                "note": note,
            }
            batch_found += bool(near)
            checked_this_run += 1
        print(f"  batch {i // a.batch_size + 1}/{-(-len(scope) // a.batch_size)}: "
              f"{batch_found}/{len(batch)} corroborated")
        save_master(out_path, master)  # save after every batch -- a crash or Ctrl+C loses nothing
        if i + a.batch_size < len(scope):
            time.sleep(a.pause)

    save_master(out_path, master)
    checked = [p.get("osm_check") for p in master if p.get("osm_check")]
    flagged = [o for o in checked if o.get("disused_nearby")]
    suppressed = [o for o in checked if o.get("cluster_name") and "suppressed" in (o.get("note") or "")]
    unmatched = [o for o in checked if not o.get("found")]
    print(f"\nwrote {out_path}: {checked_this_run} POIs checked this run ({len(checked)} total have an osm_check)")
    print(f"  disused:* nearby, single-named (real signal -- check first): {len(flagged)}")
    print(f"  disused:* nearby but suppressed (cluster-named, unreliable): {len(suppressed)}")
    print(f"  no OSM match at all (inconclusive, not a red flag by itself): {len(unmatched)}")
    print("Attribution: data (c) OpenStreetMap contributors (ODbL) -- credit on guest docs.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("master")
    ap.add_argument("--categories", default=DEFAULT_CATEGORIES,
                    help=f"comma-separated categories to check (default: {DEFAULT_CATEGORIES}); empty = all")
    ap.add_argument("--radius", type=float, default=40.0, help="metres around each POI to query (default 40)")
    ap.add_argument("--batch-size", type=int, default=60, help="POIs per Overpass call (default 60)")
    ap.add_argument("--pause", type=float, default=3.0,
                    help="seconds between batches (default 3 -- bumped up after a real run hit "
                         "429/504 at 2s; --retries/mirrors also help absorb these)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap total POIs checked, sampled evenly across categories -- for a quick first "
                         "look at what the results look like before running the full scope")
    ap.add_argument("--resume", action="store_true",
                    help="skip POIs that already have an osm_check in the master and only fetch "
                         "the gaps (e.g. batches that failed with 429/504 last time)")
    ap.add_argument("--out", default=None,
                    help="write to this file instead of overwriting --master in place (default: overwrite --master)")
    ap.add_argument("--import-sidecar", default=None,
                    help="merge an old osm_corroboration.json sidecar's results into the master's "
                         "osm_check field -- no Overpass calls, just a one-time schema migration")
    a = ap.parse_args()
    if a.import_sidecar:
        cmd_import_sidecar(a)
    else:
        cmd_check(a)


if __name__ == "__main__":
    main()
