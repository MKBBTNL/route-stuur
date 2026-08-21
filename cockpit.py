#!/usr/bin/env python3
"""
cockpit.py - BBT internal route cockpit (v4).

Layout: Tools live in the LEFT rail; Sources/setup sit in the main area;
all cue problems are consolidated in one "Cue QA" tab; a "For colleagues"
group exposes the node-strip + knooppunten/route-notes utilities.
No login — launch it with run_cockpit.bat (double-click).

Reads route_cache + cue_check CSVs + tours_resolved.csv + coverage_out.
QA flags come straight from check_cues output; nothing re-implements CLI logic.

    python -m pip install streamlit pandas
    python -m streamlit run cockpit.py
"""
import datetime, glob, importlib, io, json, os, re, shlex, subprocess, sys, webbrowser, zipfile
import pandas as pd
import streamlit as st

# --- routekit / poi_verify integration (in-process; loaded from the Toolset folder at
# runtime, fresh each call so a changed folder is honoured -- both are stdlib-only) ---
def load_routekit(toolset):
    """Import routekit.py from the chosen Toolset folder, fresh each call so a changed
    folder is honoured. routekit is stdlib-only, so this is cheap and safe."""
    p = os.path.abspath(toolset)
    if p not in sys.path:
        sys.path.insert(0, p)
    sys.modules.pop("routekit", None)
    return importlib.import_module("routekit")

def load_poi_verify(toolset):
    """Same pattern as load_routekit -- the POI map tab writes `verify` blocks using
    poi_verify.py's own category-aware review-interval logic, not a reimplementation."""
    p = os.path.abspath(toolset)
    if p not in sys.path:
        sys.path.insert(0, p)
    sys.modules.pop("poi_verify", None)
    return importlib.import_module("poi_verify")

# ------------------------- read-only metadata parsing -----------------------
NAME_KEYS=("name","title"); UPD=("updated_at","updatedAt","created_at","createdAt")
COURSE_KEYS=("course_points","coursePoints","cues"); DIST_KEYS=("distance","distance_m")
LOCALITY_KEYS=("locality","administrative_area"); WRAP=("route",)
def _first(d,ks,default=None):
    if not isinstance(d,dict): return default
    for k in ks:
        if k in d and d[k] not in (None,""): return d[k]
    return default
def _unwrap(x):
    for k in WRAP:
        if isinstance(x,dict) and isinstance(x.get(k),dict): return x[k]
    return x
def parse_language(name):
    s=f" {str(name).upper()} "
    if re.search(r"\b(ENG|EN|ENGLISH)\b",s): return "EN"
    if re.search(r"\b(NL|DUTCH|NEDERLANDS?)\b",s): return "NL"
    if re.search(r"\b(DE|DEU|GER|DEUTSCH)\b",s): return "DE"
    if re.search(r"\b(FR|FRENCH|FRAN\W?AIS)\b",s): return "FR"
    return ""
def parse_day(name):
    u=str(name).upper(); m=re.search(r"\bD(\d{1,2})\b",u) or re.search(r"\bDAY\s*(\d{1,2})\b",u)
    return int(m.group(1)) if m else None
def is_2026(r,y):
    if y in str(_first(r,NAME_KEYS,"")).lower(): return True
    return str(_first(r,UPD,"")).startswith(y)

@st.cache_data(show_spinner=False)
def scan_routes(cache_dir,year):
    rows,skipped=[],0
    for fp in sorted(glob.glob(os.path.join(cache_dir,"*.json"))):
        try: data=json.load(open(fp,encoding="utf-8"))
        except Exception: skipped+=1; continue
        r=_unwrap(data)
        if not is_2026(r,year): continue
        name=str(_first(r,NAME_KEYS,"")); rid=str(r.get("id") or os.path.splitext(os.path.basename(fp))[0])
        dist=_first(r,DIST_KEYS)
        try: dist_km=round(float(dist)/1000.0,1) if dist is not None else None
        except (TypeError,ValueError): dist_km=None
        rows.append({"route_id":rid,"name":name,"language":parse_language(name),"day":parse_day(name),
                     "distance_km":dist_km,"locality":_first(r,LOCALITY_KEYS,""),
                     "n_cues":len(_first(r,COURSE_KEYS,[]) or []),"updated":str(_first(r,UPD,""))[:10]})
    df=pd.DataFrame(rows)
    if "day" in df.columns:
        # keep 'day' a clean nullable integer (mixed int/'' breaks Arrow serialization)
        df["day"]=pd.to_numeric(df["day"],errors="coerce").astype("Int64")
    return df,skipped

@st.cache_data(show_spinner=False)
def read_csv(path):
    if path and os.path.exists(path):
        try:
            df=pd.read_csv(path,dtype={"route_id":str})
            if "route_id" in df.columns: df["route_id"]=df["route_id"].astype(str)
            return df
        except Exception as e: st.warning(f"Could not read {path}: {e}")
    return None

def tour_column(df):
    for c in df.columns:
        if c.lower() in ("tour","tour_week","week"): return c
    return None
def qr_column(df):
    for c in df.columns:
        if c.lower() in ("qr","qr_code","code"): return c
    return None

def build_registry(routes,summary,tours):
    reg=routes.copy()
    if reg.empty: return reg
    reg["route_id"]=reg["route_id"].astype(str)
    if summary is not None and not summary.empty:
        keep=[c for c in ["route_id","lang_mixed","guessed_language","lang_breakdown",
                          "n_uturn_on_straight","n_turn_on_straight","quarantine"] if c in summary.columns]
        reg=reg.merge(summary[keep],on="route_id",how="left")
    for c in ["lang_mixed","guessed_language","lang_breakdown","n_uturn_on_straight","n_turn_on_straight","quarantine"]:
        if c not in reg.columns: reg[c]=""
    reg["n_uturn_on_straight"]=pd.to_numeric(reg["n_uturn_on_straight"],errors="coerce").fillna(0).astype(int)
    reg["lang_mixed"]=reg["lang_mixed"].astype(str).replace("nan","")
    if tours is not None and not tours.empty and "route_id" in tours.columns:
        reg=reg.merge(tours,on="route_id",how="left",suffixes=("","_tour"))
        tc=tour_column(tours); reg["_has_tour"]=reg[tc].notna() if tc else False
    else:
        reg["_has_tour"]=False
    def flag(row):
        f=[]
        if str(row.get("lang_mixed","")).upper()=="YES": f.append("MIX")
        if row.get("n_uturn_on_straight",0)>0: f.append("UTURN")
        if str(row.get("quarantine","")).upper()=="YES": f.append("QUAR")
        return ",".join(f)
    reg["issues"]=reg.apply(flag,axis=1)
    return reg

# ------------------------- tool runner ------------------------------------
RISK_BADGE={"read":"read-only","build":"writes output","cache":"network→cache","danger":"rewrites master"}
TOOLS=[
 {"group":"colleague","key":"node_strip","label":"Node strip (SVG)","kind":"open","target":"knooppunten-svg-export.html",
  "help":"Opens the printable node-strip tool in your browser."},
 {"group":"colleague","key":"gpx_knoop","label":"Knooppunten from GPX","kind":"run","risk":"build","script":"gpx_to_knooppunten.py",
  "args":"ROUTE.gpx","help":"Node sequence + distance from a GPX. Offline."},
 {"group":"colleague","key":"gpx_notes","label":"Route notes from GPX","kind":"run","risk":"build","script":"gpx_to_routenotes.py",
  "args":"ROUTE.gpx","help":"Text route notes from a GPX. Offline."},
 {"group":"checks","key":"check_cues","label":"Cue check (QA)","kind":"run","risk":"read","script":"check_cues.py",
  "args":"--cache route_cache --out cue_check","help":"Writes only into cue_check/."},
 {"group":"checks","key":"poi_view","label":"POI master — view","kind":"run","risk":"read","script":"poi_master.py",
  "args":"view master_pois.json --out poi_view.csv","help":"Read-only flat CSV. (verify filename)"},
 {"group":"checks","key":"coverage","label":"Coverage (families)","kind":"run","risk":"read","script":"build_coverage.py",
  "args":"--cache route_cache --tours tours_resolved.csv --out coverage_out","help":"Collapses to families + per-language gaps."},
 {"group":"advanced","key":"make_notes","label":"make_notes","kind":"run","risk":"build","script":"make_notes.py",
  "args":"--cache route_cache --out notes","help":"Writes txt+html to notes/. Heavy over full cache."},
 {"group":"advanced","key":"route_sheet","label":"route_sheet","kind":"run","risk":"build","script":"route_sheet.py",
  "args":"--help","help":"Args vary; start with --help."},
 {"group":"advanced","key":"poi_pick","label":"POI master — pick --emit","kind":"run","risk":"build","script":"poi_master.py",
  "args":"pick master_pois.json ROUTE.gpx --emit ID.pois.json --lang nl","help":"Writes one sidecar."},
 {"group":"advanced","key":"rwgps_pull","label":"rwgps_pull (network)","kind":"run","risk":"cache","script":"rwgps_pull.py",
  "args":"--dry-run --limit 5","help":"Pulls into route_cache. Dry preview by default."},
 {"group":"advanced","key":"poi_harvest","label":"POI master — harvest (REBUILD)","kind":"run","risk":"danger","script":"poi_master.py",
  "args":"harvest master_pois.json route_cache","help":"Rebuilds the master. Deliberate re-harvest only."},
]
def build_cmd(python_exe,folder,script,args):
    return [python_exe,os.path.join(folder,script)]+shlex.split(args,posix=False)
def run_command(python_exe,folder,script,args,timeout):
    env=dict(os.environ); env["PYTHONUTF8"]="1"; env["PYTHONIOENCODING"]="utf-8"
    try:
        p=subprocess.run(build_cmd(python_exe,folder,script,args),cwd=folder,capture_output=True,
                         encoding="utf-8",errors="replace",timeout=timeout,env=env)
        return p.returncode,p.stdout,p.stderr
    except subprocess.TimeoutExpired:
        return -1,"",f"Timed out after {timeout}s. Big batch jobs are better run in a terminal."
    except Exception as e:
        return -1,"",f"{type(e).__name__}: {e}"

def render_tool(t,folder,timeout):
    if t["kind"]=="open":
        if st.button(t["label"],key=f"o_{t['key']}",width='stretch'):
            path=os.path.join(folder,t["target"])
            if os.path.exists(path):
                webbrowser.open("file:///"+os.path.abspath(path).replace("\\","/"))
                st.session_state["run"]={"label":t["label"],"cmd":f"open {t['target']}","rc":0,"out":f"Opened {t['target']} in your browser.","err":""}
            else:
                st.session_state["run"]={"label":t["label"],"cmd":f"open {t['target']}","rc":-1,"out":"","err":f"{t['target']} not found."}
        st.caption(t["help"]); return
    exists=os.path.exists(os.path.join(folder,t["script"]))
    st.markdown(f"<span style='font-size:13px;font-weight:500'>{t['label']}</span> "
                f"<span style='font-size:10px;color:#888'>{RISK_BADGE.get(t.get('risk','read'))}</span>",unsafe_allow_html=True)
    args=st.text_input("args",t["args"],key=f"a_{t['key']}",label_visibility="collapsed")
    gate=True
    if t.get("risk") in ("cache","danger"):
        gate=st.checkbox("confirm",key=f"ok_{t['key']}")
        if t["risk"]=="danger" and gate:
            gate=st.text_input("type CONFIRM",key=f"cf_{t['key']}")=="CONFIRM"
    c1,c2=st.columns(2)
    if c1.button("Run",key=f"r_{t['key']}",disabled=not(exists and gate),width='stretch'):
        rc,out,err=run_command(sys.executable,folder,t["script"],args,timeout)
        st.session_state["run"]={"label":t["label"],"cmd":f"python {t['script']} {args}","rc":rc,"out":out,"err":err}
    if c2.button("Help",key=f"h_{t['key']}",disabled=not exists,width='stretch'):
        rc,out,err=run_command(sys.executable,folder,t["script"],"--help",timeout)
        st.session_state["run"]={"label":t["label"]+" --help","cmd":f"python {t['script']} --help","rc":rc,"out":out,"err":err}
    if not exists: st.caption(f"⚠ {t['script']} missing")

# ------------------------- POI map tab (v0) --------------------------------
def poi_display_name(p):
    n = p.get("name")
    if isinstance(n, dict):
        return n.get("en") or n.get("nl") or n.get("de") or ""
    return n or ""

def poi_display_desc(p):
    d = p.get("desc")
    if isinstance(d, dict):
        return d.get("en") or d.get("nl") or d.get("de") or ""
    return d or ""

def poi_color(p):
    """green = verified & in-date, orange = verified but expired, gray = never
    verified, red = OSM says something disused nearby (single-named POIs only --
    see poi_osm_check.py's cluster-name filter). Red overrides everything else --
    it's the freshest, most specific signal available.

    osm_check lives directly on the POI record now (folded into the master
    schema, no separate osm_corroboration.json sidecar to load/pass around)."""
    o = p.get("osm_check") or {}
    if o.get("disused_nearby"):
        return "#c0101d"
    v = p.get("verify") or {}
    if v.get("status") == "verified":
        try:
            if datetime.date.fromisoformat(v.get("review_by", "1900-01-01")) >= datetime.date.today():
                return "#2e7d32"
        except Exception:
            pass
        return "#e08a00"
    return "#888888"

def poi_map_html(track_pts, placed, height=520):
    """Leaflet + OSM tiles via CDN -- no extra Python dependency, matches the
    toolset's stdlib-only ethos. Read-only view for now: click a marker for a
    popup, but verify/retire actions happen in the table below, not on the map
    itself (that needs a real bidirectional Streamlit component -- not this v0)."""
    markers = []
    for p in placed:
        o = p.get("osm_check") or {}
        markers.append({
            "lat": p["lat"], "lon": p["lon"], "id": p.get("id", ""),
            "name": poi_display_name(p) or p.get("id", ""),
            "type": p.get("type", ""), "category": p.get("category", ""),
            "km": p.get("km"), "color": poi_color(p), "osm_note": o.get("note", ""),
        })
    tpl = """
<div id="poimap" style="height:__HEIGHT__px;border-radius:8px;overflow:hidden;"></div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const track = __TRACK__;
  const markers = __MARKERS__;
  const map = L.map('poimap');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
  }).addTo(map);
  const line = L.polyline(track, {color: '#1b1b1b', weight: 3}).addTo(map);
  markers.forEach(function(m) {
    var marker = L.circleMarker([m.lat, m.lon], {
      radius: 7, color: m.color, fillColor: m.color, fillOpacity: 0.85, weight: 2
    }).addTo(map);
    marker.bindPopup(
      '<b>' + (m.name || m.id) + '</b><br>' + m.type + ' &middot; ' + m.category +
      '<br>' + (m.km != null ? m.km + ' km along route' : '') +
      (m.osm_note ? '<br><i>' + m.osm_note + '</i>' : '')
    );
  });
  if (track.length > 1) { map.fitBounds(line.getBounds(), {padding: [20, 20]}); }
  else if (markers.length) { map.setView([markers[0].lat, markers[0].lon], 13); }
  else { map.setView([52.1, 5.1], 8); }
</script>
"""
    pts_js = json.dumps([[round(la, 6), round(lo, 6)] for la, lo in track_pts])
    return (tpl.replace("__HEIGHT__", str(height))
               .replace("__TRACK__", pts_js)
               .replace("__MARKERS__", json.dumps(markers)))

def render_poi_tab(toolset):
    st.subheader("POI proximity editor")
    st.caption("Upload a route to see which master POIs sit near it, on a map, and verify or "
               "retire them in place. Colour: green = verified & in-date, orange = verified but "
               "expired, gray = never verified, red = OSM flags something disused nearby "
               "(single-named POIs only -- see poi_osm_check.py). OSM corroboration now lives "
               "directly on each POI (osm_check) -- no separate sidecar file to point at.")

    c = st.columns([2, 1])
    master_path = c[0].text_input("POI master file",
                                  os.path.join(toolset, "master_deduped.categorized.json"), key="poi_master_path")
    cut = c[1].number_input("Radius (m)", 50, 2000, 250, step=50, key="poi_cut")

    up = st.file_uploader("Route (.gpx/.tcx)", type=["gpx", "tcx"], key="poi_route_up")
    if not os.path.exists(master_path):
        st.warning(f"Master not found: {master_path!r} -- point it at the right file above."); return
    if not up:
        st.info("Upload a route to see its nearby POIs."); return

    try:
        rk = load_routekit(toolset)
        pts, xy, cum, lat0, wpts = rk.ingest(up.getvalue(), up.name)
    except Exception as e:
        st.error(f"Could not read route: {e}"); return

    master_records = json.load(open(master_path, encoding="utf-8"))
    by_id = {p["id"]: p for p in master_records if "id" in p}
    geocoded = [p for p in master_records if p.get("lat") is not None and p.get("lon") is not None]
    n_nogeo = len(master_records) - len(geocoded)
    if n_nogeo:
        st.caption(f"({n_nogeo} master POIs have no coordinates yet -- skipped, can't be placed on a map)")
    placed = rk.place_along(geocoded, xy, cum, lat0, cut)
    n_osm_checked = sum(1 for p in master_records if p.get("osm_check"))

    st.caption(f"{len(placed)} of {len(master_records)} master POIs within {cut:.0f} m of this route  ·  "
               f"OSM corroboration: {n_osm_checked} POIs checked in this master" if n_osm_checked
               else f"{len(placed)} of {len(master_records)} master POIs within {cut:.0f} m of this route  ·  "
                    f"OSM corroboration: none yet (run poi_osm_check.py)")
    st.components.v1.html(poi_map_html(pts, placed), height=540, scrolling=False)
    if not placed:
        return

    rows = []
    for p in placed:
        v = p.get("verify") or {}
        o = p.get("osm_check") or {}
        rows.append({"id": p.get("id", ""), "name": poi_display_name(p), "type": p.get("type", ""),
                     "category": p.get("category", ""), "km": p.get("km"), "offset_m": p.get("offset_m"),
                     "verify_status": v.get("status", "never"), "review_by": v.get("review_by", ""),
                     "osm_disused": o.get("disused_nearby", False), "osm_note": o.get("note", "")})
    df = pd.DataFrame(rows)
    st.dataframe(df, width='stretch', hide_index=True, height=300)

    st.markdown("##### Verify / retire selected")
    names_by_id = dict(zip(df["id"], df["name"]))
    pick = st.multiselect("POIs", df["id"].tolist(),
                          format_func=lambda i: f"{i} — {names_by_id.get(i, '')}", key="poi_pick")
    vc = st.columns([2, 1, 1])
    by_name = vc[0].text_input("Verified/retired by (initials)", key="poi_by")
    confirm = vc[1].checkbox("confirm", key="poi_confirm", help=f"writes to {master_path}")
    do_verify = vc[2].button("Verify selected", disabled=not (pick and by_name and confirm), key="poi_verify_go")
    do_retire = vc[2].button("Retire selected", disabled=not (pick and confirm), key="poi_retire_go")

    st.markdown("##### Export")
    if pick:
        export_pois = []
        for pid in pick:
            p = by_id.get(pid)
            if not p:
                continue
            export_pois.append({
                "name": poi_display_name(p) or pid,
                "type": p.get("type", ""),
                "lat": p["lat"], "lon": p["lon"],
                "desc": poi_display_desc(p),
            })
        base_name = os.path.splitext(up.name)[0]
        gpx_data = rk.gpx_str(f"{base_name} + {len(export_pois)} POIs", pts, export_pois)
        st.download_button(f"⬇ Download GPX -- route + {len(export_pois)} selected POI(s)",
                           data=gpx_data, file_name=f"{base_name}_with_pois.gpx",
                           mime="application/gpx+xml", key="poi_gpx_download")
        st.caption("Bakes the selected POIs into the route as GPX waypoints -- opens in any GPS "
                   "app/device with both the track and the POIs visible together.")
    else:
        st.caption("Select POIs above to bake them into a downloadable GPX alongside the route.")

    if do_verify or do_retire:
        pv = load_poi_verify(toolset)
        on = datetime.date.today()
        hit = 0
        for pid in pick:
            p = by_id.get(pid)
            if not p:
                continue
            if do_verify:
                years = pv.review_years(p)
                p["verify"] = {"status": "verified", "on": on.isoformat(), "by": by_name,
                               "review_by": pv.plus_interval(on, years).isoformat()}
            else:
                p.setdefault("verify", {})["status"] = "retired"
            hit += 1
        json.dump(master_records, open(master_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        st.success(f"{'Verified' if do_verify else 'Retired'} {hit} POI(s) -> {master_path}")
        st.rerun()

# =========================== UI ===========================
def main():
    st.set_page_config(page_title="BBT Route Cockpit",page_icon="🚲",layout="wide")
    st.markdown("<h2 style='margin-bottom:0'>🚲 BBT Route Cockpit "
                "<span style='font-size:14px;color:#146A51'>· v4</span></h2>",unsafe_allow_html=True)

    # --- Sources / setup (now in the main area) ---
    with st.expander("⚙ Setup · sources", expanded=False):
        c=st.columns(3)
        toolset  =c[0].text_input("Toolset folder",".")
        cache_dir=c[0].text_input("route_cache",os.path.join(".","route_cache"))
        cue_dir  =c[1].text_input("cue_check",os.path.join(".","cue_check"))
        tours_csv=c[1].text_input("tours (resolved)",os.path.join(".","tours_resolved.csv"))
        cov_dir  =c[2].text_input("coverage_out",os.path.join(".","coverage_out"))
        year     =c[2].text_input("Year","2026")
        timeout  =c[2].number_input("Tool timeout (s)",30,7200,900,step=30)
        if st.button("↻ Reload data"): st.cache_data.clear(); st.rerun()

    if not os.path.isdir(cache_dir):
        st.error(f"route_cache not found: {cache_dir!r}. Open ⚙ Setup and point it at your cache.")
        st.stop()
    with st.spinner("Reading route library…"):
        routes,skipped=scan_routes(cache_dir,year)
    summary=read_csv(os.path.join(cue_dir,"cue_summary.csv"))
    retrace=read_csv(os.path.join(cue_dir,"retrace_queue.csv"))
    uturn  =read_csv(os.path.join(cue_dir,"uturn_worklist.csv"))
    tours  =read_csv(tours_csv)
    reg=build_registry(routes,summary,tours)
    if reg.empty:
        st.warning(f"No {year} routes found. Check the Year in ⚙ Setup."); st.stop()

    # --- LEFT RAIL: Tools ---
    with st.sidebar:
        st.markdown("### Tools")
        st.caption("For colleagues")
        for t in [x for x in TOOLS if x["group"]=="colleague"]:
            render_tool(t,toolset,int(timeout)); st.divider()
        st.caption("Checks")
        for t in [x for x in TOOLS if x["group"]=="checks"]:
            render_tool(t,toolset,int(timeout)); st.divider()
        with st.expander("Advanced (build · cache · danger)"):
            for t in [x for x in TOOLS if x["group"]=="advanced"]:
                render_tool(t,toolset,int(timeout)); st.divider()

    # --- last tool run output (main area) ---
    res=st.session_state.get("run")
    if res:
        with st.container(border=True):
            st.markdown(f"**Last run — {res['label']}**  ·  exit {res['rc']}")
            st.code(res["cmd"],language="bash")
            if res["out"]: st.text_area("output",res["out"][-6000:],height=160,key="op")
            if res["err"]: st.text_area("errors",res["err"][-3000:],height=110,key="ep")
            if st.button("Clear",key="clr"): st.session_state.pop("run",None); st.rerun()

    # --- metrics ---
    n_total=len(reg)
    n_mix=int((reg["lang_mixed"].astype(str).str.upper()=="YES").sum())
    n_utrt=int((reg["n_uturn_on_straight"]>0).sum())
    n_ucue=int(reg["n_uturn_on_straight"].sum())
    m=st.columns(5)
    m[0].metric(f"{year} routes",n_total); m[1].metric("Language-mixed",n_mix)
    m[2].metric("Routes w/ U-turns",n_utrt); m[3].metric("U-turn cues",n_ucue)
    m[4].metric("Tour-mapped",int(reg["_has_tour"].sum()))
    if summary is None:
        st.info("No cue_check/cue_summary.csv — QA columns blank. Run Cue check from the left rail.")

    tab_build,tab_reg,tab_tours,tab_cov,tab_qa,tab_poi=st.tabs(
        ["🧰 Build","📋 Registry","🗓 Tours","📊 Coverage","⚠ Cue QA","📍 POI Map"])

    with tab_poi:
        render_poi_tab(toolset)

    with tab_build:
        st.subheader("Build guest deliverables")
        st.caption("Upload a .gpx or .tcx — get route notes, a POI sidecar, and the track "
                   "back as gpx + tcx. Computed in memory; nothing is written on the server.")
        up = st.file_uploader("Route file(s)", type=["gpx", "tcx"],
                              accept_multiple_files=True, key="rk_up")
        cc = st.columns([2, 2, 1])
        lang = cc[0].radio("Language", ["en", "nl", "de", "all"], horizontal=True, key="rk_lang")
        poi_choice = cc[1].selectbox(
            "POIs from", ["The uploaded file", "POI master (pick within 250 m)"], key="rk_poi")
        offline = cc[2].checkbox("Offline", value=False, key="rk_off",
                                 help="Use only the cached node network; never contact Overpass.")
        master_path = None
        if poi_choice.startswith("POI master"):
            master_path = st.text_input("POI master file",
                                        os.path.join(toolset, "master_pois.json"), key="rk_master")
            if master_path and not os.path.exists(master_path):
                st.warning(f"Master not found: {master_path!r} — it will fall back to no POIs.")
        rcn_cache = os.path.join(toolset, "rcn_cache.json")

        if up and st.button("Build deliverables", type="primary", key="rk_build"):
            try:
                rk = load_routekit(toolset)
            except Exception as e:
                st.error(f"Could not import routekit.py from Toolset folder {toolset!r}: {e}")
                st.stop()
            langs = ("en", "nl", "de") if lang == "all" else (lang,)
            for f in up:
                try:
                    meta, files = rk.build_deliverables(
                        f.getvalue(), f.name, langs=langs,
                        master=(master_path or None), cache_path=rcn_cache, offline=offline)
                except Exception as e:
                    st.error(f"{f.name}: {e}")
                    continue
                with st.container(border=True):
                    ferry_bit = f" · {meta['ferries']} ferr{'y' if meta['ferries']==1 else 'ies'}" if meta.get("ferries") else ""
                    st.markdown(
                        f"**{meta['origin']} → {meta['dest']}**  ·  {meta['distance_km']} km · "
                        f"{meta['nodes']} nodes · {meta['gaps']} gaps{ferry_bit} · {meta['pois']} POIs  "
                        f"<span style='color:#888;font-size:11px'>POIs:{meta['poi_source']} · "
                        f"kp:{meta['kp_source']}</span>", unsafe_allow_html=True)
                    notes = {k: v for k, v in files.items() if k.endswith(".txt")}
                    if notes:
                        for (fn, content), pt in zip(notes.items(), st.tabs(list(notes.keys()))):
                            pt.code(content)
                    dl = st.columns(3)
                    for i, (fn, content) in enumerate(files.items()):
                        dl[i % 3].download_button("⬇ " + fn, content.encode("utf-8"),
                                                  fn, key=f"dl_{f.name}_{fn}")
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for fn, content in files.items():
                            z.writestr(fn, content)
                    st.download_button("⬇ Download all (zip)", buf.getvalue(),
                                       f"{meta['safe']}_deliverables.zip", "application/zip",
                                       key=f"zip_{f.name}")

    with tab_reg:
        f=st.columns([2,2,3])
        langs=sorted([l for l in reg["language"].unique() if l])
        pick_lang=f[0].multiselect("Language",langs,default=langs)
        only_issues=f[1].checkbox("Only routes with issues",value=False)
        query=f[2].text_input("Search name / id","")
        view=reg.copy()
        if pick_lang: view=view[view["language"].isin(pick_lang)|(view["language"]=="")]
        if only_issues: view=view[view["issues"]!=""]
        if query:
            q=query.lower(); view=view[view["name"].str.lower().str.contains(q)|view["route_id"].str.contains(q)]
        st.caption(f"{len(view)} of {n_total} routes")
        show=["route_id","name","language","day","distance_km","n_cues","issues",
              "lang_mixed","guessed_language","lang_breakdown","n_uturn_on_straight","locality","updated"]
        tc=tour_column(reg); qc=qr_column(reg)
        if tc: show.insert(3,tc)
        if qc: show.append(qc)
        show=[c for c in show if c in view.columns]
        st.dataframe(view[show],width='stretch',hide_index=True,height=520)
        st.download_button("⬇ Download this view (CSV)",view[show].to_csv(index=False).encode("utf-8"),"registry_view.csv","text/csv")

    with tab_tours:
        tc=tour_column(reg)
        if tours is None or tc is None:
            st.info("No tours file (or no tour/week column). Point ⚙ Setup at tours_resolved.csv.")
            template=routes[["route_id","name"]].copy()
            for col in ["tour","week","day","language","qr","notes"]: template[col]=""
            st.download_button("⬇ Download tours.csv starter",template.to_csv(index=False).encode("utf-8"),"tours.csv","text/csv")
        else:
            qc=qr_column(reg); base=reg.copy()
            if "applied" in base.columns and st.checkbox("Applied matches only",value=False):
                base=base[base["applied"].astype(str).str.lower().isin(["true","1","yes"])]
            cov=base[base["_has_tour"]].groupby(tc).agg(
                routes=("route_id","count"),
                languages=("language",lambda s:",".join(sorted({x for x in s if x}))),
                flagged=("issues",lambda s:int((s!="").sum())),).reset_index().sort_values(tc)
            st.subheader("Tour readiness"); st.dataframe(cov,width='stretch',hide_index=True)
            pick_t=st.selectbox("Open a tour",sorted(base[base["_has_tour"]][tc].dropna().unique()))
            t=base[base[tc]==pick_t].copy().sort_values("day",key=lambda s:pd.to_numeric(s,errors="coerce"))
            mm=st.columns(3); mm[0].metric("Routes",len(t))
            present=sorted({x for x in t["language"] if x})
            mm[1].metric("Languages",",".join(present) or "—"); mm[2].metric("Flagged",int((t["issues"]!="").sum()))
            miss=[l for l in ("EN","NL","DE") if l not in present]
            if miss and present: st.caption(f"Not present in this tour: {', '.join(miss)} (verify if expected).")
            cols=["day","route_id","name","language","issues","lang_mixed","n_uturn_on_straight"]
            if "option" in t.columns: cols.insert(1,"option")
            if qc: cols.insert(4,qc)
            st.dataframe(t[[c for c in cols if c in t.columns]],width='stretch',hide_index=True)
            unmapped=base[~base["_has_tour"]][["route_id","name","language"]]
            with st.expander(f"{len(unmapped)} routes not yet mapped to a tour"):
                st.dataframe(unmapped,width='stretch',hide_index=True)

    with tab_cov:
        sm=read_csv(os.path.join(cov_dir,"stem_matrix.csv"))
        if sm is None:
            st.info("No coverage_out/stem_matrix.csv yet — run Coverage (families) from the left rail.")
        else:
            fam=len(sm); covered=int((sm.get("covered_any",pd.Series([],dtype=str)).astype(str)=="Y").sum()) if "covered_any" in sm else 0
            g=st.columns(3); g[0].metric("Route families",fam); g[1].metric("Covered by a tour",covered); g[2].metric("Uncovered",fam-covered)
            st.caption("A family = one route across languages/lengths. Priority: EN uncovered first, then DE; NL OK to lack.")
            sub=st.tabs(["EN (base)","DE","NL","Family matrix"])
            for i,L in enumerate(["EN","DE","NL"]):
                with sub[i]:
                    d=read_csv(os.path.join(cov_dir,f"unassigned_{L}.csv"))
                    if d is None: st.info(f"No unassigned_{L}.csv."); continue
                    if st.checkbox("UNCOVERED only",value=(L!="NL"),key=f"unc_{L}") and "status" in d.columns:
                        d=d[d["status"]=="UNCOVERED"]
                    st.caption(f"{len(d)} routes"); st.dataframe(d,width='stretch',hide_index=True,height=440)
            with sub[3]: st.dataframe(sm,width='stretch',hide_index=True,height=440)

    with tab_qa:
        st.caption("Every cue problem in one place. Re-trace first, then re-run Cue check, then work the U-turns.")
        st.subheader("① Language mix — re-trace queue")
        if retrace is None or retrace.empty:
            st.info("No retrace_queue.csv yet — run Cue check.")
        else:
            for lang in ["NL","DE","EN",""]:
                grp=retrace[retrace["guessed_language"].fillna("")==lang] if "guessed_language" in retrace else retrace.iloc[0:0]
                if len(grp):
                    with st.expander(f"{lang or '??'} — {len(grp)} routes",expanded=(lang=="NL")):
                        st.dataframe(grp,width='stretch',hide_index=True)
        st.subheader("② U-turns on straight roads")
        if uturn is None or uturn.empty:
            st.info("No uturn_worklist.csv yet — run Cue check.")
        else:
            rids=["(all)"]+sorted(uturn["route_id"].astype(str).unique())
            pick=st.selectbox("Filter by route",rids,key="qa_ut")
            u=uturn if pick=="(all)" else uturn[uturn["route_id"].astype(str)==pick]
            st.caption(f"{len(u)} spurious U-turn cues"); st.dataframe(u,width='stretch',hide_index=True,height=420)
        st.subheader("③ Quarantine")
        nq=int((reg["quarantine"].astype(str).str.upper()=="YES").sum()) if "quarantine" in reg else 0
        st.caption(f"{nq} routes flagged (only via check_cues --custom-list). Fix by hand — never bulk-trace.")

    if skipped: st.sidebar.caption(f"⚠ {skipped} JSON files skipped (parse errors).")

if __name__=="__main__": main()
