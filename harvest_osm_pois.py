#!/usr/bin/env python3
"""
harvest_osm_pois.py - pull candidate POIs from OpenStreetMap along a GPX route,
via the Overpass API. Output is a POI sidecar in the house shape, with every
point stamped source="osm" and verified=false -> it drops straight into the
verify queue (poi_verify) or a poi_master merge. OSM is a *source, not a truth*.

    python harvest_osm_pois.py --gpx ROUTE.gpx --out ROUTE.pois.json
    python harvest_osm_pois.py --gpx ROUTE.gpx --types toilets,water,repair,bakery --radius 60

Types (add more in TAGS - each is one line):
    toilets water repair bikeparking bakery cafe supermarket atm viewpoint ferry swim pharmacy

Stdlib only. Live fetch needs internet (runs on your machine, not the sandbox).
Attribution: data (c) OpenStreetMap contributors, ODbL - credit it on guest docs.
"""
import argparse, json, math, re, sys, urllib.request, urllib.parse

OVERPASS = "https://overpass-api.de/api/interpreter"
# label, list of OSM tag filters, house POI type
TAGS = {
 "toilets":     ("Public toilet", ['node[amenity=toilets]'],                      "Toilet"),
 "water":       ("Drinking water", ['node[amenity=drinking_water]','node[man_made=water_tap]'], "Water"),
 "repair":      ("Bike repair/pump",['node[amenity=bicycle_repair_station]','node[amenity=compressed_air]'], "Bike"),
 "bikeparking": ("Bike parking",   ['node[amenity=bicycle_parking]'],             "Bike Parking"),
 "bakery":      ("Bakery",         ['node[shop=bakery]'],                          "Coffee"),
 "cafe":        ("Cafe",           ['node[amenity=cafe]'],                         "Coffee"),
 "supermarket": ("Supermarket",    ['node[shop=supermarket]','node[shop=convenience]'], "Food"),
 "atm":         ("ATM",            ['node[amenity=atm]'],                          "ATM"),
 "viewpoint":   ("Viewpoint",      ['node[tourism=viewpoint]'],                    "Viewpoint"),
 "ferry":       ("Ferry",          ['node[amenity=ferry_terminal]'],               "Ferry"),
 "swim":        ("Swim spot",      ['node[leisure=swimming_area]','node[natural=beach]'], "Swimming"),
 "pharmacy":    ("Pharmacy",       ['node[amenity=pharmacy]'],                     "Caution"),
}
def hav(a,b,c,d):
    R=6371000; p1,p2=math.radians(a),math.radians(c)
    dp=math.radians(c-a); dl=math.radians(d-b)
    x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(x))

def read_track(fp):
    t=open(fp,encoding="utf-8-sig",errors="replace").read()
    lat=[float(x) for x in re.findall(r'lat="([-\d.]+)"',t)]
    lon=[float(x) for x in re.findall(r'lon="([-\d.]+)"',t)]
    pts=list(zip(lat,lon))
    cum=[0.0]
    for i in range(1,len(pts)): cum.append(cum[-1]+hav(*pts[i-1],*pts[i]))
    return pts,cum

def thin(pts,spacing=200.0):
    if not pts: return []
    kept=[pts[0]]; last=pts[0]
    for p in pts[1:]:
        if hav(*last,*p)>=spacing: kept.append(p); last=p
    if kept[-1]!=pts[-1]: kept.append(pts[-1])
    return kept

def km_along(pt,pts,cum):
    best=1e18; bi=0
    for i,q in enumerate(pts):
        d=hav(pt[0],pt[1],q[0],q[1])
        if d<best: best=d; bi=i
    return round(cum[bi]/1000.0,1), round(best)

def build_query(kept,radius,types):
    coords=",".join(f"{a:.5f},{b:.5f}" for a,b in kept)
    body=[]
    for t in types:
        for filt in TAGS[t][1]:
            body.append(f'{filt}(around:{radius},{coords});')
    return f"[out:json][timeout:120];(\n"+"\n".join(body)+"\n);out body;"

def parse_elements(elements,pts,cum,radius):
    out=[]; seen=[]
    for e in elements:
        if e.get("type")!="node": continue
        tags=e.get("tags",{}); la,lo=e.get("lat"),e.get("lon")
        if la is None: continue
        label=htype=None
        if tags.get("amenity")=="toilets": label,htype="Public toilet","Toilet"
        elif tags.get("amenity")=="drinking_water" or tags.get("man_made")=="water_tap": label,htype="Drinking water","Water"
        elif tags.get("amenity") in ("bicycle_repair_station","compressed_air"): label,htype="Bike repair/pump","Bike"
        elif tags.get("amenity")=="bicycle_parking": label,htype="Bike parking","Bike Parking"
        elif tags.get("shop")=="bakery": label,htype="Bakery","Coffee"
        elif tags.get("amenity")=="cafe": label,htype="Cafe","Coffee"
        elif tags.get("shop") in ("supermarket","convenience"): label,htype="Shop","Food"
        elif tags.get("amenity")=="atm": label,htype="ATM","ATM"
        elif tags.get("tourism")=="viewpoint": label,htype="Viewpoint","Viewpoint"
        elif tags.get("amenity")=="ferry_terminal": label,htype="Ferry","Ferry"
        elif tags.get("leisure")=="swimming_area" or tags.get("natural")=="beach": label,htype="Swim spot","Swimming"
        elif tags.get("amenity")=="pharmacy": label,htype="Pharmacy","Caution"
        else: continue
        if any(ht==htype and hav(la,lo,sa,so)<25 for sa,so,ht in seen): continue
        seen.append((la,lo,htype))
        km,off=km_along((la,lo),pts,cum)
        desc_bits=[]
        for k in ("opening_hours","fee","wheelchair","access","drinking_water"):
            if k in tags: desc_bits.append(f"{k}={tags[k]}")
        out.append({"name":tags.get("name",label),"type":htype,"lat":round(la,6),"lon":round(lo,6),
                    "desc":"; ".join(desc_bits),"km":km,"off_m":off,
                    "source":"osm","osm_id":e.get("id"),"verified":False})
    out.sort(key=lambda p:p["km"])
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--gpx",required=True); ap.add_argument("--out",default="osm.pois.json")
    ap.add_argument("--radius",type=int,default=50); ap.add_argument("--spacing",type=float,default=200.0)
    ap.add_argument("--types",default="toilets,water")
    ap.add_argument("--print-query",action="store_true")
    a=ap.parse_args()
    types=[t.strip() for t in a.types.split(",") if t.strip() in TAGS]
    bad=[t for t in a.types.split(",") if t.strip() and t.strip() not in TAGS]
    if bad: print("unknown types ignored:",bad)
    pts,cum=read_track(a.gpx)
    if not pts: sys.exit("no track points in GPX")
    kept=thin(pts,a.spacing)
    q=build_query(kept,a.radius,types)
    print(f"route {cum[-1]/1000:.1f} km | {len(pts)} pts -> {len(kept)} query points | types: {','.join(types)}")
    if a.print_query: print(q[:400]+" ..."); return
    data=urllib.parse.urlencode({"data":q}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(OVERPASS,data=data),timeout=180) as r:
            js=json.load(r)
    except Exception as e:
        sys.exit(f"Overpass fetch failed ({e}). Needs internet; try again (rate-limited).")
    pois=parse_elements(js.get("elements",[]),pts,cum,a.radius)
    json.dump(pois,open(a.out,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
    from collections import Counter
    c=Counter(p["type"] for p in pois)
    print(f"found {len(pois)} candidate POIs: {dict(c)}")
    print(f"wrote {a.out}  (all source=osm, verified=false -> send through poi_verify)")
    print("Attribution: data (c) OpenStreetMap contributors (ODbL) - credit on guest docs.")

if __name__=="__main__": main()
