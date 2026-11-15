#!/usr/bin/env python3
"""Strip-map server: serves the site and looks lines up in Warsaw's (ZTM) timetable data.

    python3 server.py                      # then open http://localhost:8000
    python3 server.py --gtfs demo_gtfs.zip --no-geocode      # offline demo

What happens:
  * Data: the ZTM Warsaw GTFS feed (default https://mkuran.pl/gtfs/warsaw.zip, built from ZTM's
    official data, refreshed daily) is downloaded to data/ and converted ONCE into data/warsaw_lines.json
    (stops in order, "na żądanie" flags, wheelchair flags, connections). The first run takes a few minutes.
  * GET /api/lines        -> every line (id, type, headsign) for the search box
  * GET /api/line/<id>    -> that line with all its stops
  * Streets and districts are not in ZTM's data. They are looked up from each stop's coordinates
    with OpenStreetMap's Nominatim, at most 1 request/second (its usage policy), cached forever in
    data/geo_cache.sqlite and filled in gradually while the page is open.

Standard library only.
"""
import argparse
import collections
import copy
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import gtfs_to_json  # noqa: E402

DEFAULT_GTFS = "https://mkuran.pl/gtfs/warsaw.zip"
DEFAULT_GEOCODER = "https://nominatim.openstreetmap.org/reverse"
REFRESH_SECONDS = 24 * 3600
# which Nominatim address fields count as the "district" (first non-empty wins) and as the street
DISTRICT_KEYS = ("suburb", "city_district", "borough", "quarter", "neighbourhood")
STREET_KEYS = ("road", "pedestrian", "footway", "path", "cycleway")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- streets + districts
class Geocoder:
    """Background reverse geocoder with a persistent cache and a polite 1 request/second rate."""

    def __init__(self, url, user_agent, db_path, enabled=True, delay=1.1):
        self.url, self.ua, self.enabled, self.delay = url, user_agent, enabled, delay
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS geo (k TEXT PRIMARY KEY, street TEXT, district TEXT, ts REAL)")
        self.db.commit()
        self.lock = threading.Lock()
        self.todo = collections.deque()
        self.queued = set()
        self.cond = threading.Condition(self.lock)
        self.error_until = 0.0       # while now < error_until the geocoder is considered unavailable
        self.failures = 0
        if enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def key(lat, lon):
        return f"{lat:.4f},{lon:.4f}"          # ~11 m grid

    def get(self, lat, lon):
        with self.lock:
            r = self.db.execute("SELECT street, district FROM geo WHERE k=?", (self.key(lat, lon),)).fetchone()
        return r

    def want(self, points):
        """Queue (lat, lon) points; points of the line asked for last are looked up first."""
        with self.cond:
            for lat, lon in reversed(points):
                k = (round(lat, 4), round(lon, 4))
                if k in self.queued:
                    try:
                        self.todo.remove(k)
                    except ValueError:
                        continue            # currently being fetched
                else:
                    self.queued.add(k)
                self.todo.appendleft(k)
            self.cond.notify()

    @property
    def status(self):
        if not self.enabled:
            return "off"
        return "error" if time.time() < self.error_until else "ok"

    def _fetch(self, lat, lon):
        q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18,
                                    "addressdetails": 1, "accept-language": "pl"})
        sep = "&" if "?" in self.url else "?"
        req = urllib.request.Request(f"{self.url}{sep}{q}", headers={"User-Agent": self.ua})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.load(r)
        addr = j.get("address") or {}
        pick = lambda keys: next((addr[k] for k in keys if addr.get(k)), "")
        return pick(STREET_KEYS), pick(DISTRICT_KEYS)

    def _worker(self):
        while True:
            with self.cond:
                while not self.todo or time.time() < self.error_until:
                    self.cond.wait(timeout=max(1.0, self.error_until - time.time()) if self.todo else None)
                lat, lon = self.todo.popleft()
            try:
                street, district = self._fetch(lat, lon)
                with self.lock:
                    self.db.execute("INSERT OR REPLACE INTO geo VALUES (?,?,?,?)",
                                    (self.key(lat, lon), street, district, time.time()))
                    self.db.commit()
                    self.queued.discard((lat, lon))
                self.failures = 0
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code != 429:       # this point is rejected for good: remember it as empty
                    with self.lock:
                        self.db.execute("INSERT OR REPLACE INTO geo VALUES (?,?,?,?)", (self.key(lat, lon), "", "", time.time()))
                        self.db.commit()
                        self.queued.discard((lat, lon))
                    continue
                self._backoff(e, lat, lon)
                continue
            except Exception as e:                      # network down, timeout, ...
                self._backoff(e, lat, lon)
                continue
            time.sleep(self.delay)

    def _backoff(self, e, lat, lon):
        self.failures += 1
        backoff = min(300, 10 * 2 ** min(self.failures, 5))
        self.error_until = time.time() + backoff
        with self.cond:
            self.todo.append((lat, lon))
        log(f"geocoder: {e} - pausing {backoff}s")


# --------------------------------------------------------------------------- data
class Index:
    def __init__(self):
        self.by_id, self.listing = {}, []

    def load(self, path):
        with open(path, encoding="utf-8") as fh:
            lines = json.load(fh)["lines"]
        by_id, listing = {}, []
        for l in lines:
            by_id.setdefault(l["line"].lower(), l)
            listing.append({"line": l["line"], "type": l["type"], "name": l["directions"][0]["name"]})
        self.by_id, self.listing = by_id, listing
        log(f"index: {len(lines)} lines")


def download(url, dst):
    log(f"downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "sip-strip-map/1.0"})
    tmp = dst + ".part"
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
        total = 0
        while chunk := r.read(1 << 20):
            fh.write(chunk)
            total += len(chunk)
    os.replace(tmp, dst)
    log(f"downloaded {total / 1e6:.1f} MB")


def refresh(src, overrides, force=False):
    """Make sure data/lines.json is built from a GTFS less than a day old. Returns the json path."""
    out = os.path.join(DATA, "warsaw_lines.json")
    zip_path = src
    if src.startswith(("http://", "https://")):
        zip_path = os.path.join(DATA, "warsaw_gtfs.zip")
        stale = not os.path.exists(zip_path) or time.time() - os.path.getmtime(zip_path) > REFRESH_SECONDS
        if stale or force:
            try:
                download(src, zip_path)
            except Exception as e:
                if not os.path.exists(zip_path):
                    raise SystemExit(f"Could not download the timetable data ({e}). "
                                     f"Download it yourself and run: python3 server.py --gtfs path/to/warsaw.zip")
                log(f"download failed ({e}); using the copy from {time.ctime(os.path.getmtime(zip_path))}")
    if force or not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(zip_path):
        tmp = out + ".new"
        gtfs_to_json.build(zip_path, tmp, overrides, log)
        os.replace(tmp, out)
    return out


# --------------------------------------------------------------------------- HTTP
INDEX = Index()
GEO = None


def line_payload(line_id):
    line = INDEX.by_id.get(line_id.strip().lower())
    if line is None:
        return None
    out = copy.deepcopy(line)
    missing, points = 0, []
    for d in out["directions"]:
        for s in d["stops"]:
            if "lat" not in s:
                continue
            g = GEO.get(s["lat"], s["lon"]) if GEO.enabled else None
            if g is None:
                points.append((s["lat"], s["lon"]))
                missing += 1
                continue
            street, district = g
            if street:
                s["street"] = street
            if district and not s.get("district"):
                s["district"] = district
    if points:
        GEO.want(points)
    out["geocoder"] = GEO.status
    out["enriching"] = missing if GEO.status == "ok" else 0
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/" not in self.path:
            super().log_message(fmt, *args)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/lines":
            return self._json({"lines": INDEX.listing})
        if path.startswith("/api/line/"):
            payload = line_payload(urllib.parse.unquote(path[len("/api/line/"):]))
            return self._json(payload) if payload else self._json({"error": "not found"}, 404)
        if path.startswith(("/data/geo_cache", "/data/warsaw_gtfs", "/server.py", "/tools/")):
            return self.send_error(404)             # don't serve internals
        return super().do_GET()


def main():
    global GEO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs", default=DEFAULT_GTFS, help="GTFS zip: URL or local path (default: %(default)s)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from other devices")
    ap.add_argument("--overrides", help="optional CSV: stop_name,district,poi,on_request,wheelchair")
    ap.add_argument("--no-geocode", action="store_true", help="don't look up streets/districts")
    ap.add_argument("--geocoder", default=DEFAULT_GEOCODER, help="Nominatim-compatible reverse-geocoding URL")
    ap.add_argument("--contact", default="", help="your e-mail/site, sent in the User-Agent (Nominatim asks for this)")
    ap.add_argument("--rebuild", action="store_true", help="re-download and re-convert now")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    ua = f"sip-strip-map/1.0 ({args.contact or 'personal project'})"
    GEO = Geocoder(args.geocoder, ua, os.path.join(DATA, "geo_cache.sqlite"), enabled=not args.no_geocode)
    INDEX.load(refresh(args.gtfs, args.overrides, args.rebuild))

    def daily():
        while True:
            time.sleep(3600)
            try:
                path = os.path.join(DATA, "warsaw_lines.json")
                if time.time() - os.path.getmtime(path) > REFRESH_SECONDS:
                    INDEX.load(refresh(args.gtfs, args.overrides, True))
            except Exception as e:
                log(f"refresh failed: {e}")
    threading.Thread(target=daily, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"open http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
