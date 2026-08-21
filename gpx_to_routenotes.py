#!/usr/bin/env python3
"""
gpx_to_routenotes.py
A text route-notes generator — the notes-oriented sibling of
gpx_to_knooppunten.py. Same GPX + OSM node network underneath, but instead of a
bare node list it produces a route sheet in one of two modes:

  --mode dependent    rider has a device following the track. Notes are light:
                      node sequence, distance, and honest "off-network" markers.
                      (Over-instructing a GPS user is just noise.)

  --mode independent  signs & paper only. Adds a HEADING ANCHOR at the points
                      that matter — the global "which way is the destination"
                      sense the node signs leave out — and bounds every
                      off-network gap with the nodes either side of it.

It reuses the tested core of gpx_to_knooppunten.py (GPX reading, Overpass fetch,
offline cache, snapping), so keep both files in the same folder. Detailed
turn-by-turn inside a gap is deliberately NOT invented here — a gap is marked
honestly; filling it with real directions is a separate, later step.

USAGE
  python3 gpx_to_routenotes.py route.gpx                 # independent (default)
  python3 gpx_to_routenotes.py route.gpx --mode dependent
  python3 gpx_to_routenotes.py route.gpx --offline       # cache only, no network
  python3 gpx_to_routenotes.py route.gpx --gap-km 2.0 --diverge-deg 70
"""

import argparse, math, os, re, sys, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpx_to_knooppunten as knp   # shared core: read_track, to_xy, seg_project, get_nodes

# ---------- bearings / headings ----------
def bearing(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def cardinal(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(deg / 45) % 8]

def angle_diff(a, b):
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d

def crow_km(a, b):
    lat0 = (a[0] + b[0]) / 2
    ax, ay = knp.to_xy(a[0], a[1], lat0); bx, by = knp.to_xy(b[0], b[1], lat0)
    return math.hypot(ax - bx, ay - by) / 1000

# ---------- title from filename (RWGPS style "Origin_-_Dest_51_km_ENG_2026") ----------
def parse_title(path):
    name = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
    origin, rest = (name.split(" - ", 1) + [""])[:2] if " - " in name else (name, "")
    m = re.match(r"(.*?)\s+\d+\s*km", rest)
    dest = (m.group(1) if m else rest).strip()
    origin = re.sub(r"\s*\(.*?\)", "", origin).strip()
    return origin or "Start", dest or "Finish"

# ---------- snap that keeps node coordinates ----------
def snap_coords(nodes, xy, cum, lat0, threshold):
    hits = []
    for nid, ref, nlat, nlon in nodes:
        nx, ny = knp.to_xy(nlat, nlon, lat0)
        best_d, best_along = 1e18, 0.0
        for i in range(len(xy) - 1):
            d, t = knp.seg_project(nx, ny, xy[i][0], xy[i][1], xy[i+1][0], xy[i+1][1])
            if d < best_d:
                best_d, best_along = d, cum[i] + t * (cum[i+1] - cum[i])
        if best_d <= threshold:
            hits.append({"kp": ref, "km": round(best_along/1000, 2),
                         "offset_m": round(best_d, 1), "lat": nlat, "lon": nlon})
    hits.sort(key=lambda h: h["km"])
    seq = []
    for h in hits:
        if not seq or seq[-1]["kp"] != h["kp"]:
            seq.append(h)
    return seq

# ---------- find off-network gaps ----------
def find_gaps(seq, total_km, gap_km, edge_km=0.8):
    gaps = []
    if seq and seq[0]["km"] > max(gap_km, edge_km):
        gaps.append({"kind": "start", "from": "START", "to": seq[0]["kp"],
                     "a_km": 0.0, "b_km": seq[0]["km"]})
    for i in range(len(seq) - 1):
        run = seq[i+1]["km"] - seq[i]["km"]
        if run > gap_km:
            gaps.append({"kind": "mid", "from": seq[i]["kp"], "to": seq[i+1]["kp"],
                         "a_km": seq[i]["km"], "b_km": seq[i+1]["km"]})
    if seq and (total_km - seq[-1]["km"]) > max(gap_km, edge_km):
        gaps.append({"kind": "end", "from": seq[-1]["kp"], "to": "FINISH",
                     "a_km": seq[-1]["km"], "b_km": total_km})
    return gaps

# ---------- renderers ----------
def render_dependent(origin, dest, total_km, seq, gaps):
    L = [f"{origin.upper()} \u2192 {dest.upper()}    {total_km:.1f} km    \u00b7 device following track \u00b7", ""]
    L.append("Nodes: " + " \u00b7 ".join(h["kp"] for h in seq) if seq else "Nodes: (none on network)")
    L.append("")
    if gaps:
        L.append(f"Off-network: {len(gaps)} stretch(es) \u2014 expected, stay on the line:")
        for g in gaps:
            span = g["b_km"] - g["a_km"]
            L.append(f"  \u2022 {span:.1f} km   {g['from']} \u2192 {g['to']}   (from {g['a_km']:.1f} km)")
    else:
        L.append("Off-network: none \u2014 whole route is on the node network.")
    return "\n".join(L)

def render_independent(origin, dest, total_km, seq, gaps, finish_pt, diverge_deg):
    L = [f"{origin.upper()} \u2192 {dest.upper()}    {total_km:.1f} km    \u00b7 signs & notes only \u00b7", ""]
    if not seq:
        L.append("No knooppunten on this route \u2014 off-network the whole way.")
        return "\n".join(L)
    L.append(f"START \u2014 follow rcn signs to node {seq[0]['kp']}")

    gap_after = {g["from"]: g for g in gaps if g["kind"] in ("mid", "end")}
    rejoin_nodes = {g["to"] for g in gaps if g["kind"] in ("start", "mid")}

    for i, h in enumerate(seq):
        nxt = seq[i+1] if i+1 < len(seq) else None
        arrow = f"\u2192 {nxt['kp']}" if nxt else "\u2192 FINISH"
        # heading anchor is "earned" at: first node, right after a rejoin, or a hard divergence
        anchor = ""
        if nxt:
            to_next = bearing((h["lat"], h["lon"]), (nxt["lat"], nxt["lon"]))
            to_dest = bearing((h["lat"], h["lon"]), finish_pt)
            div = angle_diff(to_next, to_dest)
            earned = (i == 0) or (h["kp"] in rejoin_nodes)
            if div > diverge_deg:
                anchor = f"head {cardinal(to_next)} \u2014 route swings away from {dest} on this stretch"
            elif earned:
                anchor = f"head {cardinal(to_next)} toward {dest}"
        else:
            anchor = f"{dest} {crow_km((h['lat'],h['lon']), finish_pt):.1f} km \u2014 follow \u201cCentrum\u201d"
        tail = f"   {anchor}" if anchor else ""
        L.append(f"{h['kp']:>3}  {h['km']:>5.1f} km   {arrow}{tail}")
        # emit a gap block right after this node if one starts here
        if h["kp"] in gap_after:
            g = gap_after[h["kp"]]; span = g["b_km"] - g["a_km"]
            L.append(f"        LEAVE NETWORK \u00b7 {span:.1f} km \u00b7 no signs \u00b7 rejoin at {g['to']}")
            L.append(f"        (written directions for this stretch: to be added)")
    return "\n".join(L)

def main():
    ap = argparse.ArgumentParser(description="Text route notes from a GPX track + OSM node network.")
    ap.add_argument("gpx")
    ap.add_argument("--mode", choices=["dependent", "independent"], default="independent")
    ap.add_argument("--threshold", type=float, default=30.0, help="node-to-track snap distance, m (default 30)")
    ap.add_argument("--gap-km", type=float, default=2.5, help="a run longer than this with no node is a gap (default 2.5)")
    ap.add_argument("--diverge-deg", type=float, default=75.0, help="heading-vs-destination angle that triggers an anchor note (default 75)")
    # cache passthrough to the shared core
    ap.add_argument("--cache", default=knp.DEFAULT_CACHE)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    if args.no_cache:
        args.cache = None

    pts, xy, cum, lat0 = knp.read_track(args.gpx)
    total_km = cum[-1] / 1000
    finish_pt = pts[-1]
    nodes, source = knp.get_nodes(pts, args)
    seq = snap_coords(nodes, xy, cum, lat0, args.threshold)
    gaps = find_gaps(seq, total_km, args.gap_km)
    origin, dest = parse_title(args.gpx)

    if args.mode == "dependent":
        out = render_dependent(origin, dest, total_km, seq, gaps)
    else:
        out = render_independent(origin, dest, total_km, seq, gaps, finish_pt, args.diverge_deg)

    print("\n" + out + "\n")
    print(f"[{len(seq)} nodes \u00b7 {len(gaps)} gap(s) \u00b7 source: {source}]")

if __name__ == "__main__":
    main()
