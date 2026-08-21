#!/usr/bin/env python3
"""
flag_experiences.py - flag dead RWGPS Experiences for archiving.

Rule: an experience with all-zero stats has never been used -> archive.
Safety split: all-zero experiences named 2026/2027 with no archival keyword are
flagged ARCHIVE? (maybe built-but-not-launched) so upcoming tours aren't archived.

Input: the experiences export (tab- or space-separated); each line ends with the
three stat numbers. Header and short/broken lines are skipped and counted.

    python flag_experiences.py --in experiences.txt --out experiences_flagged.csv
"""
import argparse, csv, re, sys

LITERAL_TOKENS = ["nicht verwenden","do not use","old routes","archief","archive",
                  "to be deleted","deleted","untitled","concept","fleur (archief",
                  "gandalf (archief","zwaantje (archief","fiep (archief"]
WORD_TOKENS = ["oud","test"]
LINE = re.compile(r"^(.*\S)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")

def signal(name):
    n=name.lower()
    hits=[t for t in LITERAL_TOKENS if t in n]
    hits+=[w for w in WORD_TOKENS if re.search(rf"\b{w}\b",n)]
    return ";".join(sorted(set(hits)))

def year_of(name):
    ys=[int(y) for y in re.findall(r"\b(20\d{2})\b",name)]
    return max(ys) if ys else None

def classify(nums, name):
    allzero = all(v==0 for v in nums)
    sig = signal(name); yr = year_of(name)
    if allzero:
        if sig or (yr and yr<2026):
            return "ARCHIVE","",sig,yr
        if yr and yr>=2026:
            return "ARCHIVE?","all-zero but 2026/27 - confirm it isn't an upcoming build",sig,yr
        return "ARCHIVE","",sig,yr          # untitled/test/no-year zeros
    # has activity
    if sig or (yr and yr<=2024):
        return "REVIEW","active but name/age looks archival",sig,yr
    return "KEEP","",sig,yr

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--in",dest="inp",required=True)
    ap.add_argument("--out",default="experiences_flagged.csv")
    a=ap.parse_args()

    rows=[]; unparsed=0
    for line in open(a.inp,encoding="utf-8-sig"):
        line=line.rstrip("\n")
        if not line.strip(): continue
        m=LINE.match(line)
        if not m: unparsed+=1; continue
        name=m.group(1).strip(); nums=[int(m.group(2)),int(m.group(3)),int(m.group(4))]
        status,note,sig,yr=classify(nums,name)
        rows.append({"experience":name,"this_year":nums[0],"mid":nums[1],"total":nums[2],
                     "all_zero":"Y" if all(v==0 for v in nums) else "","year":yr or "",
                     "signal":sig,"status":status,"note":note})
    with open(a.out,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["experience","this_year","mid","total","all_zero","year","signal","status","note"])
        w.writeheader()
        for r in rows: w.writerow(r)

    from collections import Counter
    c=Counter(r["status"] for r in rows)
    print(f"parsed {len(rows)} experiences  ({unparsed} lines skipped: header/short)")
    for k in ["KEEP","ARCHIVE","ARCHIVE?","REVIEW"]:
        print(f"   {k:9} {c.get(k,0)}")
    print(f"wrote {a.out}")

if __name__=="__main__": main()
