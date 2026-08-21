#!/usr/bin/env python3
"""
route_sheet.py — the complete route-notes tool.

Reads one cached RideWithGPS route (raw JSON from rwgps_pull.py) and produces a
guest route sheet in two modes, merging every source into one document:

  author course points  -> the turn-by-turn cue backbone (human-written)
  cue text              -> the knooppunten sequence (authors name "junction NN")
  points of interest    -> stops (lodging/coffee/viewpoint...), placed by distance
  track                 -> total distance, POI positioning

  --mode independent  signs & paper only: the full author cue sheet + junctions
                      + stops. Standalone; needs no device.
  --mode dependent    device following the line: node sequence + stops + distance.
                      Light — the turns are the device's job.

Offline: runs entirely on the cached JSON, no network.

USAGE
  python3 route_sheet.py route_cache/29122506.json
  python3 route_sheet.py route_cache/29122506.json --mode dependent
"""

import argparse, html, json, math, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rwgps

GLYPH = {"lodging": "dock/stay", "coffee": "coffee", "food": "food",
         "viewpoint": "see", "water": "water", "control": "control",
         "generic": "note", "ferry": "ferry"}

TURN_WORD = {"left": "left", "right": "right", "slight left": "left", "slight right": "right",
             "sharp left": "left", "sharp right": "right", "straight": "straight"}

def _norm(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    t = re.sub(r"junction\s+0*(\d+)", r"junction \1", t, flags=re.I)
    return (t[:1].upper() + t[1:]) if t else t

def _richness(c):
    t = (c["text"] or "").lower()
    return (("onto" in t) * 2 + ("junction" in t), len(t))

def dedupe(cues, dedupe_m):
    """Collapse near-coincident cues (GPS duplicates), keeping the richer text."""
    out = []
    for c in cues:
        if (out and isinstance(c["m"], (int, float)) and isinstance(out[-1]["m"], (int, float))
                and (c["m"] - out[-1]["m"]) <= dedupe_m):
            if _richness(c) > _richness(out[-1]):
                out[-1] = dict(c)
        else:
            out.append(dict(c))
    return out

def fold_straights(cues):
    """Fold consecutive nameless 'continue straight' cues into the current road."""
    def foldable(c):
        t = (c["text"] or "").lower()
        straight = (c.get("type") or "").lower() == "straight" or t.startswith("continue")
        return straight and "onto" not in t and "junction" not in t
    out = []
    for c in cues:
        if out and foldable(c) and foldable(out[-1]):
            out[-1]["_end_m"] = c["m"]      # extend the range, drop this line
            continue
        out.append(dict(c))
    return out

def bearing(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def cardinal(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(deg / 45) % 8]

def cluster_cues(cues, merge_m):
    """Chain cues that sit within merge_m of the previous one into clusters."""
    clusters, cur = [], []
    for c in cues:
        d = c["m"] if isinstance(c["m"], (int, float)) else None
        if cur and d is not None and isinstance(cur[-1]["m"], (int, float)) and (d - cur[-1]["m"]) <= merge_m:
            cur.append(c)
        else:
            if cur: clusters.append(cur)
            cur = [c]
    if cur: clusters.append(cur)
    return clusters

def _next_junction(cues):
    for c in cues:
        m = re.search(r"junction\s+0*(\d+)", c["text"], re.I)
        if m: return m.group(1)
    return ""

def km_span(a, b):
    return f"{a:.1f} km" if round(a, 1) == round(b, 1) else f"{a:.1f}–{b:.1f} km"

def _exit_bits(cluster, next_cue):
    last = cluster[-1]
    turn = TURN_WORD.get((last.get("type") or "").lower(), "")
    head = ""
    if next_cue and last.get("lat") is not None and next_cue.get("lat") is not None:
        head = cardinal(bearing((last["lat"], last["lon"]), (next_cue["lat"], next_cue["lon"])))
    bits = []
    if turn: bits.append(f"exit {turn}")
    if head: bits.append(f"heading {head}")
    return (" (" + ", ".join(bits) + ")") if bits else ""

def render_cluster_line(cluster, next_cue):
    """One line for a proximity cluster. Single = passthrough; multi = enter/exit summary."""
    if len(cluster) == 1:
        c = cluster[0]
        km = c["m"] / 1000 if isinstance(c["m"], (int, float)) else None
        if c.get("_end_m") and isinstance(c["_end_m"], (int, float)) and km is not None:
            return f"  {km_span(km, c['_end_m'] / 1000)}   {_norm(c['text'])}"
        kms = f"{km:5.1f} km" if km is not None else "   ? km"
        return f"  {kms}   {_norm(c['text'])}"
    km0 = cluster[0]["m"] / 1000; km1 = cluster[-1]["m"] / 1000
    entry = max(cluster[:2], key=lambda c: len(c["text"]))["text"]
    entry = _norm(re.sub(r"\s*to junction .*$", "", entry, flags=re.I)) or "Continue"
    bare = re.match(r"^(turn|bear|keep|slight|sharp|continue)\b", entry, re.I) and \
           "onto" not in entry.lower()
    body = "Several quick turns" if (bare and len(cluster) > 2) else entry
    j = _next_junction(cluster + ([next_cue] if next_cue else []))
    tail = f" toward junction {j}" if j else ""
    return f"  {km_span(km0, km1)}   {body}{_exit_bits(cluster, next_cue)}{tail}"

# ---- same-junction collapse (rule 6: trust signs between named nodes) ----
def _target_junction(c):
    m = re.search(r"junction\s+0*(\d+)", c["text"], re.I)
    return m.group(1) if m else None

def group_by_junction(cues):
    """Group consecutive cues that are all working toward the same named junction."""
    groups, cur, tgt = [], [], None
    for c in cues:
        t = _target_junction(c)
        if not cur:
            cur, tgt = [c], t
        elif t is None or t == tgt:
            cur.append(c)
            if tgt is None and t: tgt = t
        else:
            groups.append((tgt, cur)); cur, tgt = [c], t
    if cur: groups.append((tgt, cur))
    return groups

def _streets_in(group):
    out = []
    for c in group:
        for m in re.finditer(r"onto ([A-Za-zÀ-ÿ0-9'’\- ]+?)(?: to junction|$)", c["text"], re.I):
            s = m.group(1).strip()
            if s and s not in out: out.append(s)
    return out

def render_junction_group(tgt, group, next_cue):
    """A run heading to one junction -> one 'follow signs' line keeping streets + exit."""
    km0 = group[0]["m"] / 1000; km1 = group[-1]["m"] / 1000
    streets = _streets_in(group)
    via = f" via {', '.join(streets)}" if streets else ""
    nj = _next_junction([next_cue]) if next_cue else ""
    tail = f" toward junction {nj}" if nj and nj != tgt else ""
    return f"  {km_span(km0, km1)}   Follow signs to junction {tgt}{via}{_exit_bits(group, next_cue)}{tail}"

# ---- street collapse (off-network analog of same-junction collapse) ----
def _street(c):
    m = re.search(r"\bonto (.+)$", (c["text"] or "").strip(), re.I)
    return m.group(1).strip().rstrip(".") if m else None

def _is_stay(c):
    return bool(re.match(r"\s*(keep|continue)\b", c["text"] or "", re.I))

def group_by_street(cues):
    """New line for every real maneuver; only fold 'keep/continue' filler on the same road."""
    groups, cur, cur_street = [], None, None
    for c in cues:
        st = _street(c)
        stay = _is_stay(c) and (st is None or st == cur_street)
        if cur is None or not stay:
            cur = [c]; groups.append(cur); cur_street = st
        else:
            cur.append(c)
            if st: cur_street = st
    return groups

def render_street_item(group, next_first):
    first = group[0]
    km0 = first["m"] / 1000 if isinstance(first["m"], (int, float)) else None
    end_m = (next_first["m"] if next_first else group[-1].get("_end_m", group[-1]["m"]))
    text = _norm(first["text"])
    if (km0 is not None and isinstance(end_m, (int, float))):
        span = end_m / 1000 - km0
        if span >= 1.0:
            text += f" (follow {span:.1f} km)"
    return {"km": km0, "text": text}

def direction_items(cues, dedupe_m, collapse, fold):
    """Unified: list of {km, text} direction lines after all merge rules."""
    cues = dedupe(cues, dedupe_m)
    if fold:
        cues = fold_straights(cues)
    items = []
    if not collapse:
        return [{"km": (c["m"]/1000 if isinstance(c["m"], (int, float)) else None),
                 "text": _norm(c["text"])} for c in cues]
    groups = group_by_junction(cues)
    for gi, (tgt, grp) in enumerate(groups):
        nxt = groups[gi+1][1][0] if gi + 1 < len(groups) else None
        if tgt and len(grp) > 1:                       # on-network: trust signs
            _, text = _split_line(render_junction_group(tgt, grp, nxt))
            items.append({"km": grp[0]["m"]/1000 if isinstance(grp[0]["m"], (int, float)) else None,
                          "text": text})
        else:                                          # off-network: collapse by street
            sgs = group_by_street(grp)
            for si, sg in enumerate(sgs):
                nf = sgs[si+1][0] if si + 1 < len(sgs) else nxt
                items.append(render_street_item(sg, nf))
    return items

def poi_item(p):
    label = GLYPH.get((p.get("type") or "").lower(), p.get("type") or "stop")
    desc = (p.get("desc") or "").strip()
    return {"km": p["km"], "poi": True, "label": label, "name": p["name"].strip(), "desc": desc}

def merged_items(cues, pois, dedupe_m, collapse, fold):
    items = direction_items(cues, dedupe_m, collapse, fold)
    items += [poi_item(p) for p in pois if isinstance(p["km"], (int, float))]
    items.sort(key=lambda x: x["km"] if x["km"] is not None else 1e9)
    return items


def clean_title(name):
    return re.sub(r"\s+\d+\s*km.*$", "", name or "").strip() or (name or "Route")

def nodes_from_cues(cues):
    seq = []
    for c in cues:
        for m in re.finditer(r"junction\s+0*(\d+)", c["text"], re.I):
            kp = m.group(1)
            km = (c["m"] / 1000) if isinstance(c["m"], (int, float)) else None
            if not seq or seq[-1][0] != kp:
                seq.append((kp, km))
    return seq

def _km_along(lat, lon, track_ll, track_km):
    best, bkm = 1e18, None
    for (tl, to), km in zip(track_ll, track_km):
        d = (tl - lat) ** 2 + (to - lon) ** 2
        if d < best:
            best, bkm = d, km
    return bkm

def place_pois(route, pois):
    tps = route.get("track_points", [])
    track_ll = [(p.get("y"), p.get("x")) for p in tps]
    track_km = [(p.get("d") or 0) / 1000 for p in tps]
    out = []
    for p in pois:
        if p["lat"] is None or p["lon"] is None:
            continue
        km = _km_along(p["lat"], p["lon"], track_ll, track_km)
        out.append({**p, "km": km})
    out.sort(key=lambda p: (p["km"] is None, p["km"] or 0))
    return out

def poi_line(p):
    label = GLYPH.get((p.get("type") or "").lower(), p.get("type") or "stop")
    km = f"{p['km']:4.1f} km" if isinstance(p["km"], (int, float)) else "  ? km"
    return f"  {km}  [{label}] {p['name'].strip()}"

def stops_section(pois):
    have = [p for p in pois if (p.get("desc") or "").strip()]
    if not have:
        return ""
    L = ["", "STOPS"]
    for p in sorted(pois, key=lambda p: (p["km"] is None, p["km"] or 0)):
        label = GLYPH.get((p.get("type") or "").lower(), p.get("type") or "stop")
        km = f"{p['km']:.1f} km" if isinstance(p["km"], (int, float)) else "? km"
        desc = (p.get("desc") or "").strip()
        line = f"  [{label}] {p['name'].strip()} ({km})"
        if desc:
            line += f" — {re.sub(chr(92)+'s+', ' ', desc)}"
        L.append(line)
    return "\n".join(L)

def render_independent(title, total_km, cues, nodes, pois, dedupe_m, collapse_junctions, fold_straights_on):
    L = [f"{title.upper()}    {total_km:.1f} km    · signs & notes only ·", ""]
    node_str = " · ".join(k for k, _ in nodes)
    if node_str:
        L.append(f"Knooppunten: {node_str}")
        L.append("")
    L.append("DIRECTIONS")
    for it in merged_items(cues, pois, dedupe_m, collapse_junctions, fold_straights_on):
        km = f"{it['km']:5.1f} km" if isinstance(it["km"], (int, float)) else "   ? km"
        if it.get("poi"):
            desc = f" — {it['desc']}" if it["desc"] else ""
            L.append(f"  ★ {km.strip()}  [{it['label']}] {it['name']}{desc}")
        else:
            L.append(f"  {km}   {it['text']}")
    return "\n".join(L)

def render_dependent(title, total_km, nodes, pois):
    L = [f"{title.upper()}    {total_km:.1f} km    · device following track ·", ""]
    node_str = " · ".join(k for k, _ in nodes)
    L.append("Knooppunten: " + (node_str or "(none named in cues)"))
    if pois:
        L.append("")
        L.append("Stops:")
        for p in sorted(pois, key=lambda p: (p["km"] is None, p["km"] or 0)):
            L.append(poi_line(p))
        tail = stops_section(pois)
        if tail:
            L.append(tail)
    return "\n".join(L)

def _split_line(s):
    m = re.match(r"\s*([\d.]+(?:–[\d.]+)?)\s*km\s+(.*)$", s)
    return (m.group(1) + " km", m.group(2)) if m else ("", s.strip())

def build_direction_rows(cues, dedupe_m, collapse, fold):
    cues = dedupe(cues, dedupe_m)
    if fold:
        cues = fold_straights(cues)
    rows = []
    if collapse:
        groups = group_by_junction(cues)
        for gi, (tgt, grp) in enumerate(groups):
            nxt = groups[gi+1][1][0] if gi + 1 < len(groups) else None
            if tgt and len(grp) > 1:
                rows.append(_split_line(render_junction_group(tgt, grp, nxt)))
            else:
                for c in grp:
                    rows.append(_split_line(render_cluster_line([c], None)))
    else:
        for c in cues:
            rows.append(_split_line(render_cluster_line([c], None)))
    return rows

def render_html(title, total_km, cues, nodes, pois, mode, dedupe_m, collapse, fold):
    e = html.escape
    badge = "signs &amp; notes only" if mode == "independent" else "device following track"
    node_str = " · ".join(k for k, _ in nodes)
    dir_html = ""
    if mode == "independent":
        for it in merged_items(cues, pois, dedupe_m, collapse, fold):
            km = f"{it['km']:.1f} km" if isinstance(it["km"], (int, float)) else ""
            if it.get("poi"):
                desc = f'<div class="poi-d">{e(it["desc"])}</div>' if it["desc"] else ""
                dir_html += (f'<div class="poi"><div class="poi-h"><span class="tag">{e(it["label"])}</span>'
                             f'<span class="poi-n">{e(it["name"])}</span><span class="poi-km">{km}</span></div>'
                             f'{desc}</div>\n')
            else:
                dir_html += f'<div class="row"><span class="km">{e(km)}</span><span class="cue">{e(it["text"])}</span></div>\n'
    else:
        dir_html = '<div class="row"><span class="cue">Follow the device; nodes and stops below confirm you are on plan.</span></div>'
        for p in sorted([p for p in pois if isinstance(p["km"], (int, float))], key=lambda p: p["km"]):
            it = poi_item(p); km = f"{it['km']:.1f} km"
            desc = f'<div class="poi-d">{e(it["desc"])}</div>' if it["desc"] else ""
            dir_html += (f'<div class="poi"><div class="poi-h"><span class="tag">{e(it["label"])}</span>'
                         f'<span class="poi-n">{e(it["name"])}</span><span class="poi-km">{km}</span></div>{desc}</div>\n')

    nodes_block = f'<div class="nodes"><b>Knooppunten</b> {e(node_str)}</div>' if node_str else ""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<style>
  :root {{ --red:#c0101d; --ink:#1b1b1b; --gray:#8a8a8a; --line:#e6e2d8; --paper:#faf9f6; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,Segoe UI,Arial,sans-serif; color:var(--ink);
         background:var(--paper); margin:0; padding:28px; line-height:1.5; }}
  .sheet {{ max-width:760px; margin:0 auto; }}
  header {{ border-bottom:3px solid var(--red); padding-bottom:10px; margin-bottom:6px; }}
  h1 {{ font-size:23px; margin:0; color:var(--red); text-transform:uppercase; letter-spacing:.4px; }}
  .meta {{ color:var(--gray); font-size:13px; margin-top:3px; }}
  .nodes {{ font-size:14px; margin:14px 0; padding:8px 10px; background:#fff;
            border:1px solid var(--line); border-radius:6px; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.6px; color:var(--gray);
        margin:22px 0 8px; }}
  .row {{ display:flex; gap:12px; padding:5px 0; border-bottom:1px solid var(--line); }}
  .km {{ flex:0 0 88px; color:var(--red); font-variant-numeric:tabular-nums; font-weight:600; font-size:13px; }}
  .cue {{ flex:1; }}
  .poi {{ background:#fff; border:1px solid var(--line); border-left:3px solid var(--red);
          border-radius:6px; padding:9px 12px; margin:8px 0; }}
  .poi-h {{ display:flex; align-items:baseline; gap:8px; }}
  .tag {{ font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#fff;
          background:var(--red); border-radius:3px; padding:1px 6px; }}
  .poi-n {{ font-weight:600; }}
  .poi-km {{ margin-left:auto; color:var(--gray); font-size:13px; font-variant-numeric:tabular-nums; }}
  .poi-d {{ color:#444; font-size:14px; margin-top:4px; }}
  @media print {{ body {{ background:#fff; padding:0; }} .poi, .nodes {{ break-inside:avoid; }} }}
</style></head>
<body><div class="sheet">
  <header><h1>{e(title)}</h1><div class="meta">{total_km:.1f} km &nbsp;·&nbsp; {badge}</div></header>
  {nodes_block}
  <h2>Directions &amp; highlights</h2>
  {dir_html}
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Complete route sheet from a cached RWGPS route.")
    ap.add_argument("route_json", help="path to a cached route JSON (e.g. route_cache/12345.json)")
    ap.add_argument("--mode", choices=["independent", "dependent"], default="independent")
    ap.add_argument("--dedupe-m", type=float, default=10.0,
                    help="collapse near-coincident duplicate cues within this many metres (default 10; tune later)")
    ap.add_argument("--no-collapse-junctions", action="store_true",
                    help="turn OFF same-junction collapse (content merge, on by default)")
    ap.add_argument("--no-fold-straights", action="store_true",
                    help="turn OFF folding of consecutive nameless 'continue straight' (on by default)")
    ap.add_argument("--pois", help="external POI highlights JSON (list of {name,type,lat,lon,desc}); "
                                   "merged with the route's own POIs. This is the automatable feed.")
    ap.add_argument("--html", help="also write a self-contained printable HTML page to this path")
    args = ap.parse_args()

    rj = json.load(open(args.route_json, encoding="utf-8"))
    route = rj.get("route", rj)
    data = rwgps.extract(rj)
    title = clean_title(data["name"])
    total_km = (data["distance_m"] or 0) / 1000
    cues = [c for c in data["cues"] if c["text"]]
    nodes = nodes_from_cues(cues)
    extra_pois = json.load(open(args.pois, encoding="utf-8")) if args.pois else []
    pois = place_pois(route, data["pois"] + extra_pois)

    if args.mode == "dependent":
        out = render_dependent(title, total_km, nodes, pois)
    else:
        out = render_independent(title, total_km, cues, nodes, pois,
                                 args.dedupe_m, not args.no_collapse_junctions, not args.no_fold_straights)
    print("\n" + out + "\n")
    print(f"[{len(cues)} cues · {len(nodes)} junctions named · {len(pois)} stops]")

    if args.html:
        page = render_html(title, total_km, cues, nodes, pois, args.mode,
                           args.dedupe_m, not args.no_collapse_junctions, not args.no_fold_straights)
        open(args.html, "w", encoding="utf-8").write(page)
        print(f"Wrote {args.html}")

if __name__ == "__main__":
    main()
