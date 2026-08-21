#!/usr/bin/env python3
"""
resolve_tourprogram_routes.py - connect RWGPS route ids to Salesforce Tour_Program
day-rows, in the Salesforce export's own format.

Joins the resolved tour worksheet to the Salesforce export on
(tour, program, day); option 1/2 map to the two Cycling_Distance option fields,
and each option's route id is verified against the SF distance.

    python resolve_tourprogram_routes.py --sf Tour_Program__c-....csv --tours tours_resolved.csv --out tourprogram_routes.csv

Writes:
    tourprogram_routes.csv     SF columns + Route_Option_1__c/2__c + names + km check
    tourprogram_update.csv      lean Data Loader update: Id + the two route ids
"""
import argparse, csv, os, re, sys
import pandas as pd

def rd(f):
    h=open(f,encoding="utf-8-sig",errors="replace").readline()
    sep=";" if h.count(";")>h.count(",") else ","
    d=pd.read_csv(f,sep=sep,dtype=str,keep_default_na=False,engine="python",on_bad_lines="warn")
    d.columns=[c.lstrip("\ufeff").strip() for c in d.columns]
    return d
def nprog(s):
    s=str(s).lower().strip()
    s=re.sub(r"^program:\s*","",s)
    s=s.replace("–","-").replace("—","-")
    return re.sub(r"\s+"," ",s)
def sf_day(name):
    m=re.search(r"day\s*0*(\d+)",str(name).lower())
    return int(m.group(1)) if m else None
def to_km(x):
    try: return round(float(str(x).replace(",",".")))
    except: return None

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--sf",required=True); ap.add_argument("--tours",default="tours_resolved.csv")
    ap.add_argument("--out",default="tourprogram_routes.csv")
    a=ap.parse_args()
    sf=rd(a.sf); tr=rd(a.tours)

    TOUR="Tour_Program__r.Tour__r.Name"; PROG="Tour_Program__r.Name"
    O1="Cycling_Distance_Option_1__c"; O2="Cycling_Distance_Option_2__c"

    # pivot worksheet -> (tour, nprogram, day) : option1/2 route+name+km
    piv={}
    for _,r in tr.iterrows():
        try: day=int(float(r.get("day","") or 0))
        except: continue
        key=(r["tour"].strip(), nprog(r["program"]), day)
        slot=piv.setdefault(key,{})
        opt=str(r.get("option","")).strip()
        d={"rid":r.get("route_id","").strip(),"name":r.get("name","").strip(),
           "km":to_km(r.get("target_km","")),"status":r.get("match_status","").strip()}
        slot["1" if opt=="1" else "2" if opt=="2" else opt]=d

    out=[]; placed=0; matched_rows=0; km_flags=0
    used_keys=set()
    for _,s in sf.iterrows():
        row={c:s[c] for c in sf.columns}
        key=(s[TOUR].strip(), nprog(s[PROG]), sf_day(s["Name"]))
        slot=piv.get(key)
        r1=r2=n1=n2=""; kmchk="na"
        if slot:
            used_keys.add(key); matched_rows+=1
            o1=slot.get("1"); o2=slot.get("2")
            if o1: r1=o1["rid"]; n1=o1["name"]
            if o2: r2=o2["rid"]; n2=o2["name"]
            if r1: placed+=1
            if r2: placed+=1
            # verify against SF distances
            sf1=to_km(s.get(O1,"")); sf2=to_km(s.get(O2,""))
            checks=[]
            if o1 and o1["km"] is not None and sf1 is not None:
                checks.append("1ok" if abs(o1["km"]-sf1)<=3 else "1MISMATCH")
            if o2 and o2["km"] is not None and sf2 is not None:
                checks.append("2ok" if abs(o2["km"]-sf2)<=3 else "2MISMATCH")
            kmchk=",".join(checks) if checks else "na"
            if "MISMATCH" in kmchk: km_flags+=1
        row["Route_Option_1__c"]=r1; row["Route_Option_2__c"]=r2
        row["opt1_route_name"]=n1; row["opt2_route_name"]=n2
        row["km_check"]=kmchk
        row["matched"]="Y" if slot else ""
        out.append(row)

    cols=list(sf.columns)+["Route_Option_1__c","Route_Option_2__c","opt1_route_name","opt2_route_name","km_check","matched"]
    with open(a.out,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=cols); w.writeheader()
        for r in out: w.writerow(r)
    # lean update file (rows with at least one route id)
    upd=os.path.join(os.path.dirname(a.out) or ".","tourprogram_update.csv")
    with open(upd,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["Id","Route_Option_1__c","Route_Option_2__c"])
        for r in out:
            if r["Route_Option_1__c"] or r["Route_Option_2__c"]:
                w.writerow([r["Id"],r["Route_Option_1__c"],r["Route_Option_2__c"]])

    unused=[k for k in piv if k not in used_keys]
    print(f"SF day-rows: {len(sf)} | matched to worksheet: {matched_rows} | route ids placed: {placed}")
    print(f"km-cross-check mismatches: {km_flags}  (option order to verify)")
    print(f"worksheet (tour,program,day) keys with NO SF row: {len(unused)}")
    for k in unused[:8]: print("   no SF slot:",k)
    print(f"wrote {a.out} and {upd}")

if __name__=="__main__": main()
