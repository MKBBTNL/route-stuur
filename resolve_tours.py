#!/usr/bin/env python3
"""
resolve_tours.py - turn EITHER the raw Salesforce tour worksheet OR a cleaned
tours.csv into an id-resolved tours file, by matching route NAMES against the
route_cache. Reports every name not found in the library.

    python resolve_tours.py --tours tours.csv --cache route_cache --out tours_resolved.csv

Accepts the raw export (semicolon, BOM, decimal commas, "Gematchte CSV route")
or an already-cleaned comma file with a 'name' column. Writes:
    tours_resolved.csv    resolved rows + route_id + match_status
    tours_unmatched.csv   rows needing attention (not_found / ambiguous / blank)
"""
import argparse, glob, json, os, re, sys
import numpy as np, pandas as pd

NAME_KEYS=("name","title"); WRAP=("route",)
def _first(d,ks,default=None):
    if not isinstance(d,dict): return default
    for k in ks:
        if k in d and d[k] not in (None,""): return d[k]
    return default
def _unwrap(x):
    for k in WRAP:
        if isinstance(x,dict) and isinstance(x.get(k),dict): return x[k]
    return x
def norm(s): return re.sub(r"\s+"," ",str(s)).strip().lower()

def read_any(path):
    """Read a CSV regardless of ; or , delimiter and BOM."""
    with open(path,"r",encoding="utf-8-sig",errors="replace") as f:
        head=f.readline()
    sep=";" if head.count(";")>head.count(",") else ","
    df=pd.read_csv(path,sep=sep,encoding="utf-8-sig",dtype=str,
                   keep_default_na=False,engine="python",on_bad_lines="warn")
    df.columns=[re.sub(r"^(\ufeff|\xef\xbb\xbf|ï»¿)+","",str(c)).strip() for c in df.columns]
    return df

def to_num(x):
    x=str(x).strip().replace(".","").replace(",",".") if x else ""
    try: return float(x)
    except: return np.nan
def lang(name):
    s=f" {str(name).upper()} "
    if re.search(r"\b(ENG|EN|ENGLISH)\b",s): return "EN"
    if re.search(r"\b(NL|DUTCH|NEDERLANDS?)\b",s): return "NL"
    if re.search(r"\b(DE|GER|DEU|DEUTSCH)\b",s): return "DE"
    if re.search(r"\b(FR|FRENCH)\b",s): return "FR"
    return ""
def _int(pat,s):
    m=re.search(pat,str(s)); return int(m.group(1)) if m else ""

def clean_raw(df):
    """Map the raw Salesforce worksheet to the standard tours schema."""
    C={"Tour (Tour__r.Name)":"tour","Program":"program","Day":"day_desc","Optie":"option_raw",
       "Doel km":"target_km","Gematchte CSV route":"name","Verschil (km)":"diff_km",
       "Hoogtemeters":"elev_m","Zekerheid":"confidence","Row #":"source_row"}
    df=df.rename(columns={k:v for k,v in C.items() if k in df.columns})
    df["day"]=df["day_desc"].map(lambda s:_int(r"Day\s*(\d+)",s)) if "day_desc" in df else ""
    df["option"]=df["option_raw"].map(lambda s:_int(r"(\d+)",s)) if "option_raw" in df else ""
    df["language"]=df["name"].map(lang)
    for c in ("target_km","diff_km","elev_m"):
        if c in df: df[c]=df[c].map(to_num)
    if "confidence" in df:
        df["applied"]=~df["confidence"].str.lower().str.contains("niet toegepast")
    return df

def build_index(cache):
    idx={}
    for fp in glob.glob(os.path.join(cache,"*.json")):
        try: r=_unwrap(json.load(open(fp,encoding="utf-8")))
        except Exception: continue
        name=_first(r,NAME_KEYS,"")
        if not name: continue
        rid=str(r.get("id") or os.path.splitext(os.path.basename(fp))[0])
        idx.setdefault(norm(name),[]).append(rid)
    return idx

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--tours",default="tours.csv")
    ap.add_argument("--cache",default="route_cache")
    ap.add_argument("--out",default="tours_resolved.csv")
    a=ap.parse_args()
    if not os.path.exists(a.tours): sys.exit(f"tours file not found: {a.tours!r}")
    if not os.path.isdir(a.cache): sys.exit(f"cache not found: {a.cache!r}")

    df=read_any(a.tours)
    if "Gematchte CSV route" in df.columns:
        print("detected raw Salesforce worksheet -> cleaning first")
        df=clean_raw(df)
    if "name" not in df.columns:
        sys.exit(f"no route-name column found. columns: {list(df.columns)}")

    idx=build_index(a.cache)
    print(f"library: {sum(len(v) for v in idx.values())} routes, {len(idx)} distinct names")

    rid,status=[],[]
    for nm in df["name"]:
        if not str(nm).strip(): rid.append(""); status.append("blank"); continue
        hits=idx.get(norm(nm))
        if not hits: rid.append(""); status.append("not_found")
        elif len(set(hits))==1: rid.append(hits[0]); status.append("exact")
        else: rid.append("|".join(sorted(set(hits)))); status.append("ambiguous")
    df["route_id"]=rid; df["match_status"]=status

    front=["route_id","name","tour","program","day","option","language","match_status"]
    cols=[c for c in front if c in df.columns]+[c for c in df.columns if c not in front]
    df=df[cols]
    df.to_csv(a.out,index=False,encoding="utf-8")
    bad=df[df["match_status"].isin(["not_found","ambiguous","blank"])]
    bad.to_csv("tours_unmatched.csv",index=False,encoding="utf-8")

    print("match status:",df["match_status"].value_counts().to_dict())
    print(f"wrote {a.out}  ({(df['match_status']=='exact').sum()}/{len(df)} resolved)")
    print(f"wrote tours_unmatched.csv  ({len(bad)} rows need attention)")
    nf=df[df.match_status=='not_found']['name']
    if len(nf):
        print("\nsample not-found names:")
        for n in nf.head(10): print("  -",n)

if __name__=="__main__": main()
