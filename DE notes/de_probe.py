#!/usr/bin/env python3
"""
de_probe.py — measure what OSM gives us for a German route, so we can plan the note.

For a GPX it asks Overpass (with an offline cache, like gpx_to_knooppunten) and reports:
  1. BACKBONE   named cycle-route coverage — how much of the track follows a signed
                route (Mosel-Radweg / D-route), and which routes those are.
  2. FERRIES    Mosel ferries on/near the track (a real navigation + POI event).
  3. GUIDEPOSTS Radwegweiser signpost nodes near the track (decision-point cues).
  4. KNOOPPUNTEN rcn_ref nodes near the track (expected sparse/absent on the Mosel).
  5. LANDMARKS  DURABLE POIs only — castles, ruins, monuments/memorials, viewpoints,
                towers, churches, bridges. Hospitality (restaurant/cafe/shop/hotel) is
                DELIBERATELY NOT queried: it churns, landmarks don't.

Writes <gpx>.probe.json for the note builder.

  python3 de_probe.py route.gpx
  python3 de_probe.py routes/ --cut 300 --coverage-m 25
  python3 de_probe.py route.gpx --offline        # cache only, no network

Stdlib only. Needs internet the first time an area is fetched; caches thereafter.
"""
import argparse, glob, json, math, os, sys
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET

OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
GPX = "{http://www.topografix.com/GPX/1/1}"

# ---- geometry (equirectangular; fine at route scale) ----
def to_xy(lat, lon, lat0):
    R = 6371000.0
    return math.radians(lon)*R*math.cos(math.radians(lat0)), math.radians(lat)*R

def seg_d(px, py, ax, ay, bx, by):
    dx, dy = bx-ax, by-ay; L2 = dx*dx+dy*dy
    if L2 == 0: return math.hypot(px-ax, py-ay), 0.0
    t = max(0.0, min(1.0, ((px-ax)*dx+(py-ay)*dy)/L2))
    return math.hypot(px-(ax+t*dx), py-(ay+t*dy)), t

def read_track(path):
    r = ET.parse(path).getroot()
    pts = [(float(t.get("lat")), float(t.get("lon"))) for t in r.iter(GPX+"trkpt")]
    if len(pts) < 2: raise ValueError("no track")
    lat0 = sum(p[0] for p in pts)/len(pts)
    xy = [to_xy(la, lo, lat0) for la, lo in pts]
    cum = [0.0]
    for i in range(1, len(xy)):
        cum.append(cum[-1]+math.hypot(xy[i][0]-xy[i-1][0], xy[i][1]-xy[i-1][1]))
    return pts, xy, cum, lat0

def bbox_for(pts, pad=0.02):
    la = [p[0] for p in pts]; lo = [p[1] for p in pts]
    return (min(la)-pad, min(lo)-pad, max(la)+pad, max(lo)+pad)

def along(xy, cum, x, y):
    best_d, best_km = 1e18, 0.0
    step = max(1, len(xy)//600)          # sample for speed
    for i in range(0, len(xy)-1, step):
        d, t = seg_d(x, y, xy[i][0], xy[i][1], xy[i+1][0], xy[i+1][1])
        if d < best_d:
            best_d, best_km = d, (cum[i]+t*(cum[i+1]-cum[i]))/1000
    return best_d, round(best_km, 1)

# ---- Overpass (with cache) ----
def fetch(query, cache_path, key, offline, refresh):
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try: cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception: cache = {}
    if key in cache and not refresh:
        return cache[key]
    if offline:
        sys.exit(f"Offline and '{key}' not cached. Run once online to populate {cache_path}.")
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "de-probe/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                js = json.load(r)
            if cache_path:
                cache[key] = js.get("elements", [])
                json.dump(cache, open(cache_path, "w", encoding="utf-8"))
            return js.get("elements", [])
        except Exception as e:
            last = e
    sys.exit(f"Overpass unreachable ({last}). Try later or from a networked machine.")

def q_elements(bbox):
    s, w, n, e = bbox; b = f"{s},{w},{n},{e}"
    # durable landmarks + ferries + guideposts + knooppunten. NO hospitality/retail.
    return f"""[out:json][timeout:180];
(
  node["rcn_ref"]({b});
  node["information"="guidepost"]({b});
  node["amenity"="ferry_terminal"]({b});
  way["route"="ferry"]({b});
  node["historic"~"castle|ruins|monument|memorial|fort|manor|tower|archaeological_site|city_gate"]({b});
  way["historic"~"castle|ruins|monument|memorial|fort|manor|tower|archaeological_site|city_gate"]({b});
  node["tourism"="viewpoint"]({b});
  node["man_made"~"tower|lighthouse"]({b});
  way["man_made"~"tower|lighthouse"]({b});
  way["amenity"="place_of_worship"]({b});
  node["amenity"="place_of_worship"]({b});
  way["man_made"="bridge"]({b});
);
out center tags;"""

def q_routegeom(bbox):
    s, w, n, e = bbox; b = f"{s},{w},{n},{e}"
    return f'[out:json][timeout:180];relation["route"="bicycle"]({b});out tags;way(r);out geom;'

# ---- classify a durable landmark ----
def landmark_type(t):
    h = t.get("historic", ""); m = t.get("man_made", "")
    if h in ("castle", "fort", "manor"): return "castle"
    if h == "ruins": return "ruins"
    if h in ("monument", "memorial"): return "monument"
    if h == "city_gate": return "city_gate"
    if h == "archaeological_site": return "archaeo"
    if t.get("tourism") == "viewpoint": return "viewpoint"
    if m in ("tower", "lighthouse") or h == "tower": return "tower"
    if m == "bridge": return "bridge"
    if t.get("amenity") == "place_of_worship": return "church"
    return None

VOLATILE = {"restaurant", "cafe", "fast_food", "bar", "pub", "biergarten", "hotel", "guest_house", "hostel"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help=".gpx file or folder")
    ap.add_argument("--cut", type=float, default=300.0, help="max m from track to keep a landmark")
    ap.add_argument("--coverage-m", type=float, default=25.0, help="m band for 'on a named route'")
    ap.add_argument("--cache", default="de_probe_cache.json")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    files = [a.path] if a.path.endswith(".gpx") else sorted(glob.glob(os.path.join(a.path, "*.gpx")))
    for fp in files:
        pts, xy, cum, lat0 = read_track(fp)
        total = cum[-1]/1000
        bbox = bbox_for(pts)
        els = fetch(q_elements(bbox), a.cache, os.path.basename(fp)+"|el", a.offline, a.refresh)
        rels = fetch(q_routegeom(bbox), a.cache, os.path.basename(fp)+"|rt", a.offline, a.refresh)

        # backbone: named cycle routes + coverage
        route_names, way_geoms = [], []
        for el in rels:
            if el["type"] == "relation":
                t = el.get("tags", {})
                if t.get("route") == "bicycle":
                    route_names.append({"name": t.get("name", ""), "ref": t.get("ref", ""), "network": t.get("network", "")})
            elif el["type"] == "way" and "geometry" in el:
                way_geoms.append([(p["lat"], p["lon"]) for p in el["geometry"]])
        # coverage: % of sampled track points within coverage-m of ANY named-route way
        seg = []
        for g in way_geoms:
            for i in range(len(g)-1):
                seg.append((to_xy(*g[i], lat0), to_xy(*g[i+1], lat0)))
        # coverage timeline (km, on-route?) -> % and deviation segments
        timeline = []
        step = max(1, len(xy)//400)
        for i in range(0, len(xy), step):
            px, py = xy[i]; on = False
            for (ax, ay), (bx, by) in seg:
                d, _ = seg_d(px, py, ax, ay, bx, by)
                if d <= a.coverage_m: on = True; break
            timeline.append((cum[i]/1000, on))
        tot = len(timeline); hit = sum(1 for _, o in timeline if o)
        coverage = round(100*hit/tot, 1) if tot else 0.0
        # deviations: contiguous OFF runs >= MINDEV km (ignore GPS noise)
        MINDEV = 0.3
        deviations, run = [], None
        for km, on in timeline:
            if not on and run is None: run = km
            elif on and run is not None:
                if km-run >= MINDEV: deviations.append({"leave_km": round(run,1), "rejoin_km": round(km,1), "span_km": round(km-run,1)})
                run = None
        if run is not None and timeline and timeline[-1][0]-run >= MINDEV:
            deviations.append({"leave_km": round(run,1), "rejoin_km": round(timeline[-1][0],1), "span_km": round(timeline[-1][0]-run,1)})

        # categorize point elements
        kp, ferries, guideposts, landmarks = [], [], [], []
        for el in els:
            t = el.get("tags", {})
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None: continue
            if t.get("amenity") in VOLATILE or "shop" in t: continue   # belt-and-braces skip
            x, y = to_xy(lat, lon, lat0); d, km = along(xy, cum, x, y)
            if "rcn_ref" in t:
                if d <= 40: kp.append({"kp": t["rcn_ref"], "km": km})
                continue
            if t.get("route") == "ferry" or t.get("amenity") == "ferry_terminal":
                if d <= a.cut: ferries.append({"name": t.get("name", "ferry"), "km": km, "dist_m": round(d)})
                continue
            if t.get("information") == "guidepost":
                if d <= a.cut: guideposts.append({"km": km, "dist_m": round(d)})
                continue
            lt = landmark_type(t)
            if lt and d <= a.cut:
                landmarks.append({"name": t.get("name", ""), "type": lt, "km": km, "dist_m": round(d),
                                  "lat": round(lat, 5), "lon": round(lon, 5)})
        landmarks = [l for l in landmarks if l["name"]]      # unnamed landmarks aren't note-worthy
        for lst in (ferries, guideposts, landmarks): lst.sort(key=lambda z: z["km"])
        kp.sort(key=lambda z: z["km"])

        report = {"gpx": os.path.basename(fp), "distance_km": round(total, 1),
                  "backbone_coverage_pct": coverage,
                  "named_routes": [r for r in route_names if r["name"]][:8],
                  "deviations": deviations,
                  "ferries": ferries, "guideposts_count": len(guideposts),
                  "knooppunten": kp, "landmarks": landmarks}
        out = fp.rsplit(".", 1)[0] + ".probe.json"
        json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

        # console summary
        print(f"\n{'='*66}\n{report['gpx']}  ({report['distance_km']} km)")
        names = ", ".join(f'{r["name"]}'+(f' [{r["ref"]}]' if r["ref"] else "") for r in report["named_routes"]) or "none found"
        print(f"  BACKBONE: {coverage}% on a named route  |  routes: {names}")
        print(f"  ferries: {len(ferries)}  guideposts: {len(guideposts)}  knooppunten: {len(kp)}  durable landmarks: {len(landmarks)}")
        dev_spans = ", ".join(f"{d['leave_km']}\u2013{d['rejoin_km']}km" for d in deviations[:6])
        print(f"  deviations off the named route: {len(deviations)}" + (f"  ({dev_spans})" if deviations else ""))
        by = {}
        for l in landmarks: by[l["type"]] = by.get(l["type"], 0)+1
        if by: print("  landmark types: " + ", ".join(f"{k}×{v}" for k, v in sorted(by.items())))
        for f in ferries: print(f"    ⛴  {f['km']} km  {f['name']}")
        for l in landmarks[:8]: print(f"    ◆  {l['km']:>4} km  {l['type']:<9} {l['name']}")
        print(f"  → wrote {os.path.basename(out)}")

if __name__ == "__main__":
    main()
