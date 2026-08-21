#!/usr/bin/env python3
"""
poi_curate.py - prep steps for turning master_pois.json's backlog into
something checkable before season start: categorize by risk tier, prune POIs
whose only source route predates a cutoff year, and two data-quality passes
(near-duplicate spelling detection, standard database hygiene).

  categorize     tag every POI with a risk category derived from its `type`
                 (route_critical / commercial / infrastructure / informational /
                 safety / uncategorized). Offline, no route_cache needed.
                 Writes a NEW file -- never overwrites the master.

  resolve-dates  for every unique `source` route referenced in the master,
                 look up that route's date in route_cache/ (updated_at /
                 created_at field, falling back to a 4-digit year found in
                 the route's name) and cache it to a small sidecar. Needs
                 route_cache on disk -- run this where route_cache lives.

  status         counts by category, and by resolved source year if a
                 --dates sidecar (from resolve-dates) is given.

  prune          dry-run by default: shows how many POIs would be dropped
                 for having only a pre-cutoff source, and how many would be
                 kept because their source date couldn't be resolved (never
                 silently dropped). --apply actually writes the pruned file,
                 to --out, never touching the input master.

    python poi_curate.py categorize master_pois.json --out master_pois.categorized.json
    python poi_curate.py resolve-dates master_pois.json route_cache --out poi_source_dates.json
    python poi_curate.py status master_pois.json --dates poi_source_dates.json
    python poi_curate.py prune master_pois.json --dates poi_source_dates.json --before 2024 --out master_pruned.json
    python poi_curate.py prune master_pois.json --dates poi_source_dates.json --before 2024 --out master_pruned.json --apply
    python poi_curate.py dupes master_pois.json --out poi_dupes.csv
    python poi_curate.py dedupe master_pois.json --out master_deduped.json
    python poi_curate.py lint master_pois.json --out poi_lint.csv

Stdlib only.
"""
import argparse, csv, json, math, os, re, unicodedata
from collections import Counter


def hav(a, b):
    R = 6371000.0
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

# type (lowercased) -> risk category. Extend this as new types show up --
# `categorize` will list any it doesn't recognise so nothing is silently
# miscounted as "uncategorized" without you knowing about it.
CATEGORY_MAP = {
    "ferry": "route_critical", "start": "route_critical", "stop": "route_critical", "finish": "route_critical",
    "segment start": "route_critical", "trailhead": "route_critical",
    "coffee": "commercial", "food": "commercial", "lodging": "commercial", "bar": "commercial",
    "winery": "commercial", "wine": "commercial", "convenience store": "commercial", "convenience_store": "commercial",
    "bike shop": "commercial", "gas station": "commercial", "shopping": "commercial",
    "bike parking": "infrastructure", "restroom": "infrastructure", "rest stop": "infrastructure",
    "parking": "infrastructure", "bike": "infrastructure", "atm": "infrastructure", "bike share": "infrastructure",
    "transit center": "infrastructure", "transit": "infrastructure", "water": "infrastructure",
    "viewpoint": "informational", "monument": "informational", "information": "informational",
    "park": "informational", "swimming": "informational", "library": "informational", "summit": "informational",
    "castle": "informational", "natural": "informational", "town": "informational",
    "caution": "safety", "aid station": "safety", "first aid": "safety", "hospital": "safety",
}


def load(path):
    return json.load(open(path, encoding="utf-8"))


def save(path, data):
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def cmd_categorize(a):
    master = load(a.master)
    counts = Counter()
    for p in master:
        cat = CATEGORY_MAP.get((p.get("type") or "").strip().lower(), "uncategorized")
        p["category"] = cat
        counts[cat] += 1
    save(a.out, master)
    print(f"tagged {len(master)} POIs -> {a.out}  (original {a.master} left untouched)")
    for cat, n in counts.most_common():
        print(f"  {cat:<16} {n}")
    if counts["uncategorized"]:
        types = sorted({p.get("type", "") for p in master if p.get("category") == "uncategorized"})
        print(f"\n{counts['uncategorized']} uncategorized (unmapped types: {types}) "
              f"-- add these to CATEGORY_MAP and re-run.")


UPD_KEYS = ("updated_at", "updatedAt", "created_at", "createdAt")
NAME_KEYS = ("name", "title")


def _first(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return d[k]
    return None


def resolve_one(route_dir, source):
    fp = os.path.join(route_dir, source)
    if not os.path.exists(fp):
        return None, "missing"
    try:
        d = load(fp)
    except Exception:
        return None, "unreadable"
    r = d.get("route", d) if isinstance(d, dict) else d
    date = _first(r, UPD_KEYS)
    if date:
        return str(date)[:10], "date_field"
    name = str(_first(r, NAME_KEYS) or "")
    m = re.search(r"20\d{2}", name)
    if m:
        return m.group(0), "name_year"
    return None, "unresolvable"


def cmd_resolve_dates(a):
    master = load(a.master)
    sources = sorted({p.get("source", "") for p in master if p.get("source")})
    out = {}
    reasons = Counter()
    for s in sources:
        if not re.match(r"^\d+\.json$", s):
            reasons["not_a_route_cache_file"] += 1
            continue
        date, how = resolve_one(a.route_cache, s)
        reasons[how] += 1
        if date:
            out[s] = date
    save(a.out, out)
    print(f"resolved {len(out)}/{len(sources)} unique source routes -> {a.out}")
    for k, n in reasons.most_common():
        print(f"  {k:<20} {n}")
    print("\nrun `status` next to see how this maps onto the 5,900 POIs.")


def cmd_status(a):
    master = load(a.master)
    dates = load(a.dates) if a.dates and os.path.exists(a.dates) else {}
    cat_counts = Counter(p.get("category", "uncategorized") for p in master)
    print(f"{len(master)} POIs total\n\ncategories:")
    for c, n in cat_counts.most_common():
        print(f"  {c:<16} {n}")
    if dates:
        years = Counter()
        for p in master:
            d = dates.get(p.get("source", ""))
            years[d[:4] if d else "unresolved"] += 1
        print("\nsource-route year (via resolve-dates sidecar):")
        for y, n in sorted(years.items()):
            print(f"  {y:<12} {n}")
    else:
        print("\n(no --dates sidecar found -- run resolve-dates first for the year breakdown)")


def cmd_prune(a):
    master = load(a.master)
    dates = load(a.dates)
    cutoff = str(a.before)
    keep, drop = [], []
    for p in master:
        d = dates.get(p.get("source", ""))
        if d and d[:4] < cutoff:
            drop.append(p)
        else:
            keep.append(p)  # no resolvable date -> kept, never silently dropped
    print(f"would drop {len(drop)} / {len(master)} POIs (source route dated before {cutoff})")
    print(f"would keep {len(keep)} (including any whose source date couldn't be resolved)")
    if drop:
        print("\nsample of what would be dropped:")
        for p in drop[:15]:
            print(f"  {p.get('id', ''):<28} {p.get('type', ''):<12} src={p.get('source', '')}")
    if not a.apply:
        print("\n(dry run -- pass --apply to actually write the pruned master)")
        return
    save(a.out, keep)
    print(f"\napplied: wrote {len(keep)} POIs -> {a.out}  (original {a.master} left untouched)")


def _display_name(p):
    n = p.get("name") or {}
    return n.get("en") or n.get("nl") or n.get("de") or ""


def norm_name(s):
    """Strip accents/punctuation/case so spelling variants compare equal-ish."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def find_dupe_pairs(master, threshold, radius):
    """Shared by `dupes` (report) and `dedupe` (apply): candidate duplicate
    POIs -- same real place, entered twice under slightly different names/
    coordinates. merge/harvest only catch exact-slug or <30m matches; this
    compares normalised names within a wider radius."""
    import difflib
    cell = {}
    for p in master:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        key = (round(p["lat"], 2), round(p["lon"], 2))
        cell.setdefault(key, []).append(p)

    def neighbors(key):
        la, lo = key
        for dla in (-0.01, 0, 0.01):
            for dlo in (-0.01, 0, 0.01):
                yield (round(la + dla, 2), round(lo + dlo, 2))

    seen_pairs, hits = set(), []
    for key, pts in cell.items():
        candidates = []
        for nk in neighbors(key):
            candidates += cell.get(nk, [])
        for p in pts:
            np_ = norm_name(_display_name(p))
            if not np_:
                continue
            for q in candidates:
                if q is p:
                    continue
                pair_key = tuple(sorted((p["id"], q["id"])))
                if pair_key in seen_pairs:
                    continue
                nq = norm_name(_display_name(q))
                if not nq:
                    continue
                ratio = difflib.SequenceMatcher(None, np_, nq).ratio()
                if ratio >= threshold:
                    d = hav((p["lat"], p["lon"]), (q["lat"], q["lon"]))
                    if d <= radius:
                        seen_pairs.add(pair_key)
                        hits.append({"id_a": p["id"], "id_b": q["id"],
                                     "name_a": _display_name(p), "name_b": _display_name(q),
                                     "type_a": p.get("type", ""), "type_b": q.get("type", ""),
                                     "distance_m": round(d), "similarity": round(ratio, 2)})
    hits.sort(key=lambda h: -h["similarity"])
    return hits


def cmd_dupes(a):
    """'Spelling double register': write the candidate-duplicate CSV for
    review. Read-only -- never merges or deletes anything itself."""
    master = load(a.master)
    hits = find_dupe_pairs(master, a.threshold, a.radius)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id_a", "id_b", "name_a", "name_b",
                                          "type_a", "type_b", "distance_m", "similarity"])
        w.writeheader()
        w.writerows(hits)
    print(f"{len(hits)} likely spelling-duplicate pairs (within {a.radius:.0f} m, "
          f"similarity >= {a.threshold}) -> {a.out}")
    print("review before merging any -- similarity alone doesn't prove they're the same place.")
    for h in hits[:15]:
        print(f"  {h['similarity']:.2f}  {h['name_a']:<28} <-> {h['name_b']:<28}  {h['distance_m']}m")


def _completeness(p):
    """More filled-in fields wins when a dupe group collapses to one record."""
    score = 0
    for lang in ("en", "nl", "de"):
        if (p.get("name") or {}).get(lang):
            score += 1
        if (p.get("desc") or {}).get(lang):
            score += 1
    if p.get("locality"):
        score += 1
    if p.get("verify"):
        score += 2  # never throw away a POI a human already verified
    return score


def cmd_dedupe(a):
    """Apply the spelling-double register: collapse each candidate-duplicate
    group down to one record (the most complete -- most filled-in name/desc/
    locality fields, and never discards a POI that's already been verified),
    keep everything else untouched. No manual per-pair review -- pairs found
    at this threshold/radius are taken as confirmed duplicates. Writes a NEW
    file; the input master is never overwritten."""
    master = load(a.master)
    by_id = {p["id"]: p for p in master}
    hits = find_dupe_pairs(master, a.threshold, a.radius)

    parent = {p["id"]: p["id"] for p in master}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for h in hits:
        union(h["id_a"], h["id_b"])

    groups = {}
    for pid in by_id:
        groups.setdefault(find(pid), []).append(pid)
    groups = {root: ids for root, ids in groups.items() if len(ids) > 1}

    dropped_ids = set()
    kept_log = []
    for root, ids in groups.items():
        recs = [by_id[i] for i in ids]
        keeper = max(recs, key=_completeness)
        for i in ids:
            if i != keeper["id"]:
                dropped_ids.add(i)
        kept_log.append((keeper["id"], [i for i in ids if i != keeper["id"]]))

    result = [p for p in master if p["id"] not in dropped_ids]
    save(a.out, result)
    print(f"{len(groups)} duplicate groups -> kept 1 record each, dropped {len(dropped_ids)} "
          f"-> {len(result)} POIs total -> {a.out}  (original {a.master} left untouched)")
    for keeper, dropped in kept_log[:15]:
        print(f"  kept {keeper:<28} dropped {dropped}")


def cmd_lint(a):
    """Standard database hygiene: impossible/missing coords, a POI stranded
    far from the rest of its own source route (the robust check -- BBT tours
    span NL/DE all the way to Greek islands and the Nile, so no fixed
    lat/lon box is valid; a POI hundreds of km from its own route's other
    points is the real red flag, wherever the route is), blank required
    fields, duplicate ids, and type strings that are really the same
    category spelled/formatted differently (e.g. "convenience store" vs
    "convenience_store"). Read-only -- writes a review CSV."""
    master = load(a.master)
    issues = []
    ids_seen = Counter()
    type_variants = Counter()
    for p in master:
        pid = p.get("id", "")
        ids_seen[pid] += 1
        lat, lon = p.get("lat"), p.get("lon")
        if lat is None or lon is None:
            issues.append((pid, "missing_coords", ""))
        elif not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            issues.append((pid, "impossible_coords", f"{lat},{lon}"))
        if not _display_name(p).strip():
            issues.append((pid, "blank_name", ""))
        t = (p.get("type") or "").strip()
        if not t:
            issues.append((pid, "blank_type", ""))
        type_variants[t] += 1
    for pid, n in ids_seen.items():
        if n > 1:
            issues.append((pid, "duplicate_id", str(n)))

    # per-source-route outlier: a POI far from the centroid of its own route's
    # other POIs is a much stronger signal than any fixed geographic box.
    by_source = {}
    for p in master:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        by_source.setdefault(p.get("source", ""), []).append(p)
    for src, pts in by_source.items():
        if len(pts) < 3:
            continue  # too few points to trust a centroid
        clat = sum(p["lat"] for p in pts) / len(pts)
        clon = sum(p["lon"] for p in pts) / len(pts)
        for p in pts:
            d = hav((p["lat"], p["lon"]), (clat, clon))
            if d > a.outlier_km * 1000:
                issues.append((p["id"], "far_from_own_route", f"{round(d/1000)} km from {src}'s centroid"))

    norm_groups = {}
    for t in type_variants:
        key = re.sub(r"[^a-z0-9]", "", t.lower())
        norm_groups.setdefault(key, set()).add(t)
    inconsistent = {k: v for k, v in norm_groups.items() if len(v) > 1}

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "issue", "detail"])
        w.writerows(issues)

    print(f"{len(master)} POIs checked -> {len(issues)} issues -> {a.out}")
    for k, n in Counter(i[1] for i in issues).most_common():
        print(f"  {k:<20} {n}")
    if inconsistent:
        print(f"\n{len(inconsistent)} type spellings with inconsistent variants (fold these together):")
        for variants in inconsistent.values():
            print(f"  {sorted(variants)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("categorize")
    c.add_argument("master")
    c.add_argument("--out", default="master_pois.categorized.json")
    c.set_defaults(fn=cmd_categorize)

    r = sub.add_parser("resolve-dates")
    r.add_argument("master")
    r.add_argument("route_cache")
    r.add_argument("--out", default="poi_source_dates.json")
    r.set_defaults(fn=cmd_resolve_dates)

    s = sub.add_parser("status")
    s.add_argument("master")
    s.add_argument("--dates", default="poi_source_dates.json")
    s.set_defaults(fn=cmd_status)

    p = sub.add_parser("prune")
    p.add_argument("master")
    p.add_argument("--dates", default="poi_source_dates.json")
    p.add_argument("--before", type=int, default=2024)
    p.add_argument("--out", default="master_pruned.json")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_prune)

    d = sub.add_parser("dupes")
    d.add_argument("master")
    d.add_argument("--threshold", type=float, default=0.84, help="name-similarity cutoff 0-1 (default 0.84)")
    d.add_argument("--radius", type=float, default=300.0, help="max metres apart to count as a candidate pair")
    d.add_argument("--out", default="poi_dupes.csv")
    d.set_defaults(fn=cmd_dupes)

    dd = sub.add_parser("dedupe")
    dd.add_argument("master")
    dd.add_argument("--threshold", type=float, default=0.84)
    dd.add_argument("--radius", type=float, default=300.0)
    dd.add_argument("--out", default="master_deduped.json")
    dd.set_defaults(fn=cmd_dedupe)

    l = sub.add_parser("lint")
    l.add_argument("master")
    l.add_argument("--out", default="poi_lint.csv")
    l.add_argument("--outlier-km", type=float, default=200.0,
                   help="flag a POI this far (km) from its own route's centroid (default 200)")
    l.set_defaults(fn=cmd_lint)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
