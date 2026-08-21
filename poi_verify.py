#!/usr/bin/env python3
"""
poi_verify.py - freshness layer for the POI master.

Manual verification with dates, so only POIs a human has confirmed *this season*
reach a guest route. Review window is 1 year by default, but is now category-
aware (see CATEGORY_REVIEW_YEARS below, a placeholder -- restructure freely)
if the POI has been through poi_curate.py's `categorize`. Connects at the
flat-file edge: run `emit-clean` to produce a master holding only in-date POIs,
then point poi_master `pick --emit` at that.

  # a guide rode a route and confirms its POIs:
  python poi_verify.py verify master_pois.json --from 213.pois.json --by XX

  # or specific ids:
  python poi_verify.py verify master_pois.json --ids cafe-zaandam,ferry-f3 --by XX

  python poi_verify.py stale  master_pois.json           # worklist: never/expired
  python poi_verify.py status master_pois.json           # counts
  python poi_verify.py retire master_pois.json --ids old-cafe
  python poi_verify.py emit-clean master_pois.json --out master_fresh.json

Two independent, automated corroboration signals can feed in here, both as
SIBLING fields on the POI (never nested inside `verify`, so a bad automated
run can't touch a human's real verification): `osm_check` from
poi_osm_check.py (free, OSM/Overpass-based) and `google_check` from
poi_google_check.py (paid, Google Places API-based). `status` and `stale`
both treat a POI as flagged if EITHER source says it's gone -- see
is_flagged() below.
"""
import argparse, csv, json, re, sys
from datetime import date

ID_KEYS=("rwgps_id","id","slug","poi_id"); NAME_KEYS=("name","title","label")

# --- PLACEHOLDER: review interval per risk category, in years -------------
# Restructure freely -- this is a first-pass mapping, not a locked decision.
# Categories come from poi_curate.py's `categorize` (route_critical / safety /
# commercial / infrastructure / informational). route_critical & safety are
# where "unavailable" strands or endangers a guest -> check every season.
# commercial is the highest-churn tier (cafes/lodging close, change hands) ->
# also every season for now. infrastructure/informational barely change ->
# safe to stretch the rotation. A POI with no `category` yet (never run
# through categorize) falls back to DEFAULT_REVIEW_YEARS so nothing regresses.
CATEGORY_REVIEW_YEARS = {
    "route_critical": 1,
    "safety": 1,
    "commercial": 1,
    "infrastructure": 2,
    "informational": 3,
}
DEFAULT_REVIEW_YEARS = 1

def review_years(p):
    return CATEGORY_REVIEW_YEARS.get(p.get("category"), DEFAULT_REVIEW_YEARS) if isinstance(p, dict) else DEFAULT_REVIEW_YEARS

def plus_interval(d, years):
    try: return d.replace(year=d.year+years)
    except ValueError: return d.replace(month=2,day=28,year=d.year+years)
def iso(d): return d.isoformat()
def poi_id(obj,fallback=None):
    for k in ID_KEYS:
        if isinstance(obj,dict) and obj.get(k) not in (None,""): return str(obj[k])
    return fallback
def poi_name(obj):
    for k in NAME_KEYS:
        v=obj.get(k) if isinstance(obj,dict) else None
        if isinstance(v,dict): v=v.get("nl") or next(iter(v.values()),"")
        if v: return str(v)
    return ""
def norm(s): return re.sub(r"\s+"," ",str(s)).strip().lower()

def load_master(path):
    data=json.load(open(path,encoding="utf-8"))
    if isinstance(data,dict) and isinstance(data.get("pois"),list):
        return data,"wrapped",{poi_id(p,str(i)):p for i,p in enumerate(data["pois"])}
    if isinstance(data,list):
        return data,"list",{poi_id(p,str(i)):p for i,p in enumerate(data)}
    if isinstance(data,dict):
        # {id: poi}
        return data,"map",{k:(v if isinstance(v,dict) else {"value":v}) for k,v in data.items()}
    sys.exit("unrecognised master JSON shape")
def save_master(path,data): json.dump(data,open(path,"w",encoding="utf-8"),ensure_ascii=False,indent=2)

def is_indate(p,asof):
    v=p.get("verify") if isinstance(p,dict) else None
    if not v or v.get("status")!="verified": return False
    rb=v.get("review_by")
    try: return date.fromisoformat(rb)>=asof
    except Exception: return False

def osm_signal(p):
    """The OSM corroboration signal folded into the master schema by
    poi_osm_check.py -- a SIBLING field to `verify`, not nested inside it,
    so a bad automated run can never touch a human's actual verification.
    Returns {} if this POI has never been OSM-checked."""
    return (p.get("osm_check") if isinstance(p,dict) else None) or {}

def google_signal(p):
    """Same sibling-field pattern as osm_signal, folded in by
    poi_google_check.py -- Google's own business_status corroboration
    (OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY). Returns {} if
    this POI has never been Google-checked."""
    return (p.get("google_check") if isinstance(p,dict) else None) or {}

def is_flagged(p):
    """A POI is flagged if EITHER independent corroboration source thinks
    it's gone -- OSM's disused_nearby, or Google's business_status having
    gone to CLOSED_PERMANENTLY. Two differently-sourced, imperfect signals
    (one free/community-mapped, one Google's own) -- either firing is worth
    surfacing before a human's actual visit."""
    o=osm_signal(p); g=google_signal(p)
    return bool(o.get("disused_nearby")) or g.get("business_status")=="CLOSED_PERMANENTLY"

def collect_ids_from_route(path):
    d=json.load(open(path,encoding="utf-8"))
    pois=d.get("pois") if isinstance(d,dict) else d
    ids=set(); names=set()
    for p in (pois or []):
        i=poi_id(p)
        if i: ids.add(i)
        else: names.add(norm(poi_name(p)))
    return ids,names

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    for c in ("verify","stale","status","retire","emit-clean"):
        s=sub.add_parser(c); s.add_argument("master")
        if c in ("verify","retire"):
            s.add_argument("--ids",default=""); s.add_argument("--from",dest="frm",default="")
        if c=="verify":
            s.add_argument("--by",required=True); s.add_argument("--on",default=iso(date.today()))
        if c in ("stale","status","emit-clean"):
            s.add_argument("--as-of",default=iso(date.today()))
        if c in ("stale","emit-clean"):
            s.add_argument("--out",default=("master_fresh.json" if c=="emit-clean" else ""))
    a=ap.parse_args()
    data,kind,index=load_master(a.master)
    asof=date.fromisoformat(getattr(a,"as_of",iso(date.today())))

    if a.cmd in ("verify","retire"):
        targets=set(x.strip() for x in a.ids.split(",") if x.strip())
        name_targets=set()
        if a.frm:
            ids,names=collect_ids_from_route(a.frm); targets|=ids; name_targets|=names
        hit=0
        for pid,p in index.items():
            match = pid in targets or (name_targets and norm(poi_name(p)) in name_targets)
            if not match: continue
            if a.cmd=="verify":
                on=date.fromisoformat(a.on)
                years=review_years(p)
                p["verify"]={"status":"verified","on":iso(on),"by":a.by,"review_by":iso(plus_interval(on,years))}
            else:
                p.setdefault("verify",{})["status"]="retired"
            hit+=1
        save_master(a.master,data)
        print(f"{a.cmd}: updated {hit} POIs" + (f" (as {a.by}; review_by set per-POI from its category -- "
              f"see CATEGORY_REVIEW_YEARS)" if a.cmd=='verify' else ""))
        if targets and hit<len(targets): print(f"note: {len(targets)-hit} id(s) not found in master")
        return

    if a.cmd=="status":
        vi=sum(is_indate(p,asof) for p in index.values())
        vexp=sum(1 for p in index.values() if isinstance(p,dict) and p.get("verify",{}).get("status")=="verified" and not is_indate(p,asof))
        ret=sum(1 for p in index.values() if isinstance(p,dict) and p.get("verify",{}).get("status")=="retired")
        unv=len(index)-vi-vexp-ret
        print(f"POI master: {len(index)} total  (as of {iso(asof)})")
        print(f"  verified & in-date : {vi}")
        print(f"  verified but EXPIRED: {vexp}")
        print(f"  never verified     : {unv}")
        print(f"  retired            : {ret}")
        print(f"\nonly the {vi} in-date POIs would reach a route (via emit-clean).")
        osm_checked=[p for p in index.values() if isinstance(p,dict) and osm_signal(p)]
        if osm_checked:
            disused=sum(1 for p in osm_checked if osm_signal(p).get("disused_nearby"))
            print(f"\nOSM corroboration: {len(osm_checked)} POIs checked, {disused} flagged disused_nearby")
        google_checked=[p for p in index.values() if isinstance(p,dict) and google_signal(p)]
        if google_checked:
            closed=sum(1 for p in google_checked if google_signal(p).get("business_status")=="CLOSED_PERMANENTLY")
            print(f"Google corroboration: {len(google_checked)} POIs checked, {closed} marked CLOSED_PERMANENTLY")
        if osm_checked or google_checked:
            both_flagged=sum(1 for p in index.values() if isinstance(p,dict) and is_flagged(p))
            print(f"(prioritise these {both_flagged} OSM- or Google-flagged POIs in `stale` first)")
        return

    if a.cmd=="stale":
        rows=[]
        for pid,p in index.items():
            if not isinstance(p,dict): continue
            v=p.get("verify",{})
            if v.get("status")=="retired": continue
            if not is_indate(p,asof):
                reason="never verified" if v.get("status")!="verified" else f"expired {v.get('review_by')}"
                osm=osm_signal(p); google=google_signal(p)
                rows.append({"id":pid,"name":poi_name(p),"type":p.get("type",""),"reason":reason,
                             "last_by":v.get("by",""),"osm_disused":bool(osm.get("disused_nearby")),
                             "google_closed":google.get("business_status")=="CLOSED_PERMANENTLY"})
        # POIs flagged by EITHER free automated signal (OSM disused_nearby or
        # Google business_status=CLOSED_PERMANENTLY) surface first -- worklists
        # are long, and agreement between two independently-sourced signals
        # (or even just one firing) is the closest thing to a priority order
        # until a real ranking system exists.
        rows.sort(key=lambda r: not (r["osm_disused"] or r["google_closed"]))
        n_osm = sum(1 for r in rows if r["osm_disused"])
        n_google = sum(1 for r in rows if r["google_closed"])
        n_either = sum(1 for r in rows if r["osm_disused"] or r["google_closed"])
        bits=[]
        if n_osm: bits.append(f"{n_osm} OSM-flagged")
        if n_google: bits.append(f"{n_google} Google-flagged")
        suffix = f", {' + '.join(bits)} ({n_either} total) as possibly disused -- listed first" if bits else ""
        print(f"{len(rows)} POIs need a recheck (as of {iso(asof)}){suffix}:")
        for r in rows[:20]:
            flags=[]
            if r["osm_disused"]: flags.append("OSM: disused nearby")
            if r["google_closed"]: flags.append("Google: closed permanently")
            flag = f" [{', '.join(flags)}]" if flags else ""
            print(f"  {r['id']:<22} {r['name'][:30]:<30} {r['reason']}{flag}")
        if a.out:
            with open(a.out,"w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=["id","name","type","reason","last_by","osm_disused","google_closed"])
                w.writeheader(); w.writerows(rows)
            print(f"wrote {a.out}")
        return

    if a.cmd=="emit-clean":
        keep=[p for p in index.values() if is_indate(p,asof)]
        if kind=="map":
            out={pid:p for pid,p in index.items() if is_indate(p,asof)}
        elif kind=="wrapped":
            out=dict(data); out["pois"]=keep
        else:
            out=keep
        json.dump(out,open(a.out,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
        print(f"emit-clean: {len(keep)} in-date POIs -> {a.out}  (feed this to poi_master pick)")
        return

if __name__=="__main__": main()
