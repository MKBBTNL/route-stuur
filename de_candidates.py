#!/usr/bin/env python3
"""
de_candidates.py — turn a raw <route>.probe.json into a clean pick-list.

The probe is deliberately greedy; this makes it usable:
  * FERRIES   collapse duplicate/nearby entries to one per CROSSING; drop
              sightseeing-boat piers (Personenschifffahrt / Anlegestelle).
  * LANDMARKS rank by notability (castle/ruins > viewpoint/gate/tower/archaeo >
              church > monument-plaque), dedupe, and cap into a shortlist you
              pick from. Curated RWGPS POIs (if a sibling .gpx has waypoints)
              always win and sit on top.
  * DEVIATIONS keep only spans >= --mindev km (default 1.0); the rest are noise.

  python3 de_candidates.py de/*.probe.json
  python3 de_candidates.py route.probe.json --gpx route.gpx --cap 10

Writes <route>.candidates.json. Stdlib only, no network.
"""
import argparse, glob, json, math, os, re
import xml.etree.ElementTree as ET

TIER = {"castle":1,"ruins":1,"viewpoint":2,"city_gate":2,"archaeo":2,"tower":3,"bridge":4,"church":5,"monument":6}
TIERNAME = {1:"marquee",2:"scenic/heritage",3:"tower",4:"bridge",5:"church",6:"plaque/monument"}

def norm(s): return re.sub(r"\s+"," ",(s or "").strip().lower())

# ---- ferries: one per location, tagged (never dropped: a real crossing may be run by a 'Personenschiffahrt') ----
def ferry_kind(name):
    n = norm(name)
    if "fähr" in n or "faehr" in n or n == "ferry": return "crossing"
    if re.search(r"[a-zä]\s*[-–]\s*[a-zä]", n) and "touristik" not in n: return "crossing"  # "A - B" bank pattern
    if any(k in n for k in ("touristik","schiffs-mosel","mosel-schiffs","gebr","schifffahrt")): return "pier"
    return "pier"

def dedupe_ferries(ferries, gap=0.9):
    fs = sorted(ferries, key=lambda f: f["km"])
    out, cur = [], None
    for f in fs:
        k = ferry_kind(f.get("name", ""))
        if cur and f["km"]-cur["_last"] <= gap:
            cur["_last"] = f["km"]
            if k == "crossing": cur["kind"] = "crossing"
            if f.get("dist_m", 999) < cur["dist_m"]:
                cur["dist_m"] = f["dist_m"]
                if norm(f.get("name","")) not in ("", "ferry"): cur["name"] = f["name"]
        else:
            cur = {"name": f.get("name","Fähre") if norm(f.get("name",""))!="ferry" else "Fähre",
                   "km": f["km"], "dist_m": f.get("dist_m",0), "kind": k, "_last": f["km"]}
            out.append(cur)
    for c in out: c.pop("_last", None)
    return out

# ---- landmarks: rank + dedupe + cap ----
def rank_landmarks(lm, cap):
    seen, uniq = set(), []
    for l in sorted(lm, key=lambda x: (TIER.get(x["type"],7), x.get("dist_m",999))):
        nk = norm(l["name"])                       # collapse same-name near-duplicates (two "Brauselay")
        if not nk or nk in seen: continue
        seen.add(nk); uniq.append(l)
    marquee = [l for l in uniq if TIER.get(l["type"],7) == 1]          # castles/ruins: always keep
    rest = [l for l in uniq if TIER.get(l["type"],7) >= 2]
    rest.sort(key=lambda x: (TIER.get(x["type"],7), x["km"]))
    shortlist = marquee + rest[:max(0, cap-len(marquee))]
    shortlist.sort(key=lambda x: x["km"])
    more = [l for l in uniq if l not in shortlist]
    return shortlist, more

# ---- curated RWGPS waypoints (win over OSM) ----
def curated_from_gpx(path):
    if not path or not os.path.exists(path): return []
    G="{http://www.topografix.com/GPX/1/1}"
    r = ET.parse(path).getroot()
    out = []
    for w in r.findall(f".//{G}wpt"):
        nm = w.findtext(f"{G}name") or ""
        cmt = (w.findtext(f"{G}cmt") or w.findtext(f"{G}type") or "").strip()
        if nm: out.append({"name": nm, "kind": cmt or "curated"})
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("probes", nargs="+", help="one or more *.probe.json")
    ap.add_argument("--gpx", help="sibling gpx for curated waypoints (single-file mode)")
    ap.add_argument("--mindev", type=float, default=1.0, help="min deviation span km to keep")
    ap.add_argument("--cap", type=int, default=10, help="landmark shortlist size")
    a = ap.parse_args()
    files = []
    for p in a.probes: files += glob.glob(p)
    for fp in sorted(files):
        d = json.load(open(fp, encoding="utf-8"))
        ferries = dedupe_ferries(d.get("ferries", []))
        shortlist, more = rank_landmarks(d.get("landmarks", []), a.cap)
        devs = [x for x in d.get("deviations", []) if x["span_km"] >= a.mindev]
        minor = [x for x in d.get("deviations", []) if x["span_km"] < a.mindev]
        gpx = a.gpx if (a.gpx and len(files) == 1) else None
        curated = curated_from_gpx(gpx)
        out = {"gpx": d["gpx"], "distance_km": d["distance_km"],
               "backbone": {"coverage_pct": d["backbone_coverage_pct"],
                            "route": (d["named_routes"][0]["name"] if d.get("named_routes") else None)},
               "deviations": devs, "deviations_minor_dropped": len(minor),
               "ferry_crossings": ferries,
               "curated_pois": curated,
               "landmark_shortlist": shortlist, "landmark_more": more}
        outp = fp.replace(".probe.json", ".candidates.json").replace(".json", ".candidates.json") if not fp.endswith(".probe.json") else fp.replace(".probe.json", ".candidates.json")
        json.dump(out, open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

        print(f"\n{'='*70}\n{d['gpx'][:52]}  ({d['distance_km']} km)")
        print(f"  backbone {out['backbone']['coverage_pct']}% on {out['backbone']['route']}")
        print(f"  real deviations (>={a.mindev}km): {[(x['leave_km'],x['rejoin_km'],x['span_km']) for x in devs]}"
              f"   (dropped {len(minor)} noise)")
        print(f"  ferry crossings: {len(ferries)} (from {len(d.get('ferries',[]))} raw)")
        for f in ferries: print(f"     ⛴  {f['km']:>4} km  [{f['kind']:<8}] {f['name']}")
        if curated: print(f"  curated RWGPS POIs (win): {len(curated)}")
        print(f"  landmark shortlist ({len(shortlist)} of {len(d.get('landmarks',[]))} raw):")
        for l in shortlist:
            print(f"     ◆ {l['km']:>4} km  [{TIERNAME[TIER.get(l['type'],7)]:<15}] {l['type']:<9} {l['name']}")
        print(f"  → {os.path.basename(outp)}")

if __name__ == "__main__":
    main()
