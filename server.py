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
# The City of Warsaw's open-data API (new portal). Keys from https://dane.um.warszawa.pl/pl/key-api are JWTs
# sent RAW in the Authorization header (no "Bearer "), with POST /api/action/<action> and a JSON body.
CITY_API_HOST = "dane.um.warszawa.pl"
CITY_API_BASE = f"https://{CITY_API_HOST}/api/action/"
TOKEN_FILE = os.path.join(ROOT, "api_token.txt")          # git-ignored; never served (see BLOCKED below)
PROBE_FILE = os.path.join(DATA, "api_probe.txt")
# which Nominatim address fields count as the "district" (first non-empty wins) and as the street
DISTRICT_KEYS = ("suburb", "city_district", "borough", "quarter", "neighbourhood")
STREET_KEYS = ("road", "pedestrian", "footway", "path", "cycleway")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- City of Warsaw API
def load_token(cli_value=""):
    """API token from --api-token, then $WARSAW_API_TOKEN, then the first non-comment line of api_token.txt."""
    if cli_value and cli_value.strip():
        return cli_value.strip()
    env = os.environ.get("WARSAW_API_TOKEN", "").strip()
    if env:
        return env
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-send the Authorization header to wherever it points: refuse instead."""

    def redirect_request(self, *a, **kw):
        return None


class CityAPI:
    """Minimal client for https://dane.um.warszawa.pl. The token is only ever sent to that host,
    from this server (never to the browser)."""

    def __init__(self, token, user_agent):
        self.token, self.ua = token, user_agent
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(self, action, body=None, timeout=25):
        """POST one action. Returns (http_status, response_bytes); HTTP errors are returned, not raised
        (the new host answers 500 for every rejected request)."""
        url = CITY_API_BASE + urllib.parse.quote(action, safe="_")
        if urllib.parse.urlparse(url).hostname != CITY_API_HOST:       # belt and braces
            raise ValueError("refusing to send the token to another host")
        req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST", headers={
            "Authorization": self.token, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": self.ua})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


def probe_api(api, path=PROBE_FILE):
    """Call the City API actions known from its public docs with a few plausible request bodies and write
    status + the start of every answer to `path`, so the real field names can be checked. The token is never written."""
    stop, pole = "6045", "03"                      # the example stop used in the City's own documentation
    tries = [
        ("get_ztm_lista_linii_na_przystanku", {"busstopId": stop, "busstopNr": pole}),
        ("get_ztm_lista_linii_na_przystanku", {"zespol": stop, "slupek": pole}),
        ("get_ztm_lista_linii_na_przystanku", {}),
        ("get_ztm_odjazdy_linii_z_przystanku", {"busstopId": stop, "busstopNr": pole, "line": "256"}),
        ("get_ztm_odjazdy_linii_z_przystanku", {"busstopId": stop, "busstopNr": pole}),
        ("get_ztm_lokalizacja_pojazdow", {"type": 1}),
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [f"City API probe, {time.strftime('%Y-%m-%d %H:%M:%S')}  (host {CITY_API_HOST}; token not included)", ""]
    for action, body in tries:
        try:
            status, raw = api.call(action, body)
            text = raw.decode("utf-8", "replace")
            try:
                data = json.loads(text)
                kind = f"JSON {type(data).__name__}" + (f" with {len(data)} items" if isinstance(data, (list, dict)) else "")
            except ValueError:
                kind = "not JSON"
            lines += [f"== {action}  body={json.dumps(body)}", f"   HTTP {status}, {len(raw)} bytes, {kind}",
                      "   " + text[:700].replace("\n", " "), ""]
        except Exception as e:
            lines += [f"== {action}  body={json.dumps(body)}", f"   failed: {e}", ""]
        log(lines[-3] if len(lines) >= 3 else lines[-1])
        time.sleep(0.5)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log(f"probe written to {path}")


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


# never served over HTTP: internals, caches, the API token and anything starting with "." (.git, .gitignore ...)
BLOCKED = ("/data/geo_cache", "/data/warsaw_gtfs", "/data/api_probe", "/server.py", "/tools/", "/api_token")


def is_blocked(path):
    p = urllib.parse.unquote(path)
    return p.startswith(BLOCKED) or "/." in p


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_HEAD(self):
        if is_blocked(urllib.parse.urlparse(self.path).path):
            return self.send_error(404)
        return super().do_HEAD()

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
        if is_blocked(path):
            return self.send_error(404)             # don't serve internals, secrets or .git
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
    ap.add_argument("--api-token", default="", help="City of Warsaw API token (or $WARSAW_API_TOKEN, or api_token.txt)")
    ap.add_argument("--probe-api", action="store_true",
                    help="test the City API token, write what the API answers to data/api_probe.txt, then exit")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    ua = f"sip-strip-map/1.0 ({args.contact or 'personal project'})"
    token = load_token(args.api_token)
    if args.probe_api:
        if not token:
            raise SystemExit("No token: put it in api_token.txt, set $WARSAW_API_TOKEN or use --api-token.")
        probe_api(CityAPI(token, ua))
        return
    log("City API token: " + ("found (used by --probe-api)" if token else "none (not needed for line lookup)"))
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
