#!/usr/bin/env python3
"""
check_cues.py - cuesheet linter + worklist builder for the 2026 route set.

Turns two problems into action lists, per route:
  1. LANGUAGE MIX       - cues resolving to >1 language in one sheet.
  2. U-TURN ON STRAIGHT - a U-turn cue where the track is basically straight.

Quarantine is ONLY driven by --custom-list (route ids you know carry
hand-written / translated cues). The tool never guesses hand-written cues from
text - that was unreliable. Everything else that's language-mixed stays in the
re-trace queue where it's actionable.

Offline; nothing talks to RWGPS.

    python check_cues.py --cache route_cache --inspect
    python check_cues.py --cache route_cache --out cue_check
    python check_cues.py --cache route_cache --out cue_check --custom-list known_custom.txt

Outputs (into --out):
    cue_summary.csv     overview, one row per flagged route
    retrace_queue.csv   language-mixed routes, GROUPED BY LANGUAGE (batch job)
    uturn_worklist.csv  one row per spurious U-turn cue            (surgical job)
    quarantine.csv      ONLY your --custom-list ids - fix by hand
"""

import argparse, csv, glob, json, math, os, sys
from collections import Counter

# ------------------------- schema field candidates -------------------------
ROUTE_WRAPPER_KEYS = ("route",)
NAME_KEYS   = ("name", "title")
UPDATED_KEYS= ("updated_at", "updatedAt", "created_at", "createdAt")
COURSE_KEYS = ("course_points", "coursePoints", "cues")
TRACK_KEYS  = ("track_points", "trackPoints", "points")
CP_TYPE_KEYS= ("t", "type", "turn")
CP_NOTE_KEYS= ("n", "note", "description", "text")
CP_DIST_KEYS= ("d", "distance", "dist", "distance_m")
CP_LAT_KEYS = ("y", "lat", "latitude")
CP_LON_KEYS = ("x", "lng", "lon", "longitude")
TP_LAT_KEYS = ("y", "lat", "latitude")
TP_LON_KEYS = ("x", "lng", "lon", "longitude")

UTURN_TYPE_CODES = {"tu", "uturn", "ut", "u"}
TURN_TYPE_CODES  = {"tu","uturn","ut","u","left","right","tl","tr",
                    "slight left","slight right","sl","sr",
                    "sharp left","sharp right","shl","shr",
                    "tnleft","tnright","bear left","bear right"}

# distinctive instruction templates (multi-word => low cross-language collision)
LEXICON = {
    "EN": ["turn left","turn right","slight left","slight right","sharp left","sharp right",
           "make a u-turn","u-turn","u turn","continue","bear left","bear right","keep left",
           "keep right","roundabout"," onto ","toward","start of ride","end of ride","arrive","head "],
    "NL": ["sla linksaf","sla rechtsaf","sla ","linksaf","rechtsaf","rechtdoor","ga rechtdoor",
           "flauwe bocht","scherpe bocht","keer om","rotonde","vervolg","neem de","richting",
           "start van de rit","einde van de rit","houd links","houd rechts","kruispunt"],
    "DE": ["links abbiegen","rechts abbiegen","abbiegen","geradeaus","wenden","leicht links",
           "leicht rechts","scharf links","scharf rechts","kreisverkehr","richtung","ausfahrt",
           "weiter","halten sie","beginn der fahrt","ende der fahrt","nehmen sie"],
}
UTURN_WORDS = ["u-turn", "u turn", "make a u-turn", "keer om", "wenden"]


def first(d, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default

def unwrap(data):
    for k in ROUTE_WRAPPER_KEYS:
        if isinstance(data, dict) and isinstance(data.get(k), dict):
            return data[k]
    return data

def is_2026(route, year="2026"):
    if year in str(first(route, NAME_KEYS, "")).lower():
        return True
    return str(first(route, UPDATED_KEYS, "")).startswith(year)

def detect_lang(note):
    if not note:
        return None
    s = str(note).lower()
    scores = {lang: sum(s.count(tok) for tok in toks) for lang, toks in LEXICON.items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return None
    ordered = sorted(scores.values(), reverse=True)
    if len(ordered) > 1 and ordered[0] == ordered[1]:
        return None
    return best

def is_uturn(cp_type, note):
    if str(cp_type or "").strip().lower() in UTURN_TYPE_CODES:
        return True
    return any(w in str(note or "").lower() for w in UTURN_WORDS)

def is_turn(cp_type, note):
    if str(cp_type or "").strip().lower() in TURN_TYPE_CODES:
        return True
    return is_uturn(cp_type, note)

# ------------------------- geometry -------------------------
def haversine(lat1, lon1, lat2, lon2):
    R=6371000.0
    p1,p2=math.radians(lat1),math.radians(lat2)
    dp=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))

def bearing(lat1, lon1, lat2, lon2):
    p1,p2=math.radians(lat1),math.radians(lat2); dl=math.radians(lon2-lon1)
    x=math.sin(dl)*math.cos(p2)
    y=math.cos(p1)*math.sin(p2)-math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(x,y))+360)%360

def angle_diff(b1,b2):
    d=abs(b1-b2)%360
    return d if d<=180 else 360-d

def parse_track(tps):
    out=[]
    for tp in tps or []:
        lat=first(tp,TP_LAT_KEYS); lon=first(tp,TP_LON_KEYS)
        if lat is None or lon is None: continue
        try: out.append((float(lat),float(lon)))
        except (TypeError,ValueError): continue
    return out

def nearest_index(track,lat,lon):
    best_i=best_d=None
    for i,(tlat,tlon) in enumerate(track):
        d=haversine(lat,lon,tlat,tlon)
        if best_d is None or d<best_d: best_i,best_d=i,d
    return best_i

def point_at_offset(track,idx,meters,forward):
    acc=0.0; i=idx; step=1 if forward else -1
    while 0<=i+step<len(track) and 0<=i<len(track):
        a=track[i]; b=track[i+step]
        acc+=haversine(a[0],a[1],b[0],b[1]); i+=step
        if acc>=meters: return track[i]
    return track[i] if 0<=i<len(track) else None

def turn_angle_at(track,lat,lon,window_m):
    if len(track)<3: return None
    idx=nearest_index(track,lat,lon)
    if idx is None: return None
    before=point_at_offset(track,idx,window_m,False)
    after =point_at_offset(track,idx,window_m,True)
    anchor=track[idx]
    if not before or not after or before==anchor or after==anchor: return None
    b_in =bearing(before[0],before[1],anchor[0],anchor[1])
    b_out=bearing(anchor[0],anchor[1],after[0],after[1])
    return angle_diff(b_in,b_out)

# ------------------------- per-route check -------------------------
def check_route(route, straight_deg, window_m):
    name=str(first(route,NAME_KEYS,"")); rid=str(route.get("id",""))
    upd =str(first(route,UPDATED_KEYS,""))
    cps =first(route,COURSE_KEYS,[]) or []
    track=parse_track(first(route,TRACK_KEYS,[]))

    langs=Counter(); uturns=[]; n_turn_str=0
    for cp in cps:
        ctype=first(cp,CP_TYPE_KEYS); note=first(cp,CP_NOTE_KEYS); dist=first(cp,CP_DIST_KEYS)
        lat=first(cp,CP_LAT_KEYS); lon=first(cp,CP_LON_KEYS)
        try: dist_km=round(float(dist)/1000.0,2) if dist is not None else ""
        except (TypeError,ValueError): dist_km=""

        lang=detect_lang(note)
        if lang: langs[lang]+=1

        ang=None
        if lat is not None and lon is not None and track:
            try: ang=turn_angle_at(track,float(lat),float(lon),window_m)
            except (TypeError,ValueError): ang=None

        if ang is not None and ang<straight_deg and is_turn(ctype,note):
            if is_uturn(ctype,note):
                uturns.append((dist_km,ctype,str(note or "").strip(),round(ang,1),lang or ""))
            else:
                n_turn_str+=1

    guessed=langs.most_common(1)[0][0] if langs else ""
    breakdown=",".join(f"{l}:{c}" for l,c in langs.most_common())
    summary={
        "route_id":rid,"name":name,"updated":upd,"n_cues":len(cps),
        "langs_found":",".join(sorted(langs)),"lang_breakdown":breakdown,
        "guessed_language":guessed,"lang_mixed":"YES" if len(langs)>1 else "",
        "n_uturn_on_straight":len(uturns),"n_turn_on_straight":n_turn_str,
        "track_pts":len(track),
    }
    return summary, uturns

# ------------------------- inspect -------------------------
def inspect(paths):
    print("Inspecting first parseable route to confirm schema...\n")
    for fp in paths:
        try: data=json.load(open(fp,encoding="utf-8"))
        except Exception: continue
        r=unwrap(data); cps=first(r,COURSE_KEYS,[]) or []; tps=first(r,TRACK_KEYS,[]) or []
        print("file        :",os.path.basename(fp))
        print("route keys  :",list(r.keys())[:20] if isinstance(r,dict) else "-")
        print("name        :",first(r,NAME_KEYS))
        print("updated     :",first(r,UPDATED_KEYS),"| is_2026:",is_2026(r))
        print("n course pts:",len(cps),"| n track pts:",len(tps))
        if cps:
            cp=cps[0]; print("course[0]   :",json.dumps(cp)[:200])
            print("  -> type=",first(cp,CP_TYPE_KEYS),"| note=",repr(first(cp,CP_NOTE_KEYS)),
                  "| dist=",first(cp,CP_DIST_KEYS),"| lat/lon=",first(cp,CP_LAT_KEYS),first(cp,CP_LON_KEYS))
        if tps: print("track[0]    :",json.dumps(tps[0])[:200])
        return
    print("No parseable JSON found.")

# ------------------------- main -------------------------
def main():
    ap=argparse.ArgumentParser(description="Flag mixed-language cues and U-turns on straight roads (2026 routes); build worklists.")
    ap.add_argument("--cache",default="route_cache")
    ap.add_argument("--out",default="cue_check")
    ap.add_argument("--year",default="2026")
    ap.add_argument("--straight-deg",type=float,default=30.0)
    ap.add_argument("--window-m",type=float,default=25.0)
    ap.add_argument("--custom-list",default=None,
                    help="text file of route ids known to carry custom/free-text cues (one per line); force-quarantined")
    ap.add_argument("--all",action="store_true")
    ap.add_argument("--inspect",action="store_true")
    args=ap.parse_args()

    paths=sorted(glob.glob(os.path.join(args.cache,"*.json")))
    if not paths: sys.exit(f"No *.json in {args.cache!r}")
    if args.inspect: inspect(paths); return

    known_custom=set()
    if args.custom_list:
        if os.path.exists(args.custom_list):
            known_custom={ln.strip() for ln in open(args.custom_list,encoding="utf-8") if ln.strip()}
            print(f"custom-list: {len(known_custom)} ids loaded from {args.custom_list}")
        else:
            print(f"WARNING: --custom-list {args.custom_list!r} not found; quarantine will be empty.")

    os.makedirs(args.out,exist_ok=True)
    scanned=matched=0
    summaries=[]; uturn_rows=[]; quarantine=[]; retrace=[]

    for fp in paths:
        try: data=json.load(open(fp,encoding="utf-8"))
        except Exception as e: print("skip:",os.path.basename(fp),e); continue
        route=unwrap(data); scanned+=1
        if not args.all and not is_2026(route,args.year): continue
        matched+=1
        summ,uturns=check_route(route,args.straight_deg,args.window_m)
        rid,name=summ["route_id"],summ["name"]

        quarantined = rid in known_custom
        summ["quarantine"]="YES" if quarantined else ""
        if summ["lang_mixed"] or summ["n_uturn_on_straight"] or quarantined:
            summaries.append(summ)

        if quarantined:
            quarantine.append({
                "route_id":rid,"name":name,"reason":"in custom-list",
                "lang_mixed":summ["lang_mixed"],"n_uturn_on_straight":summ["n_uturn_on_straight"],
                "action":"Re-trace / fix BY HAND - carries free-text cues, do NOT bulk-trace","status":"TODO"})
        if summ["lang_mixed"] and not quarantined:
            retrace.append({
                "guessed_language":summ["guessed_language"],"route_id":rid,"name":name,
                "lang_breakdown":summ["lang_breakdown"],"n_cues":summ["n_cues"],
                "action":f"Set account -> {summ['guessed_language'] or '?'}, run Trace","status":"TODO"})

        for (dist_km,ctype,note,ang,lang) in uturns:
            uturn_rows.append({
                "route_id":rid,"name":name,"dist_km":dist_km,"cue_type":ctype,
                "cue_note":note,"measured_angle_deg":ang,"cue_lang":lang,
                "also_retrace":"yes" if (summ["lang_mixed"] and not quarantined) else "",
                "action":"Delete spurious U-turn cue","status":"TODO"})

    lang_order={"NL":0,"DE":1,"EN":2,"":9}
    retrace.sort(key=lambda r:(lang_order.get(r["guessed_language"],5),r["route_id"]))
    uturn_rows.sort(key=lambda r:(r["route_id"], r["dist_km"] if r["dist_km"]!="" else 1e9))
    quarantine.sort(key=lambda r:r["route_id"])

    def dump(fn,rows,fields):
        path=os.path.join(args.out,fn)
        with open(path,"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
            for r in rows: w.writerow(r)
        return path

    dump("cue_summary.csv",summaries,
         ["route_id","name","updated","n_cues","langs_found","lang_breakdown","guessed_language",
          "lang_mixed","n_uturn_on_straight","n_turn_on_straight","quarantine","track_pts"])
    dump("retrace_queue.csv",retrace,
         ["guessed_language","route_id","name","lang_breakdown","n_cues","action","status"])
    dump("uturn_worklist.csv",uturn_rows,
         ["route_id","name","dist_km","cue_type","cue_note","measured_angle_deg","cue_lang","also_retrace","action","status"])
    dump("quarantine.csv",quarantine,
         ["route_id","name","reason","lang_mixed","n_uturn_on_straight","action","status"])

    by_lang=Counter(r["guessed_language"] for r in retrace)
    print(f"\nscanned {scanned} | {args.year}-matched {matched} | flagged {len(summaries)}\n")
    print("RE-TRACE QUEUE (batch by language - set account once per language):")
    for lang in ["NL","DE","EN",""]:
        if by_lang.get(lang): print(f"   {lang or '??':<3} {by_lang[lang]:>4} routes")
    print(f"U-TURN WORKLIST : {len(uturn_rows)} spurious cues across "
          f"{len(set(r['route_id'] for r in uturn_rows))} routes")
    print(f"QUARANTINE      : {len(quarantine)} routes from --custom-list (0 unless you pass a list)")
    print(f"\nwrote 4 CSVs to {args.out}/")
    print("Tip: re-trace first, then re-run this - u-turns on traced routes regenerate away.")


if __name__=="__main__":
    main()
