#!/usr/bin/env python3
"""
rwgps.py — Ride with GPS provider for the route-notes tools.

Pulls a route's TRACK, POINTS OF INTEREST, and — the reason this exists —
its COURSE POINTS: the author's own cue sheet (turn text + coordinates) that
the GPX export throws away. Those cues are what fill the gap placeholders in
gpx_to_routenotes.py with real, human-written directions.

CREDENTIALS (one-time, in your RideWithGPS account)
  Account -> Settings -> Developers -> create an API client   -> gives api_key
  On that client's edit page -> "Create new Auth Token"       -> gives auth_token
  Then either pass --apikey/--auth-token or set env vars:
      export RWGPS_API_KEY=...      export RWGPS_AUTH_TOKEN=...

USAGE
  python3 rwgps.py list                         # your routes (id + name + km)
  python3 rwgps.py route 12345678               # print that route's cue sheet
  python3 rwgps.py route 12345678 --json out.json

NOTE ON AUTH
  Confirmed against the RWGPS OpenAPI spec: authenticate with two request
  HEADERS on every call — x-rwgps-api-key and x-rwgps-auth-token. Both come
  from your API client (api_key on creation; auth_token via the client's
  Basic Authentication section).
"""

import argparse, json, os, sys, urllib.request, urllib.parse

API = "https://ridewithgps.com/api/v1"

# ---------- auth + fetch (the one place to verify against your client page) ----------
def _creds(args):
    key = getattr(args, "apikey", None) or os.environ.get("RWGPS_API_KEY")
    tok = getattr(args, "auth_token", None) or os.environ.get("RWGPS_AUTH_TOKEN")
    if not (key and tok):
        sys.exit("Missing credentials. Create an API client in RideWithGPS "
                 "(Account -> Settings -> Developers), make an Auth Token on it, then set\n"
                 "  export RWGPS_API_KEY=...   export RWGPS_AUTH_TOKEN=...\n"
                 "or pass --apikey / --auth-token.")
    return key, tok

def _auth_headers(args):
    key, tok = _creds(args)
    # RWGPS v1 authenticates via headers (confirmed against the OpenAPI spec).
    return {"x-rwgps-api-key": key, "x-rwgps-auth-token": tok}

def api_get(path, args, params=None):
    q = dict(params or {})
    url = f"{API}/{path}.json" + ("?" + urllib.parse.urlencode(q) if q else "")
    headers = {"User-Agent": "knp-routenotes/1.0", "Accept": "application/json"}
    headers.update(_auth_headers(args))
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("401 from RWGPS — credentials rejected. Check api_key/auth_token, "
                     "and confirm the auth convention on your API client page.")
        sys.exit(f"RWGPS HTTP {e.code}: {e.reason}")
    except Exception as e:
        sys.exit(f"Could not reach RWGPS ({e}).")

# ---------- endpoints ----------
def list_routes(args, page=1, page_size=50):
    js = api_get("routes", args, {"page": page, "page_size": page_size})
    return js.get("routes", []), js.get("meta", {}).get("pagination", {})

def get_route(route_id, args):
    return api_get(f"routes/{route_id}", args)

# ---------- transform: RWGPS route JSON -> clean structure ----------
def _f(d, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default

def extract(route_json):
    """Return {name, distance_m, track:[(lat,lon)], cues:[...], pois:[...]}.
    Defensive about field names, since a live response is the source of truth."""
    r = route_json.get("route", route_json)

    track = []
    for p in r.get("track_points", []):
        lat, lon = _f(p, "y", "lat"), _f(p, "x", "lng", "lon")
        if lat is not None and lon is not None:
            track.append((float(lat), float(lon)))

    cues = []
    for c in r.get("course_points", []):
        lat, lon = _f(c, "y", "lat"), _f(c, "x", "lng", "lon")
        cues.append({
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "m":   _f(c, "d", "distance", "dist"),                 # metres along route
            "type": (_f(c, "t", "type", default="") or "").strip(),  # turn type
            "text": (_f(c, "n", "note", "description", "name", default="") or "").strip(),
        })

    pois = []
    for item in route_json.get("extras", []):        # export-style container
        if item.get("type") == "point_of_interest":
            p = item["point_of_interest"]
            pois.append({"type": p.get("type", ""), "lat": p.get("lat"), "lon": _f(p, "lng", "lon"),
                         "name": p.get("name", ""), "desc": p.get("description", "")})
    for p in r.get("points_of_interest", []):          # inline container
        pois.append({"type": p.get("type", ""), "lat": _f(p, "lat", "y"), "lon": _f(p, "lng", "lon", "x"),
                     "name": p.get("name", ""), "desc": p.get("description", "")})

    return {"name": r.get("name", ""), "distance_m": r.get("distance"),
            "track": track, "cues": cues, "pois": pois}

def cue_text(c):
    """One clean instruction line from a course point (text usually already complete)."""
    t = c["text"] or (c["type"].capitalize() if c["type"] else "Continue")
    return t

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Ride with GPS route + cue fetcher.")
    ap.add_argument("cmd", choices=["list", "route"])
    ap.add_argument("route_id", nargs="?")
    ap.add_argument("--apikey"); ap.add_argument("--auth-token", dest="auth_token")
    ap.add_argument("--json", help="write raw route JSON here")
    args = ap.parse_args()

    if args.cmd == "list":
        routes, pg = list_routes(args)
        for r in routes:
            km = (r.get("distance") or 0) / 1000
            print(f"  {r.get('id'):>10}  {km:6.1f} km  {r.get('name','')}")
        print(f"\n[{pg.get('record_count','?')} routes total]")
        return

    if not args.route_id:
        sys.exit("route needs an id: python3 rwgps.py route <id>")
    rj = get_route(args.route_id, args)
    if args.json:
        json.dump(rj, open(args.json, "w"), indent=2); print(f"wrote {args.json}")
    data = extract(rj)
    km = (data["distance_m"] or 0) / 1000
    print(f"\n{data['name']}   {km:.1f} km   ({len(data['cues'])} cues, {len(data['pois'])} POIs)\n")
    for c in data["cues"]:
        at = f"{c['m']/1000:5.1f} km" if isinstance(c["m"], (int, float)) else "   ? km"
        print(f"  {at}   {cue_text(c)}")

if __name__ == "__main__":
    main()
