# sip19192 – strip map

Type a line number and the page shows that line's strip map, using Warsaw's (ZTM) timetable data.

## Run it

```
python3 server.py --contact you@example.com
```

Open http://localhost:8000. Python 3.8+, no packages to install.

The first start downloads the ZTM Warsaw GTFS feed (`https://mkuran.pl/gtfs/warsaw.zip`, a daily
conversion of ZTM's official data) and converts it once into `data/warsaw_lines.json`. This takes a few
minutes. After that it starts instantly and refreshes itself every 24 h.

| What the strip shows | Where it comes from |
|---|---|
| stops, order, minutes from the first stop | ZTM timetable data (GTFS) |
| "na żądanie" stops (empty circle) | GTFS `pickup_type` / `drop_off_type` = 3 |
| wheelchair badge | GTFS `wheelchair_boarding` (2 → red badge; 1 → no badge) |
| connections | other routes serving a stop with the same name within 400 m |
| streets (under the district bar) and districts | not in ZTM data: looked up from each stop's coordinates with OpenStreetMap Nominatim, max 1 request/second, cached forever in `data/geo_cache.sqlite`. They fill in gradually (the page shows progress); the first time you open a long line it takes about a minute, then it is instant. |

Options: `--gtfs PATH_OR_URL`, `--port`, `--host 0.0.0.0`, `--no-geocode`, `--geocoder URL` (any
Nominatim-compatible server, e.g. your own), `--overrides file.csv` (`stop_name,district,poi,on_request,wheelchair`
– values there win over everything), `--rebuild`.

Without the server (plain static hosting) the page falls back to the demo data in `data/lines.json`.
Demo data: `python3 tools/make_demo_gtfs.py demo.zip && python3 server.py --gtfs demo.zip --no-geocode`.

## Notes
* Attribution: ZTM Warszawa (data), Mikołaj Kuranowski (GTFS conversion), © OpenStreetMap contributors (streets and districts).
* Nominatim's public server is for light use. Keep `--contact` set and don't expose it to many users at once.
* The City's old `api.um.warszawa.pl` was switched off on 21 Oct 2026. The new `dane.um.warszawa.pl` needs a personal token
  and, as far as I could verify, has no "all stops of a line" endpoint, which is why the GTFS feed is used.
