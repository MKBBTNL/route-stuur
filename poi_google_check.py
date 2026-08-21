#!/usr/bin/env python3
"""
poi_google_check.py - a second, independent corroboration signal alongside
poi_osm_check.py's OSM/Overpass check, using Google's Places API (New) --
its `businessStatus` field (OPERATIONAL / CLOSED_TEMPORARILY /
CLOSED_PERMANENTLY) is Google's own equivalent of OSM's disused:* tags.

WHY A SECOND SOURCE: OSM coverage is patchy in places (rural stretches,
smaller villages) and its disused:* tagging depends on a mapper having
actually visited and tagged the closure. Google's business data comes from
a different pipeline (crawled web content, licensed data, user edits/
reviews, Business Profile claims -- see their own support docs; they don't
publish a detailed methodology beyond that). Two independent, imperfect
signals agreeing is a much stronger "this is probably really gone" signal
than either alone -- see poi_verify.py's combined `stale` worklist, which
now sorts POIs flagged by EITHER source to the top.

FOLDED INTO THE MASTER SCHEMA THE SAME WAY osm_check IS: `google_check` is
a SIBLING field to `verify` and `osm_check` on each POI, not nested inside
either -- a bad or partial Google run can never overwrite a human's actual
verification, or the OSM signal, or vice versa.

REAL MONEY, UNLIKE OVERPASS: Places API (New) Nearby Search is a paid,
metered API -- check current pricing at
https://mapsplatform.google.com/pricing/ before running this at any real
scale. Unlike Overpass (one query can batch many points), Nearby Search
needs one HTTP call PER POI, so cost scales directly with how many POIs
you check. Defaults here are deliberately conservative: --limit 20 unless
you pass --all explicitly, and a printed cost estimate (rough -- verify
against your own account's actual pricing tier) before any calls are made.

SETUP -- you need your own Google Cloud project with the "Places API (New)"
enabled and billing turned on, then an API key. Put it in a small env file
next to rwgps_env.bat's pattern, e.g. google_env.bat:

    set GOOGLE_PLACES_API_KEY=your-key-here

then `call google_env.bat` before running this, or set the env var however
you prefer.

FUZZY NAME MATCH, NOT A GUARANTEE: like poi_osm_check.py's cluster-name
heuristic, matching a Google place to a master POI is done by proximity +
name similarity (same difflib approach poi_curate.py's dedupe uses) --
it's advisory, not proof. A POI with no confident match gets a neutral
note, not a "gone" flag -- absence of a Google match doesn't mean the
place doesn't exist.

    call google_env.bat
    python poi_google_check.py master_deduped.categorized.json --limit 20 --out master_test_google.json
    python poi_google_check.py master_deduped.categorized.json --categories route_critical,safety,commercial --all
    python poi_google_check.py master_deduped.categorized.json --resume   # fill in gaps from a failed run

Stdlib only (urllib + difflib -- same ethos as the rest of the toolset).
"""
import argparse, difflib, json, os, re, sys, time, unicodedata, urllib.request, urllib.error
from datetime import date

PLACES_API_URL = "https://places.googleapis.com/v1/places:searchNearby"
DEFAULT_CATEGORIES = "route_critical,safety,commercial"  # same default scope as poi_osm_check.py --
                                                          # lowest-churn categories skipped by default
FIELD_MASK = "places.id,places.displayName,places.businessStatus,places.location,places.types"

# rough, illustrative only -- ALWAYS check https://mapsplatform.google.com/pricing/
# for your account's real current rate before trusting this number.
EST_COST_PER_1000_USD = 32.0


def norm_name(s):
    """Same normalisation poi_curate.py's dedupe uses, so similarity scores
    mean the same thing across both scripts."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def poi_name(p):
    n = p.get("name") or {}
    return n.get("en") or n.get("nl") or n.get("de") or ""


def load_master(path):
    master = json.load(open(path, encoding="utf-8"))
    by_id = {p["id"]: p for p in master if "id" in p}
    return master, by_id


def save_master(path, master):
    json.dump(master, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def search_nearby(api_key, lat, lon, radius):
    body = json.dumps({
        "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": radius}},
        "maxResultCount": 10,
    }).encode()
    req = urllib.request.Request(
        PLACES_API_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def best_match(name, places):
    """Proximity already narrowed the field (searchNearby's radius); this
    picks the best NAME match among what came back, same threshold-and-ratio
    approach as poi_curate.py's dedupe. Returns (place, similarity) or
    (None, 0.0) if nothing clears the bar."""
    target = norm_name(name)
    if not target:
        return None, 0.0
    best, best_ratio = None, 0.0
    for pl in places:
        cand = (pl.get("displayName") or {}).get("text", "")
        ratio = difflib.SequenceMatcher(None, target, norm_name(cand)).ratio()
        if ratio > best_ratio:
            best, best_ratio = pl, ratio
    return best, best_ratio


def cmd_check(a):
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        sys.exit("GOOGLE_PLACES_API_KEY not set -- see this script's docstring for setup "
                 "(needs a Google Cloud project with Places API (New) + billing enabled).")

    master, by_id = load_master(a.master)
    cats = set(c.strip() for c in a.categories.split(",") if c.strip())
    scope = [p for p in master
             if p.get("lat") is not None and p.get("lon") is not None
             and (not cats or p.get("category") in cats)
             and (p.get("verify", {}) or {}).get("status") != "retired"]
    print(f"{len(scope)}/{len(master)} POIs in scope (categories: {sorted(cats) or 'all'})")

    if a.resume:
        before = len(scope)
        scope = [p for p in scope if not (p.get("google_check") or {}).get("checked_on")]
        print(f"--resume: {before - len(scope)} already have a google_check, {len(scope)} left to fetch")

    if not a.all:
        limit = a.limit or 20
        if len(scope) > limit:
            print(f"--limit {limit} (default -- pass --all for the full {len(scope)}-POI scope, "
                  f"once you've checked a quick sample and are happy with the cost/quality tradeoff)")
            scope = scope[:limit]

    if not scope:
        print("nothing to check.")
        return

    est = len(scope) / 1000.0 * EST_COST_PER_1000_USD
    print(f"about to make {len(scope)} Places API request(s) -- rough estimate ${est:.2f} "
          f"(verify against https://mapsplatform.google.com/pricing/ -- this number is illustrative only)")
    if not a.yes:
        resp = input("proceed? [y/N] ").strip().lower()
        if resp != "y":
            print("cancelled -- nothing checked, nothing written.")
            return

    out_path = a.out or a.master
    today = date.today().isoformat()
    checked_this_run = 0
    for i, p in enumerate(scope):
        try:
            js = search_nearby(api_key, p["lat"], p["lon"], a.radius)
        except urllib.error.HTTPError as e:
            print(f"  [{i+1}/{len(scope)}] {p['id']}: HTTP {e.code} -- skipped ({e.reason})")
            continue
        except Exception as e:
            print(f"  [{i+1}/{len(scope)}] {p['id']}: request failed ({e}) -- skipped, try again later")
            continue
        places = js.get("places", [])
        match, ratio = best_match(poi_name(p), places)
        if match is None:
            p["google_check"] = {
                "checked_on": today, "matched": False, "business_status": None,
                "place_name": None, "similarity": 0.0,
                "note": "no confident name match nearby -- not proof it's gone, may just be unmapped/renamed",
            }
        else:
            status = match.get("businessStatus", "")
            note = ""
            if status == "CLOSED_PERMANENTLY":
                note = "Google marks the matched place permanently closed"
            elif status == "CLOSED_TEMPORARILY":
                note = "Google marks the matched place temporarily closed -- not necessarily gone for good"
            p["google_check"] = {
                "checked_on": today, "matched": True, "business_status": status,
                "place_name": (match.get("displayName") or {}).get("text", ""),
                "similarity": round(ratio, 2), "note": note,
            }
        checked_this_run += 1
        if (i + 1) % 10 == 0 or i + 1 == len(scope):
            save_master(out_path, master)  # save periodically, not just at the end
            print(f"  [{i+1}/{len(scope)}] checked, saved to {out_path}")
        if i + 1 < len(scope):
            time.sleep(a.pause)

    save_master(out_path, master)
    checked = [p.get("google_check") for p in master if p.get("google_check")]
    closed_perm = [g for g in checked if g.get("business_status") == "CLOSED_PERMANENTLY"]
    closed_temp = [g for g in checked if g.get("business_status") == "CLOSED_TEMPORARILY"]
    unmatched = [g for g in checked if not g.get("matched")]
    print(f"\nwrote {out_path}: {checked_this_run} POIs checked this run ({len(checked)} total have a google_check)")
    print(f"  marked permanently closed by Google (real signal -- check first): {len(closed_perm)}")
    print(f"  marked temporarily closed (heads-up, not necessarily gone): {len(closed_temp)}")
    print(f"  no confident match nearby (inconclusive, not a red flag by itself): {len(unmatched)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("master")
    ap.add_argument("--categories", default=DEFAULT_CATEGORIES,
                    help=f"comma-separated categories to check (default: {DEFAULT_CATEGORIES}); empty = all")
    ap.add_argument("--radius", type=float, default=40.0, help="metres around each POI to search (default 40)")
    ap.add_argument("--pause", type=float, default=0.2, help="seconds between requests (default 0.2)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap POIs checked this run (default 20 unless --all is given)")
    ap.add_argument("--all", action="store_true", help="check the full scope, ignoring the default --limit")
    ap.add_argument("--resume", action="store_true",
                    help="skip POIs that already have a google_check and only fetch the gaps")
    ap.add_argument("--out", default=None,
                    help="write to this file instead of overwriting --master in place (default: overwrite --master)")
    ap.add_argument("--yes", action="store_true", help="skip the cost-estimate confirmation prompt")
    a = ap.parse_args()
    cmd_check(a)


if __name__ == "__main__":
    main()
