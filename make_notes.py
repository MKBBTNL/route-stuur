#!/usr/bin/env python3
"""
make_notes.py — the one-command tie-together.

Turns cached RideWithGPS route JSON into finished guest route notes (a .txt and a
self-contained .html per route), running the whole merge pipeline from
route_sheet.py. Works on a single route or a whole folder.

Highlights: if a sidecar file named <route>.pois.json sits next to a route
(e.g. 56125488.json -> 56125488.pois.json), its POIs are woven in automatically.
That sidecar is the automatable hand-off point.

Keep this in the same folder as route_sheet.py / rwgps.py.

USAGE
  python3 make_notes.py 56125488.json                       # one route -> notes/
  python3 make_notes.py --cache route_cache --out notes     # every cached route
  python3 make_notes.py 56125488.json --mode dependent
  python3 make_notes.py a.json b.json --pois shared.pois.json
"""

import argparse, glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route_sheet as rs
import rwgps

def sidecar_pois(route_path, explicit):
    if explicit and os.path.exists(explicit):
        return explicit
    guess = re.sub(r"\.json$", ".pois.json", route_path)
    return guess if os.path.exists(guess) else None

def build_one(path, mode, explicit_pois, out_dir, dedupe_m, collapse, fold):
    rj = json.load(open(path, encoding="utf-8"))
    route = rj.get("route", rj)
    data = rwgps.extract(rj)
    title = rs.clean_title(data["name"])
    total_km = (data["distance_m"] or 0) / 1000
    cues = [c for c in data["cues"] if c["text"]]
    nodes = rs.nodes_from_cues(cues)
    pf = sidecar_pois(path, explicit_pois)
    extra = json.load(open(pf, encoding="utf-8")) if pf else []
    pois = rs.place_pois(route, data["pois"] + extra)

    if mode == "dependent":
        txt = rs.render_dependent(title, total_km, nodes, pois)
    else:
        txt = rs.render_independent(title, total_km, cues, nodes, pois, dedupe_m, collapse, fold)
    page = rs.render_html(title, total_km, cues, nodes, pois, mode, dedupe_m, collapse, fold)

    safe = re.sub(r"[^\w\-]+", "_", title).strip("_") or "route"
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, safe + ".txt"), "w", encoding="utf-8").write(txt + "\n")
    open(os.path.join(out_dir, safe + ".html"), "w", encoding="utf-8").write(page)
    return safe, len(cues), len(nodes), len(pois), bool(pf)

def main():
    ap = argparse.ArgumentParser(description="Batch route JSON -> finished notes (txt + html).")
    ap.add_argument("routes", nargs="*", help="cached route JSON file(s)")
    ap.add_argument("--cache", help="folder of cached routes; process every *.json (except *.pois.json/index.json)")
    ap.add_argument("--out", default="notes", help="output folder (default notes/)")
    ap.add_argument("--mode", choices=["independent", "dependent"], default="independent")
    ap.add_argument("--pois", help="a POIs file to use for ALL routes in this run (else per-route sidecar)")
    ap.add_argument("--dedupe-m", type=float, default=10.0)
    ap.add_argument("--no-collapse-junctions", action="store_true")
    ap.add_argument("--no-fold-straights", action="store_true")
    args = ap.parse_args()

    files = list(args.routes)
    if args.cache:
        files += [f for f in glob.glob(os.path.join(args.cache, "*.json"))
                  if not f.endswith(".pois.json") and os.path.basename(f) != "index.json"]
    files = sorted(set(files))
    if not files:
        sys.exit("No routes given. Pass a file, or --cache <folder>.")

    ok = fail = 0
    for f in files:
        try:
            name, nc, nn, npo, hadp = build_one(
                f, args.mode, args.pois, args.out, args.dedupe_m,
                not args.no_collapse_junctions, not args.no_fold_straights)
            tag = " +highlights" if hadp else ""
            print(f"  ✓ {name}  ({nc} cues, {nn} junctions, {npo} stops{tag})")
            ok += 1
        except Exception as ex:
            print(f"  ! {os.path.basename(f)} failed: {ex}")
            fail += 1
    print(f"\nWrote {ok} route(s) to {args.out}/  (txt + html each){'; ' + str(fail) + ' failed' if fail else ''}")

if __name__ == "__main__":
    main()
