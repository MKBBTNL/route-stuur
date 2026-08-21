#!/usr/bin/env python3
"""
routekit.py — one tool. A GPX (or TCX) goes in, all the guest deliverables come out.

  routekit build tour.gpx                 # foolproof: everything, into ./out/
  routekit build tour.gpx --lang nl       # notes in Dutch
  routekit build routes/ --lang all       # a whole folder, all three languages
  routekit build tour.tcx --master poi_master.json   # pull POIs from the master

  routekit poi merge   master.json a.gpx b.gpx        # expert: POI-library ops
  routekit poi pick    master.json tour.gpx --emit tour.pois.json
  routekit poi view    master.json --out master_view.csv
  routekit poi harvest master.json route_cache/ --inspect

WHAT A COLLEAGUE HAS TO KNOW
  Just:  routekit build <file>
  Drop a .gpx or .tcx in, get a dated output folder with route notes (txt + html),
  a POI sidecar (json), and the track re-exported as clean .gpx and .tcx. POIs are
  read from the file's own waypoints, or from a <file>.pois.json sidecar sitting
  next to it, or from a shared master with --master. No other flags needed.

DESIGN NOTES (folded-in decisions)
  - Single complete route-notes output. The old dependent/independent split is gone:
    the notes carry everything a paper-only rider needs; a GPS rider ignores the extras.
  - Knooppunten come from the OSM node network (rcn_ref nodes), snapped to the track,
    with the same offline cache as before — populate once online, run offline forever.
  - Off-network gaps are marked honestly. Turn-by-turn inside a gap is never invented.
  - Full compass words, in the output language. Proper names are never translated.
  - Stdlib only. One file. Nothing to pip-install, nothing to keep in the same folder.
"""

import argparse, csv, glob, json, math, os, re, sys
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ======================================================================================
# House wording — edit these to taste; POI names/descriptions are never touched by this.
# ======================================================================================
COMPASS = {
    "en": ["North", "Northeast", "East", "Southeast", "South", "Southwest", "West", "Northwest"],
    "nl": ["Noord", "Noordoost", "Oost", "Zuidoost", "Zuid", "Zuidwest", "West", "Noordwest"],
    "de": ["Norden", "Nordosten", "Osten", "Südosten", "Süden", "Südwesten", "Westen", "Nordwesten"],
}
PHR = {
    "en": {
        "subtitle": "route notes",
        "nodes": "Nodes", "none_on_net": "(none on the node network)",
        "start_to": "Start — follow the rcn signs to node {kp}",
        "toward": "heading {c} toward {dest}",
        "swings": "heading {c} — the route swings away from {dest} on this stretch",
        "to_finish": "→ FINISH", "arrow": "→ {kp}",
        "finish": "{dest} {km:.1f} km — follow the \u201cCentrum\u201d signs",
        "leave": "LEAVE THE NETWORK \u00b7 {span:.1f} km \u00b7 no node signs \u00b7 rejoin at {to}",
        "toadd": "(written directions for this stretch: to be added)",
        "stop": "STOP", "no_net": "No knooppunten on this route — off-network the whole way.",
        "kp_unavail": "Knooppunten unavailable (no network and this area is not cached).",
    },
    "nl": {
        "subtitle": "routebeschrijving",
        "nodes": "Knooppunten", "none_on_net": "(geen op het knooppuntennetwerk)",
        "start_to": "Start — volg de knooppuntbordjes naar knooppunt {kp}",
        "toward": "richting {c} naar {dest}",
        "swings": "richting {c} — de route buigt hier weg van {dest}",
        "to_finish": "→ FINISH", "arrow": "→ {kp}",
        "finish": "{dest} {km:.1f} km — volg de borden \u201cCentrum\u201d",
        "leave": "VERLAAT HET NETWERK \u00b7 {span:.1f} km \u00b7 geen bordjes \u00b7 sluit weer aan bij {to}",
        "toadd": "(beschrijving voor dit stuk: volgt nog)",
        "stop": "STOP", "no_net": "Geen knooppunten op deze route — het hele traject buiten het netwerk.",
        "kp_unavail": "Knooppunten niet beschikbaar (geen netwerk en dit gebied staat niet in de cache).",
    },
    "de": {
        "subtitle": "Routenbeschreibung",
        "nodes": "Knotenpunkte", "none_on_net": "(keine im Knotenpunktnetz)",
        "start_to": "Start — den Knotenpunktschildern zu Knoten {kp} folgen",
        "toward": "Richtung {c} nach {dest}",
        "swings": "Richtung {c} — die Route führt hier von {dest} weg",
        "to_finish": "→ ZIEL", "arrow": "→ {kp}",
        "finish": "{dest} {km:.1f} km — den Schildern \u201eZentrum\u201c folgen",
        "leave": "NETZ VERLASSEN \u00b7 {span:.1f} km \u00b7 keine Schilder \u00b7 Wiedereinstieg bei {to}",
        "toadd": "(Wegbeschreibung für diesen Abschnitt: folgt)",
        "stop": "STOPP", "no_net": "Keine Knotenpunkte auf dieser Route — durchgehend außerhalb des Netzes.",
        "kp_unavail": "Knotenpunkte nicht verfügbar (kein Netz und dieses Gebiet ist nicht im Cache).",
    },
}
LANGS = ("en", "nl", "de")

# ======================================================================================
# Geometry (equirectangular projection; fine at tour scale)  [from gpx_to_knooppunten]
# ======================================================================================
def to_xy(lat, lon, lat0):
    R = 6371000.0
    return math.radians(lon) * R * math.cos(math.radians(lat0)), math.radians(lat) * R

def seg_project(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    fx, fy = ax + t * dx, ay + t * dy
    return math.hypot(px - fx, py - fy), t

def hav(a, b):
    R = 6371000.0
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def bearing(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def cardinal_word(deg, lang):
    return COMPASS[lang][round(deg / 45) % 8]

def angle_diff(a, b):
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d

def crow_km(a, b):
    lat0 = (a[0] + b[0]) / 2
    ax, ay = to_xy(a[0], a[1], lat0); bx, by = to_xy(b[0], b[1], lat0)
    return math.hypot(ax - bx, ay - by) / 1000

# ======================================================================================
# Ingest — GPX and TCX, unified into (points, waypoints, name)
# ======================================================================================
GPX_TRKPT = "{http://www.topografix.com/GPX/1/1}trkpt"
GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}
TCX_NS = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}

def _cum(points):
    lat0 = sum(p[0] for p in points) / len(points)
    xy = [to_xy(la, lo, lat0) for la, lo in points]
    cum = [0.0]
    for i in range(1, len(xy)):
        cum.append(cum[-1] + math.hypot(xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]))
    return xy, cum, lat0

def read_gpx(root):
    pts = [(float(t.get("lat")), float(t.get("lon"))) for t in root.iter(GPX_TRKPT)]
    wpts = []
    for w in root.findall("g:wpt", GPX_NS):
        wpts.append({"pt": (float(w.get("lat")), float(w.get("lon"))),
                     "name": w.findtext("g:name", "", GPX_NS),
                     "type": w.findtext("g:cmt", "", GPX_NS) or "",
                     "desc": (w.findtext("g:desc", "", GPX_NS) or "").replace("\n", " ").strip()})
    return pts, wpts

def read_tcx(root):
    pts = []
    for tp in root.findall(".//t:Trackpoint", TCX_NS):
        pos = tp.find("t:Position", TCX_NS)
        if pos is None:
            continue
        la = pos.findtext("t:LatitudeDegrees", None, TCX_NS)
        lo = pos.findtext("t:LongitudeDegrees", None, TCX_NS)
        if la and lo:
            pts.append((float(la), float(lo)))
    wpts = []
    for cp in root.findall(".//t:CoursePoint", TCX_NS):
        pos = cp.find("t:Position", TCX_NS)
        if pos is None:
            continue
        la = pos.findtext("t:LatitudeDegrees", None, TCX_NS)
        lo = pos.findtext("t:LongitudeDegrees", None, TCX_NS)
        if not (la and lo):
            continue
        wpts.append({"pt": (float(la), float(lo)),
                     "name": cp.findtext("t:Name", "", TCX_NS) or "",
                     "type": cp.findtext("t:PointType", "", TCX_NS) or "",
                     "desc": (cp.findtext("t:Notes", "", TCX_NS) or "").strip()})
    return pts, wpts

def _root(source):
    """Parse an XML root from a filesystem path OR raw bytes/str content (an upload)."""
    if isinstance(source, (bytes, bytearray)):
        return ET.fromstring(source)
    if isinstance(source, str) and not os.path.exists(source) and source.lstrip().startswith("<"):
        return ET.fromstring(source)
    return ET.parse(source).getroot()   # path

def ingest(source, name=None):
    """source: a path, or raw .gpx/.tcx content (bytes/str). name gives the extension
    when source is content rather than a path."""
    ext = os.path.splitext(name or (source if isinstance(source, str) else ""))[1].lower()
    root = _root(source)
    if ext == ".tcx":
        pts, wpts = read_tcx(root)
    elif ext == ".gpx":
        pts, wpts = read_gpx(root)
    else:
        raise ValueError(f"unsupported input '{ext or '?'}' (use .gpx or .tcx)")
    if len(pts) < 2:
        raise ValueError("no track with 2+ points found")
    xy, cum, lat0 = _cum(pts)
    return pts, xy, cum, lat0, wpts

# ======================================================================================
# Title from filename (RWGPS style "Origin - Dest 51 km ENG 2026")
# ======================================================================================
def parse_title(path):
    name = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
    origin, rest = (name.split(" - ", 1) + [""])[:2] if " - " in name else (name, "")
    m = re.match(r"(.*?)\s+\d+\s*km", rest)
    dest = (m.group(1) if m else rest).strip()
    origin = re.sub(r"\s*\(.*?\)", "", origin).strip()
    return origin or "Start", dest or "Finish"

# ======================================================================================
# Overpass fetch + offline cache  [from gpx_to_knooppunten]
# ======================================================================================
OVERPASS_MIRRORS = ["https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter"]
DEFAULT_CACHE = "rcn_cache.json"

def bbox_for(pts, pad=0.01):
    lats = [p[0] for p in pts]; lons = [p[1] for p in pts]
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)

def bbox_contains(o, i):
    return o[0] <= i[0] and o[1] <= i[1] and o[2] >= i[2] and o[3] >= i[3]

def fetch_overpass(bbox):
    s, w, n, e = bbox
    q = f'[out:json][timeout:60];node["rcn_ref"]({s},{w},{n},{e});out;'
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "routekit/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                js = json.load(r)
            return [[el["id"], el["tags"]["rcn_ref"], el["lat"], el["lon"]]
                    for el in js.get("elements", []) if el.get("tags", {}).get("rcn_ref")]
        except Exception as ex:
            last = ex
    raise RuntimeError(str(last))

def load_cache(path):
    if path and os.path.exists(path):
        try:
            js = json.load(open(path, encoding="utf-8"))
            return {"bboxes": [tuple(b) for b in js.get("bboxes", [])],
                    "nodes": {int(k): v for k, v in js.get("nodes", {}).items()}}
        except Exception:
            pass
    return {"bboxes": [], "nodes": {}}

def save_cache(path, cache):
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

def get_nodes(pts, cache_path, offline, refresh):
    """Return (nodes, source) or ([], reason). Never fatal — knooppunten degrade gracefully."""
    bbox = bbox_for(pts)
    cache = load_cache(cache_path) if cache_path else {"bboxes": [], "nodes": {}}
    covered = any(bbox_contains(b, bbox) for b in cache["bboxes"])
    if offline or (covered and not refresh):
        if not covered:
            return [], "offline-uncached"
        nodes = [[nid, ref, lat, lon] for nid, (ref, lat, lon) in cache["nodes"].items()
                 if bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]]
        return nodes, "cache"
    try:
        fetched = fetch_overpass(bbox)
    except RuntimeError:
        if cache["nodes"]:
            nodes = [[nid, ref, lat, lon] for nid, (ref, lat, lon) in cache["nodes"].items()
                     if bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]]
            if nodes:
                return nodes, "cache(partial)"
        return [], "no-network"
    if cache_path:
        for nid, ref, lat, lon in fetched:
            cache["nodes"][nid] = [ref, lat, lon]
        cache["bboxes"].append(bbox)
        save_cache(cache_path, cache)
        return fetched, "overpass(cached)"
    return fetched, "overpass"

# ======================================================================================
# Snap nodes to track, find gaps  [from gpx_to_routenotes]
# ======================================================================================
def snap_coords(nodes, xy, cum, lat0, threshold):
    hits = []
    for nid, ref, nlat, nlon in nodes:
        nx, ny = to_xy(nlat, nlon, lat0)
        best_d, best_along = 1e18, 0.0
        for i in range(len(xy) - 1):
            d, t = seg_project(nx, ny, xy[i][0], xy[i][1], xy[i + 1][0], xy[i + 1][1])
            if d < best_d:
                best_d, best_along = d, cum[i] + t * (cum[i + 1] - cum[i])
        if best_d <= threshold:
            hits.append({"kp": ref, "km": round(best_along / 1000, 2),
                         "offset_m": round(best_d, 1), "lat": nlat, "lon": nlon})
    hits.sort(key=lambda h: h["km"])
    seq = []
    for h in hits:
        if not seq or seq[-1]["kp"] != h["kp"]:
            seq.append(h)
    return seq

def fetch_ferry_ways(bbox):
    """Ferry route ways (route=ferry) from OSM within bbox, with inline geometry --
    a second, independent Overpass query alongside fetch_overpass's rcn_ref node query."""
    s, w, n, e = bbox
    q = f'[out:json][timeout:60];way["route"="ferry"]({s},{w},{n},{e});out geom;'
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "routekit/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                js = json.load(r)
            ways = []
            for el in js.get("elements", []):
                geom = el.get("geometry")
                if geom and len(geom) >= 2:
                    ways.append({"name": el.get("tags", {}).get("name", ""),
                                 "pts": [(g["lat"], g["lon"]) for g in geom]})
            return ways
        except Exception as ex:
            last = ex
    raise RuntimeError(str(last))

def find_ferry_crossings(xy, cum, lat0, ferry_ways, edge_m=400.0):
    """A ferry way 'crosses' the track if both of its shore ends project onto the
    track within edge_m (terminals often sit a little off the recorded path) --
    the along-track span between those two projected km positions is the crossing."""
    crossings = []
    for way in ferry_ways:
        pts = way["pts"]
        along = []
        for (la, lo) in (pts[0], pts[-1]):
            px, py = to_xy(la, lo, lat0)
            best_d, best_along = 1e18, 0.0
            for i in range(len(xy) - 1):
                d, t = seg_project(px, py, xy[i][0], xy[i][1], xy[i + 1][0], xy[i + 1][1])
                if d < best_d:
                    best_d, best_along = d, cum[i] + t * (cum[i + 1] - cum[i])
            along.append((best_d, best_along))
        if along[0][0] <= edge_m and along[1][0] <= edge_m:
            a_km, b_km = sorted(a[1] / 1000 for a in along)
            crossings.append({"from_km": round(a_km, 2), "to_km": round(b_km, 2), "name": way.get("name", "")})
    crossings.sort(key=lambda c: c["from_km"])
    return crossings

def get_ferries(pts, xy, cum, lat0, offline):
    """Best-effort ferry detection via OSM route=ferry ways. Online-only for now (no
    cache, unlike the knooppunt network) -- never fatal, just degrades to zero
    detected crossings (ferry marking stays a manual ribbon toggle, same as before)."""
    if offline:
        return []
    try:
        ways = fetch_ferry_ways(bbox_for(pts))
    except Exception:
        return []
    return find_ferry_crossings(xy, cum, lat0, ways)

def apply_ferry_flags(seq, crossings):
    """Mark the node just after each detected crossing's far shore as 'ferry' -- a
    dashed connector + boat glyph is drawn *before* that node, matching the convention
    the route_builder.html ribbon checkbox already uses. Never flags the first node
    (nothing to cross from)."""
    for h in seq:
        h.setdefault("ferry", False)
    for c in crossings:
        nxt = next((h for h in seq if h["km"] >= c["to_km"] - 0.05), None)
        if nxt is not None and nxt is not seq[0]:
            nxt["ferry"] = True
    return seq

def find_gaps(seq, total_km, gap_km, edge_km=0.8):
    gaps = []
    if seq and seq[0]["km"] > max(gap_km, edge_km):
        gaps.append({"kind": "start", "from": "START", "to": seq[0]["kp"], "a_km": 0.0, "b_km": seq[0]["km"]})
    for i in range(len(seq) - 1):
        if seq[i + 1]["km"] - seq[i]["km"] > gap_km:
            gaps.append({"kind": "mid", "from": seq[i]["kp"], "to": seq[i + 1]["kp"],
                         "a_km": seq[i]["km"], "b_km": seq[i + 1]["km"]})
    if seq and (total_km - seq[-1]["km"]) > max(gap_km, edge_km):
        gaps.append({"kind": "end", "from": seq[-1]["kp"], "to": "FINISH",
                     "a_km": seq[-1]["km"], "b_km": total_km})
    return gaps

# ======================================================================================
# POIs — from waypoints, or picked from a master  [from poi_master + make_notes]
# ======================================================================================
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def place_along(pois_xy, xy, cum, lat0, cut):
    """Attach km-along-track + offset to each POI; keep those within cut; order along track."""
    out = []
    for p in pois_xy:
        pt = (p["lat"], p["lon"])
        px, py = to_xy(pt[0], pt[1], lat0)
        best_d, best_along = 1e18, 0.0
        for i in range(len(xy) - 1):
            d, t = seg_project(px, py, xy[i][0], xy[i][1], xy[i + 1][0], xy[i + 1][1])
            if d < best_d:
                best_d, best_along = d, cum[i] + t * (cum[i + 1] - cum[i])
        if best_d <= cut:
            q = dict(p); q["km"] = round(best_along / 1000, 2); q["offset_m"] = round(best_d, 1)
            out.append(q)
    out.sort(key=lambda p: p["km"])
    return out

def pois_from_waypoints(wpts, lang):
    """GPX/TCX own waypoints -> normalized POIs in the requested language (single-language source)."""
    out = []
    for w in wpts:
        if not w["name"]:
            continue
        out.append({"name": w["name"], "type": w.get("type", ""),
                    "lat": w["pt"][0], "lon": w["pt"][1], "desc": w.get("desc", "")})
    return out

def pois_from_master(master_path, lang):
    master = json.load(open(master_path, encoding="utf-8"))
    out = []
    for p in master:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        nm = p["name"].get(lang) or p["name"].get("en") or ""
        ds = p["desc"].get(lang) or p["desc"].get("en") or ""
        if not nm:
            continue
        out.append({"name": nm, "type": p.get("type", ""), "lat": p["lat"], "lon": p["lon"], "desc": ds})
    return out

def sidecar_path(route_path):
    guess = re.sub(r"\.(gpx|tcx)$", ".pois.json", route_path, flags=re.I)
    return guess if os.path.exists(guess) else None

def pois_from_sidecar(path, lang):
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for p in data:
        nm = p.get("name")
        if isinstance(nm, dict):
            nm = nm.get(lang) or nm.get("en")
        ds = p.get("desc", "")
        if isinstance(ds, dict):
            ds = ds.get(lang) or ds.get("en") or ""
        if nm and p.get("lat") is not None and p.get("lon") is not None:
            out.append({"name": nm, "type": p.get("type", ""), "lat": p["lat"], "lon": p["lon"], "desc": ds})
    return out

# ======================================================================================
# Render — single complete route notes (localized)
# ======================================================================================
def render_notes(origin, dest, total_km, seq, gaps, pois, finish_pt, diverge_deg, lang):
    P = PHR[lang]
    L = [f"{origin.upper()} \u2192 {dest.upper()}    {total_km:.1f} km    \u00b7 {P['subtitle']} \u00b7", ""]
    # merge nodes + pois into one km-ordered timeline
    timeline = [{"t": "node", "km": h["km"], "d": h} for h in seq] + \
               [{"t": "poi", "km": p["km"], "d": p} for p in pois]
    timeline.sort(key=lambda e: e["km"])

    if not seq:
        L.append(P["no_net"])
    else:
        L.append(P["start_to"].format(kp=seq[0]["kp"]))
    gap_after = {g["from"]: g for g in gaps if g["kind"] in ("mid", "end")}
    rejoin = {g["to"] for g in gaps if g["kind"] in ("start", "mid")}
    node_order = [h["kp"] for h in seq]
    idx_of = {id(h): i for i, h in enumerate(seq)}

    for e in timeline:
        if e["t"] == "poi":
            p = e["d"]
            typ = f" [{p['type']}]" if p.get("type") else ""
            line = f"     \u2691 {p['km']:>5.1f} km  {p['name']}{typ}"
            L.append(line)
            if p.get("desc"):
                L.append(f"          {p['desc']}")
            continue
        h = e["d"]; i = idx_of[id(h)]
        nxt = seq[i + 1] if i + 1 < len(seq) else None
        arrow = P["arrow"].format(kp=nxt["kp"]) if nxt else P["to_finish"]
        anchor = ""
        if nxt:
            to_next = bearing((h["lat"], h["lon"]), (nxt["lat"], nxt["lon"]))
            to_dest = bearing((h["lat"], h["lon"]), finish_pt)
            c = cardinal_word(to_next, lang)
            if angle_diff(to_next, to_dest) > diverge_deg:
                anchor = P["swings"].format(c=c, dest=dest)
            elif i == 0 or h["kp"] in rejoin:
                anchor = P["toward"].format(c=c, dest=dest)
        else:
            anchor = P["finish"].format(dest=dest, km=crow_km((h["lat"], h["lon"]), finish_pt))
        tail = f"   {anchor}" if anchor else ""
        L.append(f"{h['kp']:>3}  {h['km']:>5.1f} km   {arrow}{tail}")
        if h["kp"] in gap_after:
            g = gap_after[h["kp"]]
            L.append("        " + P["leave"].format(span=g["b_km"] - g["a_km"], to=g["to"]))
            L.append("        " + P["toadd"])
    return "\n".join(L)

def _h(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

LOC = {
    "en": {"trouble":"Trouble en route?","callship":"Call your ship:","scan":"scan for GPS",
           "today":"Today\u2019s route","along":"Along the way","start":"START","finish":"FINISH","day":"DAY"},
    "nl": {"trouble":"Problemen onderweg?","callship":"Bel je schip:","scan":"scan voor GPS",
           "today":"Route van vandaag","along":"Onderweg","start":"START","finish":"AANKOMST","day":"DAG"},
    "de": {"trouble":"Probleme unterwegs?","callship":"Rufen Sie Ihr Schiff an:","scan":"f\u00fcr GPS scannen",
           "today":"Heutige Route","along":"Unterwegs","start":"START","finish":"ZIEL","day":"TAG"},
    "fr": {"trouble":"Un probl\u00e8me en route\u202f?","callship":"Appelez votre bateau\u202f:","scan":"scanner pour GPS",
           "today":"Itin\u00e9raire du jour","along":"En chemin","start":"D\u00c9PART","finish":"ARRIV\u00c9E","day":"JOUR"},
}
DAYWORD = {k: v["day"] for k, v in LOC.items()}

# ferry / veerpont glyph for the knooppunt ribbon -- the real brand icon
# (Transport/Ferry.svg from the BBT glyph set), recoloured to currentColor, mirrored
# from route_builder.html's FERRY_ICON so a CLI/cockpit-built sheet matches exactly.
FERRY_ICON_HTML = ('<span class="ferry-boat" title="veerpont"><svg viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 10.189V14"/><path d="M12 2v3"/><path d="M19 13V7a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v6"/>'
    '<path d="M19.38 20A11.6 11.6 0 0 0 21 14l-8.188-3.639a2 2 0 0 0-1.624 0L3 14a11.6 11.6 0 0 0 2.81 7.76"/>'
    '<path d="M2 21c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1s1.2 1 2.5 1c2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/>'
    '</svg></span>')

def render_timeline_html(seq, origin, dest):
    """The knooppunt ribbon: .tl-track > .node(.dot .km .town). Matches route_note_template.html.
    origin/dest are placed as the town on the first/last node. A node with h["ferry"] set
    (see apply_ferry_flags) renders the same dashed-connector + boat glyph the
    route_builder.html ribbon's manual toggle draws."""
    if not seq:
        return '<div class="tl-track" data-rows="2"><div class="node"><span class="dot">–</span></div></div>'
    rows = max(2, min(6, -(-len(seq) // 8)))   # ceil(n/8), clamped to the CSS tiers
    cells = []
    for i, h in enumerate(seq):
        end = " end" if i == len(seq) - 1 else ""
        town = origin if i == 0 else (dest if i == len(seq) - 1 else "")
        townspan = f'<span class="town">{_h(town)}</span>' if town else ""
        is_ferry = i > 0 and h.get("ferry")
        ferry_cls = " ferry" if is_ferry else ""
        ferry_icon = FERRY_ICON_HTML if is_ferry else ""
        cells.append(f'<div class="node{ferry_cls}">{ferry_icon}<span class="dot{end}">{_h(h["kp"])}</span>'
                     f'<span class="km">{h["km"]:.1f}</span>{townspan}</div>')
    return f'<div class="tl-track" data-rows="{rows}">' + "".join(cells) + "</div>"

def render_route_draft(seq, gaps, pois, lang):
    """A plain node-sequence draft for the {{ROUTE}} region — a scaffold to rewrite, not final prose.
    Off-network stretches get a cue line each, with a placeholder rather than invented directions."""
    if not seq:
        return ""
    nodes = " &middot; ".join(_h(h["kp"]) for h in seq)
    stops = ", ".join(_h(p["name"]) for p in pois) if pois else ""
    P = PHR[lang]
    out = [f'<p class="rk-draft">{P["subtitle"]} (concept): <span class="jct">kp {nodes}</span>.'
           + (f' Stops: {stops}.' if stops else "") + "</p>"]
    for g in gaps:
        span = g["b_km"] - g["a_km"]
        if g["kind"] == "start":
            txt = f'Start: follow signs to <span class="jct">kp {_h(g["to"])}</span> ({span:.1f} km off-network) — add directions —'
        elif g["kind"] == "end":
            txt = f'After <span class="jct">kp {_h(g["from"])}</span>, leave the network for {span:.1f} km to the finish — add directions —'
        else:
            txt = (f'At <span class="jct">kp {_h(g["from"])}</span>, leave the node network for {span:.1f} km, '
                   f'rejoining at <span class="jct">kp {_h(g["to"])}</span> — add directions —')
        out.append(f'<p class="rk-draft">{txt}</p>')
    return "\n".join(out)

BUILTIN_TEMPLATE = ('<!doctype html><html lang="{{LANG}}"><head><meta charset="utf-8">'
    '<title>{{TITLE}}</title><style>body{font:18px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:52rem}'
    'h1{color:#043D7D;border-bottom:3px solid #043D7D}.day{color:#0E55A6;font-weight:800;letter-spacing:3px}'
    '.tl-track{display:flex;flex-wrap:wrap;gap:14px}.node{text-align:center}'
    '.dot{display:inline-flex;width:40px;height:40px;align-items:center;justify-content:center;'
    'background:#0E55A6;color:#fff;border-radius:50%;font-weight:700}.dot.end{background:#043D7D}'
    '.km{display:block;font-size:11px;color:#7a8394}.dist{color:#40D9AB;font-weight:700}</style></head><body>'
    '<p class="day">{{DAY}}</p><h1>{{TITLE}}</h1><p class="dist">{{DISTANCE}}</p>'
    '<div class="timeline">{{TIMELINE}}</div><div class="route">{{ROUTE}}</div>'
    '<div class="info">{{HIGHLIGHTS}}</div><p>{{START}} &rarr; {{END}}</p></body></html>')

def find_template(explicit=None):
    """explicit path -> CWD -> next to this file -> built-in fallback."""
    for cand in [explicit, os.path.join(os.getcwd(), "route_note_template.html"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "route_note_template.html")]:
        if cand and os.path.exists(cand):
            return open(cand, encoding="utf-8").read()
    return BUILTIN_TEMPLATE

def render_html(origin, dest, total_km, seq, gaps, pois, finish_pt, diverge_deg, lang,
                template=None, day="", route_pill="", ship="", highlights="",
                qr="", qr_code=""):
    """Fill the house template. Frame tokens are generated; the middle
    (route prose, highlights, ship, QR) is left blank for hand-authoring."""
    tpl = template if (template and template.lstrip().startswith("<")) else find_template(template)
    loc = LOC.get(lang, LOC["en"])
    repl = {
        "{{LANG}}": lang,
        "{{TITLE}}": f'{_h(origin)} &#8594; {_h(dest)}',
        "{{DAY}}": _h(day),
        "{{DISTANCE}}": f"{total_km:.0f} km",
        "{{TIMELINE}}": render_timeline_html(seq, origin, dest),
        "{{ROUTE}}": render_route_draft(seq, gaps, pois, lang),
        "{{HIGHLIGHTS}}": highlights or "",
        "{{ROUTE_PILL}}": _h(route_pill),
        "{{SHIP}}": _h(ship),
        "{{QR}}": qr or "",
        "{{QR_CODE}}": _h(qr_code),
        "{{START}}": _h(origin),
        "{{END}}": _h(dest),
        "{{L_TROUBLE}}": _h(loc["trouble"]), "{{L_CALLSHIP}}": _h(loc["callship"]),
        "{{L_SCAN}}": _h(loc["scan"]), "{{L_TODAY}}": _h(loc["today"]),
        "{{L_ALONG}}": _h(loc["along"]), "{{L_START}}": _h(loc["start"]),
        "{{L_FINISH}}": _h(loc["finish"]),
    }
    out = tpl
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def sidecar_json_str(pois):
    data = [{"name": p["name"], "type": p.get("type", ""), "lat": p["lat"], "lon": p["lon"],
             "desc": p.get("desc", "")} for p in pois]
    return json.dumps(data, ensure_ascii=False, indent=2)

def gpx_str(name, pts, pois):
    def esc(s): return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="routekit" xmlns="http://www.topografix.com/GPX/1/1">',
             f"<metadata><name>{esc(name)}</name></metadata>"]
    for p in pois:
        lines.append(f'<wpt lat="{p["lat"]:.6f}" lon="{p["lon"]:.6f}">'
                     f'<name>{esc(p["name"])}</name>'
                     f'<cmt>{esc(p.get("type",""))}</cmt>'
                     f'<desc>{esc(p.get("desc",""))}</desc></wpt>')
    lines.append(f"<trk><name>{esc(name)}</name><trkseg>")
    for la, lo in pts:
        lines.append(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"></trkpt>')
    lines += ["</trkseg></trk>", "</gpx>"]
    return "\n".join(lines)

def _iso(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")

def tcx_str(name, pts, cum, pois, lat0, xy):
    """Emit a Garmin Course. Synthesizes monotonic times (Garmin requires them);
    each CoursePoint borrows the time of its nearest trackpoint."""
    def esc(s): return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    base = datetime(2000, 1, 1, tzinfo=timezone.utc)
    times = [base + timedelta(seconds=i) for i in range(len(pts))]  # 1 pt/sec placeholder
    total_m = cum[-1]
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">',
         "<Courses><Course>", f"<Name>{esc(name[:15])}</Name>",
         f"<Lap><TotalTimeSeconds>{len(pts)-1}</TotalTimeSeconds>"
         f"<DistanceMeters>{total_m:.1f}</DistanceMeters>"
         "<BeginPosition><LatitudeDegrees>%.6f</LatitudeDegrees><LongitudeDegrees>%.6f</LongitudeDegrees></BeginPosition>"
         "<EndPosition><LatitudeDegrees>%.6f</LatitudeDegrees><LongitudeDegrees>%.6f</LongitudeDegrees></EndPosition>"
         "<Intensity>Active</Intensity></Lap>"
         % (pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]),
         "<Track>"]
    for i, (la, lo) in enumerate(pts):
        L.append(f"<Trackpoint><Time>{_iso(times[i])}</Time>"
                 f"<Position><LatitudeDegrees>{la:.6f}</LatitudeDegrees>"
                 f"<LongitudeDegrees>{lo:.6f}</LongitudeDegrees></Position>"
                 f"<DistanceMeters>{cum[i]:.1f}</DistanceMeters></Trackpoint>")
    L.append("</Track>")
    for p in pois:
        # nearest trackpoint index for this POI -> its time
        px, py = to_xy(p["lat"], p["lon"], lat0)
        bi = min(range(len(xy)), key=lambda i: (xy[i][0] - px) ** 2 + (xy[i][1] - py) ** 2)
        # Garmin caps CoursePoint Name at 10 chars; keep the full name in Notes so it's never lost.
        full = p["name"]
        note = p.get("desc") or ""
        notes = f"{full} — {note}" if (len(full) > 10 and note) else (full if len(full) > 10 else (note or full))
        L.append(f"<CoursePoint><Name>{esc(full[:10])}</Name>"
                 f"<Time>{_iso(times[bi])}</Time>"
                 f"<Position><LatitudeDegrees>{p['lat']:.6f}</LatitudeDegrees>"
                 f"<LongitudeDegrees>{p['lon']:.6f}</LongitudeDegrees></Position>"
                 f"<PointType>Generic</PointType>"
                 f"<Notes>{esc(notes)}</Notes></CoursePoint>")
    L += ["</Course></Courses>", "</TrainingCenterDatabase>"]
    return "\n".join(L)

# ======================================================================================
# build — the foolproof front door
# ======================================================================================
def gather_pois(route_path, wpts, master, explicit_sidecar, lang, xy, cum, lat0, cut):
    """Priority: explicit --pois > auto sidecar > master(pick) > the file's own waypoints."""
    raw, origin_label = [], ""
    if explicit_sidecar and os.path.exists(explicit_sidecar):
        raw, origin_label = pois_from_sidecar(explicit_sidecar, lang), "sidecar(--pois)"
    else:
        auto = sidecar_path(route_path) if route_path else None
        if auto:
            raw, origin_label = pois_from_sidecar(auto, lang), "sidecar(auto)"
        elif master:
            raw, origin_label = pois_from_master(master, lang), "master"
        elif wpts:
            raw, origin_label = pois_from_waypoints(wpts, lang), "waypoints"
    placed = place_along(raw, xy, cum, lat0, cut) if raw else []
    return placed, origin_label

def build_deliverables(source, name, *, langs=("en",), master=None, pois_sidecar=None,
                       formats=("txt", "html", "json", "gpx", "tcx", "frame"),
                       threshold=30.0, gap_km=2.5, diverge_deg=75.0, cut=250.0,
                       cache_path=DEFAULT_CACHE, offline=False, refresh=False, route_path=None,
                       template=None):
    """Pure engine — no disk writes. Import this from your dashboard.

    source      a filesystem path, OR raw .gpx/.tcx content as bytes/str (an upload)
    name        filename (used for the title and to detect .gpx vs .tcx)
    returns     (meta: dict, files: dict[filename -> str content])
    """
    formats = set(formats)
    pts, xy, cum, lat0, wpts = ingest(source, name)
    total_km = cum[-1] / 1000
    finish_pt = pts[-1]
    origin, dest = parse_title(name or route_path or "route")
    nodes, ksource = get_nodes(pts, cache_path, offline, refresh)
    seq = snap_coords(nodes, xy, cum, lat0, threshold)
    ferries = get_ferries(pts, xy, cum, lat0, offline)
    apply_ferry_flags(seq, ferries)
    gaps = find_gaps(seq, total_km, gap_km)
    safe = re.sub(r"[^\w\-]+", "_", f"{origin}_{dest}").strip("_") or "route"

    files = {}
    base_pois, poi_src = gather_pois(route_path, wpts, master, pois_sidecar, langs[0], xy, cum, lat0, cut)
    _m = re.search(r"\b(?:d|day|dag|tag)\s*0*(\d{1,2})\b", (name or ""), re.I)
    day_num = int(_m.group(1)) if _m else None
    if "gpx" in formats:
        files[safe + ".gpx"] = gpx_str(f"{origin} - {dest}", pts, base_pois)
    if "tcx" in formats:
        files[safe + ".tcx"] = tcx_str(safe, pts, cum, base_pois, lat0, xy)
    if "json" in formats:
        files[safe + ".pois.json"] = sidecar_json_str(base_pois)
    if "frame" in formats:
        # the clean hand-off the builder reads: frame fields + the node ribbon + off-network gaps
        nodes = [{"kp": h["kp"], "km": round(h["km"], 1),
                  "town": (origin if i == 0 else dest if i == len(seq) - 1 else ""),
                  "ferry": bool(h.get("ferry", False))}
                 for i, h in enumerate(seq)]
        frame_gaps = [{"kind": g["kind"], "from": g["from"], "to": g["to"],
                       "from_km": round(g["a_km"], 1), "to_km": round(g["b_km"], 1),
                       "span_km": round(g["b_km"] - g["a_km"], 1)} for g in gaps]
        frame = {"lang": langs[0], "day_num": day_num, "origin": origin, "dest": dest,
                 "title": f"{origin} → {dest}", "distance_km": round(total_km, 1),
                 "nodes": nodes, "gaps": frame_gaps, "start": origin, "end": dest}
        files[safe + ".frame.json"] = json.dumps(frame, ensure_ascii=False, indent=2)
    for lang in langs:
        pois, _ = gather_pois(route_path, wpts, master, pois_sidecar, lang, xy, cum, lat0, cut)
        tag = "" if lang == "en" else f".{lang}"
        if "txt" in formats:
            body = render_notes(origin, dest, total_km, seq, gaps, pois, finish_pt, diverge_deg, lang)
            if ksource in ("offline-uncached", "no-network") and not seq:
                body += "\n\n[" + PHR[lang]["kp_unavail"] + "]"
            files[f"{safe}{tag}.txt"] = body + "\n"
        if "html" in formats:
            day = f"{DAYWORD.get(lang, 'DAY')} {day_num:02d}" if day_num is not None else ""
            files[f"{safe}{tag}.html"] = render_html(
                origin, dest, total_km, seq, gaps, pois, finish_pt, diverge_deg, lang,
                template=template, day=day)

    meta = {"safe": safe, "origin": origin, "dest": dest, "distance_km": round(total_km, 1),
            "nodes": len(seq), "gaps": len(gaps), "ferries": sum(1 for h in seq if h.get("ferry")),
            "pois": len(base_pois), "poi_source": poi_src, "kp_source": ksource}
    return meta, files

def build_one(path, langs, master, explicit_sidecar, out_dir, formats,
              threshold, gap_km, diverge_deg, cut, cache_path, offline, refresh):
    """CLI helper: run the engine, then write its files to out_dir."""
    meta, files = build_deliverables(
        path, os.path.basename(path), langs=langs, master=master, pois_sidecar=explicit_sidecar,
        formats=formats, threshold=threshold, gap_km=gap_km, diverge_deg=diverge_deg, cut=cut,
        cache_path=cache_path, offline=offline, refresh=refresh, route_path=path)
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for fn, content in files.items():
        fp = os.path.join(out_dir, fn)
        open(fp, "w", encoding="utf-8").write(content)
        made.append(fp)
    return meta, made

def cmd_build(a):
    langs = list(LANGS) if a.lang == "all" else [a.lang]
    formats = set(a.formats.split(","))
    files = []
    for item in a.inputs:
        if os.path.isdir(item):
            files += [f for f in glob.glob(os.path.join(item, "*"))
                      if f.lower().endswith((".gpx", ".tcx"))]
        else:
            files.append(item)
    files = sorted(set(files))
    if not files:
        sys.exit("Nothing to build. Pass a .gpx/.tcx file or a folder.")
    ok = fail = 0
    for f in files:
        try:
            meta, made = build_one(
                f, langs, a.master, a.pois, a.out, formats,
                a.threshold, a.gap_km, a.diverge_deg, a.cut, a.cache, a.offline, a.refresh)
            psrc_tag = f" \u00b7 POIs:{meta['poi_source']}" if meta["pois"] else ""
            ferry_tag = f" \u00b7 {meta['ferries']} ferr{'y' if meta['ferries']==1 else 'ies'}" if meta["ferries"] else ""
            print(f"  \u2713 {meta['safe']}  ({meta['distance_km']:.1f} km \u00b7 {meta['nodes']} nodes \u00b7 "
                  f"{meta['gaps']} gaps{ferry_tag} \u00b7 {meta['pois']} POIs{psrc_tag} \u00b7 kp:{meta['kp_source']})")
            ok += 1
        except Exception as ex:
            print(f"  ! {os.path.basename(f)} failed: {ex}")
            fail += 1
    print(f"\nWrote {ok} route(s) to {a.out}/" + (f"  ({fail} failed)" if fail else ""))

# ======================================================================================
# poi — expert POI-library subcommands  [from poi_master, unchanged behaviour]
# ======================================================================================
def gpx_track(path):
    r = ET.parse(path).getroot()
    return [(float(p.get("lat")), float(p.get("lon"))) for p in r.findall(".//g:trkpt", GPX_NS)]

def gpx_waypoints(path):
    r = ET.parse(path).getroot()
    out = []
    for w in r.findall("g:wpt", GPX_NS):
        out.append({"pt": (float(w.get("lat")), float(w.get("lon"))),
                    "name": w.findtext("g:name", "", GPX_NS),
                    "type": w.findtext("g:cmt", "", GPX_NS),
                    "desc": (w.findtext("g:desc", "", GPX_NS) or "").replace("\n", " ").strip()})
    return out

def poi_merge(a):
    master = json.load(open(a.master, encoding="utf-8")) if os.path.exists(a.master) else []
    index = {p["id"] for p in master}
    added = 0
    for gpx in a.gpx:
        for w in gpx_waypoints(gpx):
            sid = slug(w["name"])
            near = next((p for p in master if hav((p["lat"], p["lon"]), w["pt"]) < 30), None)
            if sid in index or near:
                continue
            master.append({"id": sid, "lat": w["pt"][0], "lon": w["pt"][1], "type": w["type"],
                           "name": {"en": w["name"], "nl": "", "de": ""},
                           "desc": {"en": w["desc"], "nl": "", "de": ""},
                           "source": gpx.split("/")[-1]})
            index.add(sid); added += 1
    json.dump(master, open(a.master, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    todo = sum(1 for p in master if not p["name"]["nl"] or not p["name"]["de"])
    print(f"merged {added} new POIs -> {len(master)} total ({todo} still need NL/DE)")

def poi_pick(a):
    master = json.load(open(a.master, encoding="utf-8"))
    trk = gpx_track(a.gpx)
    cum = [0.0]
    for i in range(1, len(trk)):
        cum.append(cum[-1] + hav(trk[i - 1], trk[i]))
    hits = []
    for p in master:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        best, bi = 1e18, 0
        for i, q in enumerate(trk):
            d = hav((p["lat"], p["lon"]), q)
            if d < best:
                best, bi = d, i
        if best <= a.cut:
            hits.append((cum[bi], best, p))
    hits.sort(key=lambda x: x[0])
    print(f"# {a.gpx.split('/')[-1]}: {len(hits)}/{len(master)} master POIs within {a.cut:.0f} m (lang={a.lang})\n")
    for km, off, p in hits:
        print(f"  {km/1000:5.1f} km  {(p['name'].get(a.lang) or p['name']['en']):34.34} [{p['type']}]  {round(off)} m")
    if a.emit:
        sidecar = [{"name": p["name"].get(a.lang) or p["name"]["en"], "type": p["type"],
                    "lat": p["lat"], "lon": p["lon"],
                    "desc": p["desc"].get(a.lang) or p["desc"].get("en", "")} for _, _, p in hits]
        json.dump(sidecar, open(a.emit, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n-> wrote {a.emit} ({len(sidecar)} POIs, {a.lang})")

def poi_view(a):
    master = json.load(open(a.master, encoding="utf-8"))
    cols = ["id", "type", "locality", "lat", "lon", "name_en", "name_nl", "name_de",
            "desc_en", "desc_nl", "desc_de", "source", "todo"]
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for p in master:
            todo = []
            if p.get("lat") is None: todo.append("coords")
            if not p["name"].get("nl") or not p["name"].get("de"): todo.append("translate")
            w.writerow({"id": p["id"], "type": p.get("type", ""), "locality": p.get("locality", ""),
                        "lat": p.get("lat", ""), "lon": p.get("lon", ""),
                        "name_en": p["name"].get("en", ""), "name_nl": p["name"].get("nl", ""),
                        "name_de": p["name"].get("de", ""), "desc_en": p["desc"].get("en", ""),
                        "desc_nl": p["desc"].get("nl", ""), "desc_de": p["desc"].get("de", ""),
                        "source": p.get("source", ""), "todo": "+".join(todo)})
    print(f"wrote {a.out}: {len(master)} rows")

def poi_harvest(a):
    paths = glob.glob(os.path.join(a.path, "*.json")) if os.path.isdir(a.path) else glob.glob(a.path)
    POI_KEYS = ("points_of_interest", "pois", "poi")
    def find_pois(obj):
        if isinstance(obj, dict):
            for k in POI_KEYS:
                if isinstance(obj.get(k), list):
                    return obj[k]
            for v in obj.values():
                r = find_pois(v)
                if r: return r
        return []
    def field(p, *names):
        for n in names:
            if p.get(n) not in (None, ""): return p[n]
        return None
    if a.inspect:
        for fp in paths:
            try: pois = find_pois(json.load(open(fp, encoding="utf-8")))
            except Exception: continue
            if pois:
                print(f"{os.path.basename(fp)}: {len(pois)} POIs; first keys = {sorted(pois[0].keys())}")
                return
        print("No POIs found in any file scanned."); return
    master = json.load(open(a.master, encoding="utf-8")) if os.path.exists(a.master) else []
    index = {p["id"] for p in master}
    added = files_with = 0
    for fp in paths:
        try: pois = find_pois(json.load(open(fp, encoding="utf-8")))
        except Exception: continue
        if pois: files_with += 1
        for p in pois:
            name = field(p, "name"); lat = field(p, "lat", "latitude"); lon = field(p, "lng", "lon", "longitude")
            if not name or lat is None or lon is None: continue
            lat, lon = float(lat), float(lon); sid = slug(name)
            near = next((q for q in master if q.get("lat") is not None
                         and hav((q["lat"], q["lon"]), (lat, lon)) < 30), None)
            if sid in index or near: continue
            master.append({"id": sid, "lat": lat, "lon": lon, "locality": "",
                           "type": field(p, "type_name", "type") or "",
                           "name": {"en": name, "nl": "", "de": ""},
                           "desc": {"en": field(p, "description", "desc") or "", "nl": "", "de": ""},
                           "source": os.path.basename(fp)})
            index.add(sid); added += 1
    json.dump(master, open(a.master, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"harvested {added} new POIs from {files_with} file(s) ({len(paths)} scanned) -> master {len(master)} total")

# ======================================================================================
def main():
    ap = argparse.ArgumentParser(prog="routekit", description="GPX/TCX in, all guest deliverables out.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="GPX/TCX -> notes(txt/html) + POIs(json) + track(gpx/tcx)")
    b.add_argument("inputs", nargs="+", help="one or more .gpx/.tcx files, or a folder")
    b.add_argument("--lang", choices=["en", "nl", "de", "all"], default="en")
    b.add_argument("--master", help="POI master to pull POIs from (if no sidecar/waypoints)")
    b.add_argument("--pois", help="explicit POI sidecar for ALL inputs this run")
    b.add_argument("--out", default="out")
    b.add_argument("--formats", default="txt,html,json,gpx,tcx,frame",
                   help="comma list from: txt,html,json,gpx,tcx (default all)")
    b.add_argument("--threshold", type=float, default=30.0)
    b.add_argument("--gap-km", type=float, default=2.5)
    b.add_argument("--diverge-deg", type=float, default=75.0)
    b.add_argument("--cut", type=float, default=250.0)
    b.add_argument("--cache", default=DEFAULT_CACHE)
    b.add_argument("--no-cache", action="store_true")
    b.add_argument("--offline", action="store_true")
    b.add_argument("--refresh", action="store_true")
    b.set_defaults(fn=cmd_build)

    p = sub.add_parser("poi", help="expert POI-library operations")
    psub = p.add_subparsers(dest="poicmd", required=True)
    pm = psub.add_parser("merge"); pm.add_argument("master"); pm.add_argument("gpx", nargs="+"); pm.set_defaults(fn=poi_merge)
    pp = psub.add_parser("pick"); pp.add_argument("master"); pp.add_argument("gpx")
    pp.add_argument("--cut", type=float, default=250.0); pp.add_argument("--lang", default="en")
    pp.add_argument("--emit"); pp.set_defaults(fn=poi_pick)
    pv = psub.add_parser("view"); pv.add_argument("master"); pv.add_argument("--out", default="master_view.csv"); pv.set_defaults(fn=poi_view)
    ph = psub.add_parser("harvest"); ph.add_argument("master"); ph.add_argument("path")
    ph.add_argument("--inspect", action="store_true"); ph.set_defaults(fn=poi_harvest)

    a = ap.parse_args()
    if getattr(a, "cmd", None) == "build" and a.no_cache:
        a.cache = None
    a.fn(a)

if __name__ == "__main__":
    main()
