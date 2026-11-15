#!/usr/bin/env python3
"""Convert a GTFS zip into data/lines.json for the strip-map site.

Usage:  python3 tools/gtfs_to_json.py path/to/gtfs.zip data/lines.json [districts.csv]

districts.csv (optional) has the header "stop_name,district[,poi]"; it fills the
district bar under the line (and optional landmarks below it). GTFS itself has
no district data.

Standard library only. For every route and direction it picks the most common
stop pattern (so short-turn variants don't win), takes stop order from
stop_sequence, minutes from the first stop, and builds the "connections" for
every stop: all OTHER routes that serve a stop with the same name.
"""
import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict

TYPE_NAMES = {0: "tram", 1: "metro", 2: "train", 3: "bus", 11: "trolleybus", 800: "trolleybus",
              700: "bus", 900: "tram", 400: "metro", 100: "train", 109: "train"}


def rows(zf, name):
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def to_minutes(t):
    h, m, s = (int(x) for x in t.strip().split(":"))
    return h * 60 + m + s / 60  # GTFS allows hours >= 24


def natural_key(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def main(src, dst, districts_csv=None):
    zf = zipfile.ZipFile(src)

    # optional: CSV with header "stop_name,district" -> shown in the red district bar
    # optional extra column "poi": a landmark printed diagonally under the district bar
    districts, pois = {}, {}
    if districts_csv:
        with open(districts_csv, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                districts[r["stop_name"].strip()] = (r.get("district") or "").strip()
                pois[r["stop_name"].strip()] = (r.get("poi") or "").strip()

    routes = {r["route_id"]: r for r in rows(zf, "routes.txt")}
    stops = {s["stop_id"]: s["stop_name"].strip() for s in rows(zf, "stops.txt")}
    trips = {t["trip_id"]: (t["route_id"], t.get("direction_id") or "0") for t in rows(zf, "trips.txt")}

    # trip_id -> [(sequence, stop_id, minutes)]
    per_trip = defaultdict(list)
    for st in rows(zf, "stop_times.txt"):
        if st["trip_id"] in trips:
            t = st["arrival_time"] or st["departure_time"]
            per_trip[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"], to_minutes(t) if t else None))

    # (route, direction) -> Counter of stop_id patterns, plus one example trip per pattern
    patterns = defaultdict(Counter)
    example = {}
    for trip_id, seq in per_trip.items():
        seq.sort()
        key = trips[trip_id]
        pattern = tuple(s for _, s, _ in seq)
        patterns[key][pattern] += 1
        example.setdefault((key, pattern), seq)

    chosen = {key: c.most_common(1)[0][0] for key, c in patterns.items()}

    # stop name -> set of route_ids serving it (grouping poles by name)
    served = defaultdict(set)
    for (route_id, _), pattern in chosen.items():
        for sid in pattern:
            served[stops.get(sid, sid)].add(route_id)

    def route_info(rid):
        r = routes[rid]
        return {"line": r["route_short_name"] or r["route_long_name"],
                "type": TYPE_NAMES.get(int(r["route_type"] or 3), "bus")}

    out = []
    by_route = defaultdict(dict)
    for (route_id, direction), pattern in chosen.items():
        by_route[route_id][direction] = pattern

    for route_id, dirs in by_route.items():
        info = route_info(route_id)
        directions = []
        for direction in sorted(dirs):
            pattern = dirs[direction]
            seq = example[((route_id, direction), pattern)]
            t0 = seq[0][2]
            stop_list = []
            for _, sid, minutes in seq:
                name = stops.get(sid, sid)
                conns = {(route_info(r)["line"], route_info(r)["type"])
                         for r in served[name] if r != route_id}
                order = {"metro": 0, "train": 1, "tram": 2, "trolleybus": 3, "bus": 4}
                conns = sorted(conns, key=lambda c: (order.get(c[1], 9), natural_key(c[0])))
                stop_list.append({
                    "name": name,
                    "district": districts.get(name, ""),
                    "poi": pois.get(name, ""),
                    "minutes": round(minutes - t0) if minutes is not None and t0 is not None else None,
                    "connections": [{"line": l, "type": t} for l, t in conns],
                })
            directions.append({"name": "→ " + stop_list[-1]["name"], "stops": stop_list})
        out.append({"line": info["line"], "type": info["type"], "directions": directions})

    out.sort(key=lambda l: natural_key(l["line"]))
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump({"lines": out}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(out)} lines to {dst}")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    main(*sys.argv[1:])
