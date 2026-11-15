#!/usr/bin/env python3
"""Builds a tiny demo GTFS zip so the site works before you import real data.
Line 256 uses the stops from the screenshot; the other lines are invented
demo lines that just share some stop names, so connections show up."""
import csv
import io
import sys
import zipfile

LINES = {
    # line: (route_type, [(stop name, minutes from first stop)])
    "256": (3, [("Metro Kondratowicza", 0), ("Knapika", 2), ("Małborska", 4), ("CH Targówek", 5),
                ("CH Targówek", 7), ("Jesiennych Liści", 8), ("Podwojna", 9), ("Sieczna", 10),
                ("Okrągła", 11), ("Grodzisk-Kościół", 12), ("Grodzisk", 13), ("Internetowa", 13),
                ("Os. Lewandów II", 14), ("Oknicka", 15), ("Chudoby", 17), ("Grodzisk", 18),
                ("Zaułek-Szkoła", 19)]),
    "M2": (1, [("Rondo Daszyńskiego", 0), ("Centrum Nauki Kopernik", 6), ("Dworzec Wileński", 10),
               ("Targówek Mieszkaniowy", 14), ("Metro Kondratowicza", 17), ("Bródno", 19)]),
    "17": (0, [("Metro Kondratowicza", 0), ("Knapika", 3), ("Małborska", 5), ("Jesiennych Liści", 9),
               ("Sieczna", 12), ("Dworzec Wileński", 25)]),
    "523": (3, [("Dworzec Wileński", 0), ("CH Targówek", 8), ("Okrągła", 12), ("Internetowa", 15),
                ("Oknicka", 17), ("Chudoby", 19)]),
}


def main(dst):
    stop_ids = {}
    for _, (_, seq) in LINES.items():
        for name, _ in seq:
            stop_ids.setdefault(name, f"S{len(stop_ids) + 1:03d}")
    # the screenshot lists "CH Targówek" and "Grodzisk" twice; give each pole its own id
    files = {k: io.StringIO() for k in ("routes", "stops", "trips", "stop_times")}
    w = {k: csv.writer(v) for k, v in files.items()}
    w["routes"].writerow(["route_id", "route_short_name", "route_long_name", "route_type"])
    w["stops"].writerow(["stop_id", "stop_name", "stop_lat", "stop_lon", "wheelchair_boarding"])
    w["trips"].writerow(["route_id", "trip_id", "direction_id"])
    w["stop_times"].writerow(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"])
    for i, (name, sid) in enumerate(stop_ids.items()):
        # invented coordinates along a line north-east of the centre; every 4th stop is not wheelchair accessible
        w["stops"].writerow([sid, name, f"{52.28 + i * 0.002:.6f}", f"{21.04 + i * 0.003:.6f}", 2 if i % 4 == 1 else 1])
    for line, (rtype, seq) in LINES.items():
        w["routes"].writerow([line, line, "", rtype])
        total = seq[-1][1]
        for direction in (0, 1):
            trip = f"{line}-{direction}"
            w["trips"].writerow([line, trip, direction])
            ordered = seq if direction == 0 else [(n, total - m) for n, m in reversed(seq)]
            for i, (name, m) in enumerate(ordered, 1):
                t = f"{8 + m // 60:02d}:{m % 60:02d}:00"
                req = 3 if name in ("Małborska", "Chudoby") else 0     # "na żądanie" demo stops
                w["stop_times"].writerow([trip, t, t, stop_ids[name], i, req, req])
    with zipfile.ZipFile(dst, "w") as zf:
        for k, v in files.items():
            zf.writestr(f"{k}.txt", v.getvalue())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_gtfs.zip")
