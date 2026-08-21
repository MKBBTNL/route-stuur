#!/usr/bin/env python3
"""Build the Koblenz - Treis-Karden pilot German route sheet (milestone-strip variant)."""
import os, re

TPL = open("/home/claude/work/route_note_template.html", encoding="utf-8").read()

# --- milestone-strip CSS (5 per row, glyph icons instead of node numbers) ---
STRIP_CSS = """
/* milestone strip (German variant of the knooppunt ribbon) */
.tl-track.milestones{row-gap:22px}
.tl-track.milestones .node{flex:0 0 20%}
.tl-track.milestones .dot{background:#fff;border:2px solid var(--blue);width:34px;height:34px}
.tl-track.milestones .dot svg{width:18px;height:18px;fill:none;stroke:var(--blue);stroke-width:1.8}
.tl-track.milestones .node::before{top:17px;background:var(--line)}
.tl-track.milestones .dot.poi{border-color:var(--green)}.tl-track.milestones .dot.poi svg{stroke:var(--green)}
.tl-track.milestones .dot.detour{border-color:var(--red)}.tl-track.milestones .dot.detour svg{stroke:var(--red)}
.tl-track.milestones .dot.term{background:var(--dark);border-color:var(--dark)}.tl-track.milestones .dot.term svg{stroke:#fff}
.tl-track.milestones .town{font-size:10px;line-height:1.15}
"""
ICON = {
 "anchor":'<svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2"/><path d="M12 7v13M5 13a7 7 0 0 0 14 0M4 13h2M18 13h2"/></svg>',
 "ferry":'<svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15l1.5 4h13L20 15zM12 5v6M8 8h8M4 15h16"/></svg>',
 "castle":'<svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V8l2 1V6l2 1V5l2 1V5l2-1v2l2-1v2l2-1v3l2-1v12z"/><path d="M10 20v-4h4v4"/></svg>',
 "view":'<svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s4-6 10-6 10 6 10 6-4 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.5"/></svg>',
 "flag":'<svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><path d="M6 21V4M6 4h11l-2 4 2 4H6"/></svg>',
 "finish":'<svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><path d="M6 21V4M6 4h12v9H6"/><path d="M6 4h3v3H6zM12 4h3v3h-3zM9 7h3v3H9zM15 7h3v3h-3zM6 10h3v3H6zM12 10h3v3h-3z" fill="currentColor" stroke="none"/></svg>',
}
# milestone spine: (icon, km, label, class)   curated moorings/Eltz + candidates' castles/ferries/deviation
MILESTONES = [
 ("anchor", "0",  "Koblenz",            "term"),
 ("castle", "0",  "Alte Burg",          "poi"),
 ("castle", "16", "Niederburg",         "poi"),
 ("castle", "18", "Schloss v.d. Leyen", "poi"),
 ("ferry",  "20", "Ferry Kobern",       ""),
 ("castle", "31", "Burg Bischofstein",  "poi"),
 ("flag",   "33", "Leave Radweg",       "detour"),
 ("castle", "35", "Burg Eltz \u2191",   "detour"),
 ("flag",   "39", "Rejoin Radweg",      "detour"),
 ("finish", "44", "Treis-Karden",       "term"),
]

def strip_html():
    cells = []
    for icon, km, label, cls in MILESTONES:
        c = f" {cls}" if cls else ""
        cells.append(f'<div class="node"><span class="dot{c}">{ICON[icon]}</span>'
                     f'<span class="km">{km}</span><span class="town">{label}</span></div>')
    rows = 2
    return f'<div class="tl-track milestones" data-rows="{rows}">' + "".join(cells) + "</div>"

ROUTE = """<p class="rk-draft">Follow the <span class="jct">Mosel-Radweg</span> downstream from the Koblenz mooring \u2014 about 78% of today is on the signed route, so for most of the day you simply follow the blue Mosel-Radweg signs along the river.</p>
<p class="rk-draft rk-confirm">km 33 \u00b7 leave the Mosel-Radweg toward <span class="jct">Burg Eltz</span> \u2014 a ~5.5 km there-and-back climb up the Eltzbach valley \u2014 rejoin the Radweg at km 39. \u2014 confirm road \u2014</p>
<p class="rk-draft">Passenger ferries cross at Kobern-Gondorf (20 km), Alken (25 km) and Treis-Karden\u2013Cochem (43 km) if you want the far bank. Finish at the Treis-Karden mooring, by the Cochem ferry landing.</p>"""

HIGHLIGHTS = """<div class="block high"><h3>{castle} Castles along the Mosel</h3><ul>
<li>Alte Burg, Koblenz \u2014 riverside medieval fort (0 km)</li>
<li>Niederburg, Kobern \u2014 hill castle above the village (16 km)</li>
<li>Schloss von der Leyen, Gondorf \u2014 moated Renaissance schloss (18 km)</li>
<li>Burg Bischofstein (31 km)</li>
<li><b>Burg Eltz</b> \u2014 the detour star, one of Germany\u2019s best-preserved medieval castles (35 km)</li></ul></div>
<div class="block high"><h3>{view} Views</h3><ul>
<li>\u00dcber\u2019m Rath (5 km) \u00b7 Blums Lay (13 km)</li>
<li>Hubertush\u00f6he (20 km) \u00b7 Kompuskopf (43 km)</li></ul></div>
<div class="block safe"><h3>{flag} The Burg Eltz detour</h3><ul>
<li>Leaves the Radweg around km 33, up the Eltzbach valley</li>
<li>~5.5 km round trip; park bikes at the lot and walk the last stretch</li>
<li>Adds real climbing \u2014 allow extra time</li></ul></div>"""

def fill():
    out = TPL.replace("</style>", STRIP_CSS + "\n</style>")
    loc = {"trouble":"Trouble en route?","callship":"Call your ship:","scan":"scan for GPS",
           "today":"Today\u2019s route","along":"Along the way","start":"START","finish":"FINISH"}
    repl = {
        "{{LANG}}":"en", "{{DAY}}":"", "{{TITLE}}":"Koblenz &#8594; Treis-Karden",
        "{{DISTANCE}}":"44 km", "{{TIMELINE}}":strip_html(), "{{ROUTE}}":ROUTE,
        "{{HIGHLIGHTS}}":HIGHLIGHTS.format(castle=ICON["castle"], view=ICON["view"], flag=ICON["flag"]),
        "{{ROUTE_PILL}}":"Mosel &middot; via Burg Eltz", "{{SHIP}}":"\u00abship + phone\u00bb",
        "{{QR}}":"", "{{QR_CODE}}":"\u00abQR\u00bb",
        "{{START}}":"Koblenz, mooring", "{{END}}":"Treis-Karden, mooring",
        "{{L_TROUBLE}}":loc["trouble"],"{{L_CALLSHIP}}":loc["callship"],"{{L_SCAN}}":loc["scan"],
        "{{L_TODAY}}":loc["today"],"{{L_ALONG}}":loc["along"],"{{L_START}}":loc["start"],"{{L_FINISH}}":loc["finish"],
    }
    for k,v in repl.items(): out = out.replace(k,v)
    out = out.replace('class="route-pill"','class="route-pill red"')
    # style the confirm-draft cue so it stands out for verification
    out = out.replace("</style>", ".route .rk-confirm{color:var(--red);font-style:normal}\n</style>")
    return out

open("/home/claude/work/Koblenz_Treis-Karden_pilot_EN.html","w",encoding="utf-8").write(fill())
print("wrote pilot; unfilled tokens:", re.findall(r"\{\{[A-Z_]+\}\}", open('/home/claude/work/Koblenz_Treis-Karden_pilot_EN.html').read()) or "none")
