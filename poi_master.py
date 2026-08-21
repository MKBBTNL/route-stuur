#!/usr/bin/env python3
"""
poi_master.py — the POI cache: one master database, per-route picking.

  merge  <master.json> <route.gpx ...>   add POIs from GPX waypoints
                                          (dedup by proximity+name, stable id;
                                           EN filled, NL/DE left blank = to-translate)
  pick   <master.json> <route.gpx>        list master POIs within --cut of the
                                          route track, ordered by distance along it

The master is the source of truth and the translation store. Routes never hold
POIs of their own — they pull from here.
"""
import sys, json, math, re, argparse, csv, os
import xml.etree.ElementTree as ET

NS = {'g': 'http://www.topografix.com/GPX/1/1'}


def hav(a, b):
    R = 6371000.0
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2-la1), math.radians(lo2-lo1)
    h = math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def load_master(path):
    try:
        return json.load(open(path, encoding='utf-8'))
    except FileNotFoundError:
        return []


def track(path):
    r = ET.parse(path).getroot()
    return [(float(p.get('lat')), float(p.get('lon')))
            for p in r.findall('.//g:trkpt', NS)]


def waypoints(path):
    r = ET.parse(path).getroot()
    out = []
    for w in r.findall('g:wpt', NS):
        out.append({'pt': (float(w.get('lat')), float(w.get('lon'))),
                    'name': w.findtext('g:name', '', NS),
                    'type': w.findtext('g:cmt', '', NS),
                    'desc': (w.findtext('g:desc', '', NS) or '').replace('\n', ' ').strip()})
    return out


def cmd_merge(a):
    master = load_master(a.master)
    index = {p['id']: p for p in master}
    added = 0
    for gpx in a.gpx:
        for w in waypoints(gpx):
            sid = slug(w['name'])
            near = next((p for p in master
                         if hav((p['lat'], p['lon']), w['pt']) < 30), None)
            if sid in index or near:
                continue                       # already in the master
            master.append({'id': sid, 'lat': w['pt'][0], 'lon': w['pt'][1],
                           'type': w['type'],
                           'name': {'en': w['name'], 'nl': '', 'de': ''},
                           'desc': {'en': w['desc'], 'nl': '', 'de': ''},
                           'source': gpx.split('/')[-1]})
            index[sid] = master[-1]
            added += 1
    json.dump(master, open(a.master, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    todo = sum(1 for p in master if not p['name']['nl'] or not p['name']['de'])
    print(f"merged {added} new POIs -> {len(master)} total "
          f"({todo} still need NL/DE)")


def cmd_pick(a):
    master = load_master(a.master)
    trk = track(a.gpx)
    cum = [0.0]
    for i in range(1, len(trk)):
        cum.append(cum[-1]+hav(trk[i-1], trk[i]))
    hits = []
    for p in master:
        if p.get('lat') is None or p.get('lon') is None:
            continue                       # not yet geocoded -> can't place
        best, bi = 1e18, 0
        for i, q in enumerate(trk):
            d = hav((p['lat'], p['lon']), q)
            if d < best:
                best, bi = d, i
        if best <= a.cut:
            hits.append((cum[bi], best, p))
    hits.sort(key=lambda x: x[0])
    print(f"# {a.gpx.split('/')[-1]}: {len(hits)}/{len(master)} master POIs "
          f"within {a.cut:.0f} m  (lang={a.lang})\n")
    for km, off, p in hits:
        print(f"  {km/1000:5.1f} km  {p['name'].get(a.lang) or p['name']['en']:34.34}"
              f" [{p['type']}]  {round(off)} m")
    if a.emit:
        sidecar = [{'name': p['name'].get(a.lang) or p['name']['en'],
                    'type': p['type'], 'lat': p['lat'], 'lon': p['lon'],
                    'desc': p['desc'].get(a.lang) or p['desc'].get('en', '')}
                   for _, _, p in hits]
        json.dump(sidecar, open(a.emit, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        print(f"\n-> wrote {a.emit}  ({len(sidecar)} POIs, {a.lang})")


def cmd_view(a):
    master = load_master(a.master)
    cols = ['id', 'type', 'locality', 'lat', 'lon',
            'name_en', 'name_nl', 'name_de',
            'desc_en', 'desc_nl', 'desc_de', 'source', 'todo']
    with open(a.out, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for p in master:
            todo = []
            if p.get('lat') is None:
                todo.append('coords')
            if not p['name'].get('nl') or not p['name'].get('de'):
                todo.append('translate')
            w.writerow({'id': p['id'], 'type': p.get('type', ''),
                        'locality': p.get('locality', ''),
                        'lat': p.get('lat', ''), 'lon': p.get('lon', ''),
                        'name_en': p['name'].get('en', ''),
                        'name_nl': p['name'].get('nl', ''),
                        'name_de': p['name'].get('de', ''),
                        'desc_en': p['desc'].get('en', ''),
                        'desc_nl': p['desc'].get('nl', ''),
                        'desc_de': p['desc'].get('de', ''),
                        'source': p.get('source', ''),
                        'todo': '+'.join(todo)})
    print(f"wrote {a.out}: {len(master)} rows, {len(cols)} columns "
          f"(open in Sheets/Excel to filter & sort)")


def cmd_harvest(a):
    import glob as _glob
    paths = _glob.glob(os.path.join(a.path, '*.json')) if os.path.isdir(a.path) else _glob.glob(a.path)
    POI_KEYS = ('points_of_interest', 'pois', 'poi')
    def find_pois(obj):
        if isinstance(obj, dict):
            for k in POI_KEYS:
                if isinstance(obj.get(k), list):
                    return obj[k]
            for v in obj.values():          # one level down (e.g. {"route": {...}})
                r = find_pois(v)
                if r:
                    return r
        return []
    def field(p, *names):
        for n in names:
            if p.get(n) not in (None, ''):
                return p[n]
        return None
    if a.inspect:
        for fp in paths:
            try:
                pois = find_pois(json.load(open(fp, encoding='utf-8')))
            except Exception:
                continue
            if pois:
                print(f"{os.path.basename(fp)}: {len(pois)} POIs; first POI keys = "
                      f"{sorted(pois[0].keys())}")
                return
        print("No POIs found in any file scanned.")
        return
    master = load_master(a.master)
    index = {p['id'] for p in master}
    added = files_with = 0
    for fp in paths:
        try:
            pois = find_pois(json.load(open(fp, encoding='utf-8')))
        except Exception:
            continue
        if pois:
            files_with += 1
        for p in pois:
            name = field(p, 'name')
            lat = field(p, 'lat', 'latitude')
            lon = field(p, 'lng', 'lon', 'longitude')
            if not name or lat is None or lon is None:
                continue
            lat, lon = float(lat), float(lon)
            sid = slug(name)
            near = next((q for q in master
                         if q.get('lat') is not None
                         and hav((q['lat'], q['lon']), (lat, lon)) < 30), None)
            if sid in index or near:
                continue
            desc = field(p, 'description', 'desc') or ''
            typ = field(p, 'type_name', 'type') or ''
            master.append({'id': sid, 'lat': lat, 'lon': lon, 'locality': '',
                           'type': typ, 'name': {'en': name, 'nl': '', 'de': ''},
                           'desc': {'en': desc, 'nl': '', 'de': ''},
                           'source': os.path.basename(fp)})
            index.add(sid); added += 1
    json.dump(master, open(a.master, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f"harvested {added} new POIs from {files_with} route file(s) with POIs "
          f"({len(paths)} scanned) -> master {len(master)} total")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('merge'); m.add_argument('master'); m.add_argument('gpx', nargs='+')
    m.set_defaults(fn=cmd_merge)
    p = sub.add_parser('pick'); p.add_argument('master'); p.add_argument('gpx')
    p.add_argument('--cut', type=float, default=250.0)
    p.add_argument('--lang', default='en')
    p.add_argument('--emit', help='write <id>.pois.json sidecar in Marc\'s shape')
    p.set_defaults(fn=cmd_pick)
    v = sub.add_parser('view'); v.add_argument('master')
    v.add_argument('--out', default='master_view.csv'); v.set_defaults(fn=cmd_view)
    h = sub.add_parser('harvest'); h.add_argument('master'); h.add_argument('path')
    h.add_argument('--inspect', action='store_true',
                   help='print the POI field names found, write nothing')
    h.set_defaults(fn=cmd_harvest)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
