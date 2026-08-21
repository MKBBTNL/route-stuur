#!/usr/bin/env python3
"""
build_coverage.py - collapse the 2026 library into route FAMILIES and report
tour coverage per language, with English as the base.

A family = the same route across languages/lengths, found by a name-stem
(km, language tag, [ALT]/[LONG]/[SHORT], year stripped). A family is COVERED if
any of its routes (any language) is assigned to a tour. So a DE/NL route whose
EN sibling is already in a tour is covered, not a gap.

    python build_coverage.py --cache route_cache --tours tours_resolved.csv --out coverage_out

Writes into --out:
    stem_matrix.csv        one row per family: which languages exist, which are assigned
    unassigned_EN.csv      base-language routes not in a tour  (priority 1)
    unassigned_DE.csv      German  (priority 2)
    unassigned_NL.csv      Dutch   (priority 3, OK to lack)
Each unassigned row is tagged covered_by_sibling (family already in a tour) or
UNCOVERED (no version in any tour) -> the UNCOVERED ones are the real work.
"""
import argparse, glob, json, os, re, sys
import pandas as pd

NAME_KEYS=("name","title"); WRAP=("route",); UPD=("updated_at","updatedAt","created_at","createdAt")
def _first(d,ks,default=None):
    if not isinstance(d,dict): return default
    for k in ks:
        if k in d and d[k] not in (None,""): return d[k]
    return default
def _unwrap(x):
    for k in WRAP:
        if isinstance(x,dict) and isinstance(x.get(k),dict): return x[k]
    return x
def is_2026(r,y):
    if y in str(_first(r,NAME_KEYS,"")).lower(): return True
    return str(_first(r,UPD,"")).startswith(y)
def language(name):
    s=f" {str(name).upper()} "
    if re.search(r"\b(ENG|EN|ENGLISH)\b",s): return "EN"
    if re.search(r"\b(DE|DEU|GER|DEUTSCH)\b",s): return "DE"
    if re.search(r"\b(NL|DUTCH|NEDERLANDS?)\b",s): return "NL"
    if re.search(r"\b(FR|FRENCH)\b",s): return "FR"
    return "OTHER"
def stem(name):
    s=str(name)
    s=re.sub(r"\[[^\]]*\]"," ",s)                                   # [ALT] [LONG] [SHORT]
    s=re.sub(r"\b20\d{2}\b"," ",s)                                  # year
    s=re.sub(r"\b(ENG|EN|ENGLISH|NL|DUTCH|NEDERLANDS?|DE|DEU|GER|DEUTSCH|FR|FRENCH)\b"," ",s,flags=re.I)
    s=re.sub(r"\b\d+\s*km\b"," ",s,flags=re.I)                      # "38 km"
    s=re.sub(r"\b\d+\b"," ",s)                                      # stray numbers
    s=re.sub(r"\s+"," ",s).strip(" -–—|").lower()
    return re.sub(r"\s+"," ",s)

def scan(cache,year):
    rows=[]
    for fp in glob.glob(os.path.join(cache,"*.json")):
        try: r=_unwrap(json.load(open(fp,encoding="utf-8")))
        except Exception: continue
        if not is_2026(r,year): continue
        name=str(_first(r,NAME_KEYS,""))
        rid=str(r.get("id") or os.path.splitext(os.path.basename(fp))[0])
        rows.append({"route_id":rid,"name":name,"language":language(name),"stem":stem(name)})
    return pd.DataFrame(rows)

def load_assigned(tours):
    if not os.path.exists(tours):
        print(f"WARNING: {tours!r} not found; treating nothing as assigned."); return set()
    df=pd.read_csv(tours,dtype=str,keep_default_na=False)
    if "route_id" not in df.columns: sys.exit("tours file has no route_id column (run resolve_tours.py first)")
    ids=set()
    for v in df["route_id"]:
        for part in str(v).split("|"):
            if part.strip(): ids.add(part.strip())
    return ids

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--cache",default="route_cache")
    ap.add_argument("--tours",default="tours_resolved.csv")
    ap.add_argument("--out",default="coverage_out")
    ap.add_argument("--year",default="2026")
    a=ap.parse_args()
    if not os.path.isdir(a.cache): sys.exit(f"cache not found: {a.cache!r}")
    os.makedirs(a.out,exist_ok=True)

    reg=scan(a.cache,a.year)
    if reg.empty: sys.exit("no 2026 routes found")
    assigned=load_assigned(a.tours)
    reg["assigned"]=reg["route_id"].isin(assigned)

    # per-family (stem) language + assignment matrix
    fam=[]
    for st,g in reg.groupby("stem"):
        langs=set(g["language"])
        row={"stem":st,"n_routes":len(g)}
        for L in ["EN","DE","NL","FR","OTHER"]:
            row[f"has_{L}"]="Y" if L in langs else ""
            row[f"assigned_{L}"]="Y" if g[(g.language==L)&(g.assigned)].shape[0]>0 else ""
        row["covered_any"]="Y" if g["assigned"].any() else ""
        row["example_ids"]=",".join(g["route_id"].head(4))
        fam.append(row)
    matrix=pd.DataFrame(fam).sort_values("stem")
    matrix.to_csv(os.path.join(a.out,"stem_matrix.csv"),index=False,encoding="utf-8")

    covered_stems=set(matrix[matrix.covered_any=="Y"]["stem"])
    # per-language unassigned files, tagged covered_by_sibling vs UNCOVERED
    def unassigned_for(L):
        d=reg[(reg.language==L)&(~reg.assigned)].copy()
        d["status"]=d["stem"].apply(lambda s:"covered_by_sibling" if s in covered_stems else "UNCOVERED")
        return d[["route_id","name","stem","status"]].sort_values(["status","stem"])
    counts={}
    for L in ["EN","DE","NL"]:
        d=unassigned_for(L)
        d.to_csv(os.path.join(a.out,f"unassigned_{L}.csv"),index=False,encoding="utf-8")
        counts[L]=(len(d),int((d.status=="UNCOVERED").sum()))

    tot=len(reg); nstem=reg["stem"].nunique()
    ncov=len(covered_stems); nunc=nstem-ncov
    print(f"2026 routes: {tot}   ->   distinct route families (stems): {nstem}")
    print(f"families covered by a tour (any language): {ncov}")
    print(f"families with NO tour in any language      : {nunc}   <- the real gap\n")
    print("unassigned routes per language  (total / of which UNCOVERED family):")
    for L in ["EN","DE","NL"]:
        print(f"   {L}: {counts[L][0]:>5} / {counts[L][1]:>4} uncovered")
    print(f"\nPriority: EN uncovered first, then DE; NL uncovered is OK to leave.")
    print(f"wrote stem_matrix.csv + unassigned_EN/DE/NL.csv to {a.out}/")

if __name__=="__main__": main()
