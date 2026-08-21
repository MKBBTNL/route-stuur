#!/usr/bin/env python3
"""
de_cues.py — turn a route's Mosel-Radweg leave/rejoin points into draft prose cues.

Reads a GPX and its <route>.probe.json (from de_probe.py). For every DEVIATION
(where the track leaves the named route and where it rejoins) it writes one prose
strip, in the house voice, per language. Nothing else is cued — you follow the
signed Radweg the rest of the way.

Each cue is a DRAFT you verify:
  - direction (left/right, sharp) comes from the track bearing over a ~40 m window;
  - a nearby durable landmark is used as a human anchor when there is one;
  - the road you turn onto is NOT asserted (no reliable name yet) -> every leave cue
    is tagged "confirm" so you add/So check the road. Low-confidence turns are tagged too.

  python3 de_cues.py route.gpx                 # prints EN/NL/DE drafts
  python3 de_cues.py route.gpx --lang de --html > cues.html

Stdlib only. No network (works off the probe.json + the GPX).
"""
import argparse, json, math, os
import xml.etree.ElementTree as ET

GPX = "{http://www.topografix.com/GPX/1/1}"

def read_track(path):
    r = ET.parse(path).getroot()
    pts = [(float(t.get("lat")), float(t.get("lon"))) for t in r.iter(GPX+"trkpt")]
    R = 6371000.0
    lat0 = sum(p[0] for p in pts)/len(pts)
    xy = [(math.radians(lo)*R*math.cos(math.radians(lat0)), math.radians(la)*R) for la, lo in pts]
    cum = [0.0]
    for i in range(1, len(xy)):
        cum.append(cum[-1]+math.hypot(xy[i][0]-xy[i-1][0], xy[i][1]-xy[i-1][1]))
    return pts, cum

def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dl = lo2-lo1
    x = math.sin(dl)*math.cos(la2)
    y = math.cos(la1)*math.sin(la2)-math.sin(la1)*math.cos(la2)*math.cos(dl)
    return (math.degrees(math.atan2(x, y))+360) % 360

def idx_at_km(cum, km):
    m = km*1000
    for i, c in enumerate(cum):
        if c >= m: return i
    return len(cum)-1

def pt_offset(pts, cum, i, meters):
    """track point ~meters before(-)/after(+) index i."""
    target = cum[i]+meters
    if meters < 0:
        j = i
        while j > 0 and cum[j] > target: j -= 1
        return j
    j = i
    while j < len(cum)-1 and cum[j] < target: j += 1
    return j

def turn_at(pts, cum, i, win=40):
    a = pts[pt_offset(pts, cum, i, -win)]; b = pts[i]; c = pts[pt_offset(pts, cum, i, win)]
    if a == b or b == c: return 0.0
    d = (bearing(b, c)-bearing(a, b)+540) % 360 - 180   # signed: + right, - left
    return d

# ---- house-voice templates ----
T = {
 "en": {"follow":"Follow the Mosel-Radweg (signed) \u2014 about {cov}% of today is on the route.",
        "leave":"km {k} \u00b7 leave the Mosel-Radweg, turn {dir}{anchor}.",
        "rejoin":"km {k} \u00b7 rejoin the Mosel-Radweg and follow the signs.",
        "off":"km {a}\u2013{b} \u00b7 off the route for {s} km.",
        "left":"left","right":"right","sharp":"sharp ","at":" (at {n})","confirm":" \u2014 confirm road \u2014",
        "confirm_dir":" \u2014 confirm direction \u2014"},
 "nl": {"follow":"Volg de Mosel-Radweg (bewegwijzerd) \u2014 ongeveer {cov}% van vandaag ligt op de route.",
        "leave":"km {k} \u00b7 verlaat de Mosel-Radweg, ga {dir}{anchor}.",
        "rejoin":"km {k} \u00b7 weer op de Mosel-Radweg, volg de bordjes.",
        "off":"km {a}\u2013{b} \u00b7 {s} km buiten de route.",
        "left":"links","right":"rechts","sharp":"scherp ","at":" (bij {n})","confirm":" \u2014 weg controleren \u2014",
        "confirm_dir":" \u2014 richting controleren \u2014"},
 "de": {"follow":"Dem Mosel-Radweg folgen (ausgeschildert) \u2014 rund {cov}% des Tages liegen auf der Route.",
        "leave":"km {k} \u00b7 den Mosel-Radweg verlassen, {dir} abbiegen{anchor}.",
        "rejoin":"km {k} \u00b7 zur\u00fcck auf den Mosel-Radweg, den Schildern folgen.",
        "off":"km {a}\u2013{b} \u00b7 {s} km abseits der Route.",
        "left":"links","right":"rechts","sharp":"scharf ","at":" (bei {n})","confirm":" \u2014 Stra\u00dfe pr\u00fcfen \u2014",
        "confirm_dir":" \u2014 Richtung pr\u00fcfen \u2014"},
}

def anchor_for(km, landmarks, lang):
    best = None
    for l in landmarks:
        dk = abs(l["km"]-km)
        if dk <= 1.0 and (best is None or dk < best[0]): best = (dk, l["name"])
    return T[lang]["at"].format(n=best[1]) if best else ""

def cues_for(pts, cum, probe, lang):
    t = T[lang]
    devs = probe.get("deviations", [])
    lines = []  # (text, low_conf)
    cov = probe.get("backbone_coverage_pct", 0)
    routes = probe.get("named_routes", [])
    if routes and cov:
        lines.append((t["follow"].format(cov=cov), False))
    for d in devs:
        i = idx_at_km(cum, d["leave_km"])
        turn = turn_at(pts, cum, i)
        mag = abs(turn)
        dir_word = (t["sharp"] if mag >= 100 else "") + (t["right"] if turn > 0 else t["left"])
        low_dir = mag < 30                      # gentle -> ambiguous
        anch = anchor_for(d["leave_km"], probe.get("landmarks", []), lang)
        leave = t["leave"].format(k=d["leave_km"], dir=dir_word, anchor=anch)
        leave += t["confirm"]                   # road name never asserted -> always confirm road
        if low_dir: leave += t["confirm_dir"]
        lines.append((leave, True))
        if d["span_km"] >= 0.5:
            lines.append((t["off"].format(a=d["leave_km"], b=d["rejoin_km"], s=d["span_km"]), False))
        lines.append((t["rejoin"].format(k=d["rejoin_km"]), False))
    if not devs and routes:
        lines.append(("km \u2014 \u00b7 stays on the Mosel-Radweg the whole way; no route changes.", False))
    return lines

def render(lines, lang, html):
    if not html:
        return "\n".join(("[confirm] " if lc else "        ") + txt for txt, lc in lines)
    out = []
    for txt, lc in lines:
        cls = "rk-draft rk-confirm" if lc else "rk-draft"
        out.append(f'<p class="{cls}">{txt}</p>')
    return "\n".join(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx")
    ap.add_argument("--probe", help="path to <route>.probe.json (default: alongside the gpx)")
    ap.add_argument("--lang", choices=["en", "nl", "de", "all"], default="all")
    ap.add_argument("--html", action="store_true")
    a = ap.parse_args()
    probe_path = a.probe or (a.gpx.rsplit(".", 1)[0] + ".probe.json")
    if not os.path.exists(probe_path):
        raise SystemExit(f"probe json not found: {probe_path} (run de_probe.py first)")
    probe = json.load(open(probe_path, encoding="utf-8"))
    pts, cum = read_track(a.gpx)
    langs = ["en", "nl", "de"] if a.lang == "all" else [a.lang]
    for lang in langs:
        lines = cues_for(pts, cum, probe, lang)
        print(f"\n===== {lang.upper()} =====")
        print(render(lines, lang, a.html))

if __name__ == "__main__":
    main()
