#!/usr/bin/env python3
"""Convert a GTFS zip (Warsaw: ZTM data) into data/lines.json for the strip-map site.

Usage:  python3 tools/gtfs_to_json.py path/to/gtfs.zip data/lines.json [overrides.csv]

Streams stop_times.txt, so the full Warsaw feed (hundreds of MB unzipped) needs
little memory. For every route and direction it picks the most common stop
pattern (short-turn variants don't win), takes the order from stop_sequence and
minutes from the first stop, and builds "connections" for every stop: all OTHER
routes serving a stop with the same name within CONN_RADIUS_M metres.

Read from GTFS:
  wheelchair_boarding = 2 (stops.txt)             -> "wheelchair": "none"
  pickup_type or drop_off_type = 3 (stop_times)    -> "onRequest": true  (na żądanie)
  stop_lat / stop_lon                              -> "lat" / "lon" (used by server.py for streets and districts)

overrides.csv (optional) has the header "stop_name,district[,poi,on_request,wheelchair]";
its values win over everything else (GTFS has no district data).
  on_request  1/yes/true/tak     wheelchair  none | partial | full

Standard library only.
"""
import csv
import io
import json
import math
import re
import sys
import zipfile
from collections import Counter, defaultdict

TYPE_NAMES = {0: "tram", 1: "metro", 2: "train", 3: "bus", 11: "trolleybus", 800: "trolleybus",
              700: "bus", 900: "tram", 400: "metro", 100: "train", 109: "train"}
TYPE_ORDER = {"metro": 0, "train": 1, "tram": 2, "trolleybus": 3, "bus": 4}
CONN_RADIUS_M = 400          # same-name stops further apart than this are not treated as one interchange


def rows(zf, name):
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))


def to_minutes(t):
    h, m, s = (int(x) for x in t.strip().split(":"))
    return h * 60 + m + s / 60  # GTFS allows hours >= 24


def natural_key(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def metres(a, b):
    """Rough distance between two (lat, lon) pairs; 0 if either is unknown."""
    if a is None or b is None:
        return 0
    dy = (a[0] - b[0]) * 111_200
    dx = (a[1] - b[1]) * 111_200 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def stream_stop_times(zf, trips, log):
    """Yield (trip_id, [(seq, stop_id, time, on_request), ...]) per trip.
    Assumes the rows of one trip are contiguous (true for the Warsaw feed)."""
    with zf.open("stop_times.txt") as fh:
        rd = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
        h = {n: i for i, n in enumerate(next(rd))}
        it, ia, id_, is_, iseq = h["trip_id"], h.get("arrival_time"), h.get("departure_time"), h["stop_id"], h["stop_sequence"]
        ipu, idr = h.get("pickup_type"), h.get("drop_off_type")
        cur, buf, n = None, [], 0
        for r in rd:
            n += 1
            if n % 2_000_000 == 0:
                log(f"  ... {n:,} stop_times rows")
            tid = r[it]
            if tid != cur:
                if buf:
                    yield cur, buf
                cur, buf = tid, []
            if tid not in trips:
                continue
            t = (r[ia] if ia is not None else "") or (r[id_] if id_ is not None else "")
            req = (ipu is not None and r[ipu].strip() == "3") or (idr is not None and r[idr].strip() == "3")
            buf.append((int(r[iseq]), r[is_], t, req))
        if buf:
            yield cur, buf


def build(src, dst, overrides_csv=None, log=print):
    zf = zipfile.ZipFile(src)

    over = {}
    if overrides_csv:
        with open(overrides_csv, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                flag = (r.get("on_request") or "").strip().lower()
                over[r["stop_name"].strip()] = {
                    "district": (r.get("district") or "").strip(),
                    "poi": (r.get("poi") or "").strip(),
                    "on_request": (flag in ("1", "yes", "true", "tak")) if flag else None,
                    "wheelchair": (r.get("wheelchair") or "").strip().lower(),
                }

    log("reading routes, stops, trips ...")
    routes = {r["route_id"]: r for r in rows(zf, "routes.txt")}
    stops = {}
    for s in rows(zf, "stops.txt"):
        try:
            pos = (float(s["stop_lat"]), float(s["stop_lon"]))
        except (KeyError, ValueError):
            pos = None
        stops[s["stop_id"]] = {"name": s["stop_name"].strip(), "pos": pos,
                               "wc": (s.get("wheelchair_boarding") or "").strip()}
    trips = {t["trip_id"]: (t["route_id"], t.get("direction_id") or "0")
             for t in rows(zf, "trips.txt") if t["route_id"] in routes}

    log("reading stop_times (this takes a while for the full Warsaw feed) ...")
    patterns = defaultdict(Counter)    # (route, direction) -> Counter of stop_id tuples
    example = {}                       # ((route, direction), pattern) -> one trip's rows
    seen, jumbled = set(), 0
    for tid, buf in stream_stop_times(zf, trips, log):
        if tid in seen:
            jumbled += 1
            continue
        seen.add(tid)
        if not buf:
            continue
        buf.sort()
        key = trips[tid]
        pattern = tuple(s for _, s, _, _ in buf)
        patterns[key][pattern] += 1
        example.setdefault((key, pattern), buf)
    if jumbled:
        log(f"warning: {jumbled} trips had non-contiguous stop_times rows and were skipped")
    chosen = {key: c.most_common(1)[0][0] for key, c in patterns.items()}

    # stop name -> [(position, route_id)] for every pole used by a chosen pattern
    served = defaultdict(set)
    for (rid, _), pattern in chosen.items():
        for sid in pattern:
            st = stops.get(sid)
            if st:
                served[st["name"]].add((st["pos"], rid))

    def route_info(rid):
        r = routes[rid]
        return {"line": r.get("route_short_name") or r.get("route_long_name") or rid,
                "type": TYPE_NAMES.get(int(r.get("route_type") or 3), "bus")}

    by_route = defaultdict(dict)
    for (rid, direction), pattern in chosen.items():
        by_route[rid][direction] = pattern

    out = []
    for rid, dirs in by_route.items():
        info = route_info(rid)
        directions = []
        for direction in sorted(dirs):
            pattern = dirs[direction]
            seq = example[((rid, direction), pattern)]
            t0 = to_minutes(seq[0][2]) if seq[0][2] else None
            stop_list = []
            for _, sid, tstr, req in seq:
                st = stops.get(sid, {"name": sid, "pos": None, "wc": ""})
                name = st["name"]
                conns = {(route_info(r)["line"], route_info(r)["type"])
                         for pos, r in served[name] if r != rid and metres(pos, st["pos"]) <= CONN_RADIUS_M}
                conns = sorted(conns, key=lambda c: (TYPE_ORDER.get(c[1], 9), natural_key(c[0])))
                o = over.get(name, {})
                stop = {
                    "name": name,
                    "district": o.get("district", ""),
                    "poi": o.get("poi", ""),
                    "minutes": round(to_minutes(tstr) - t0) if tstr and t0 is not None else None,
                    "connections": [{"line": l, "type": t} for l, t in conns],
                    "stopId": sid,
                }
                if st["pos"]:
                    stop["lat"], stop["lon"] = round(st["pos"][0], 6), round(st["pos"][1], 6)
                if o.get("on_request") if o.get("on_request") is not None else req:
                    stop["onRequest"] = True
                wc = o.get("wheelchair") or ("none" if st["wc"] == "2" else "")
                if wc in ("none", "partial"):
                    stop["wheelchair"] = wc
                stop_list.append(stop)
            directions.append({"name": "→ " + stop_list[-1]["name"], "stops": stop_list})
        out.append({"line": info["line"], "type": info["type"], "directions": directions})

    out.sort(key=lambda l: natural_key(l["line"]))
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump({"lines": out}, fh, ensure_ascii=False, separators=(",", ":"))
    log(f"Wrote {len(out)} lines to {dst}")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    build(*sys.argv[1:])
