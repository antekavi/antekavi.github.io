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
– values there win over everything), `--rebuild`, `--api-token TOKEN`, `--probe-api`.

## City of Warsaw API token

The token from https://dane.um.warszawa.pl/pl/key-api lives in `api_token.txt` (git-ignored, never served by the
server; or set `$WARSAW_API_TOKEN`, or pass `--api-token`). It is sent only by `server.py`, only to
`dane.um.warszawa.pl`, as the raw `Authorization` header (no `Bearer`), never to the browser. **Do not commit it,
and keep the repository's `.gitignore`.** If it was ever pushed or shared, create a new one on the portal.

Line lookups do not use it: the City's documented actions (`get_ztm_odjazdy_linii_z_przystanku`,
`get_ztm_lista_linii_na_przystanku`, `get_ztm_lokalizacja_pojazdow`) cover departures, lines at a stop and live
vehicle positions, not "all stops of a line", streets or districts. To see what your token really returns:

```
python3 server.py --probe-api
```

This calls those actions once with a few request bodies and writes HTTP status + the start of every answer to
`data/api_probe.txt` (the token is not written there). Send that file along and the useful parts can be wired in.

Without the server (plain static hosting) the page falls back to the demo data in `data/lines.json`.
Demo data: `python3 tools/make_demo_gtfs.py demo.zip && python3 server.py --gtfs demo.zip --no-geocode`.

## Notes
* Attribution: ZTM Warszawa (data), Mikołaj Kuranowski (GTFS conversion), © OpenStreetMap contributors (streets and districts).
* Nominatim's public server is for light use. Keep `--contact` set and don't expose it to many users at once.
* The City's old `api.um.warszawa.pl` was switched off on 21 Oct 2026. The new `dane.um.warszawa.pl` needs a personal token
  and, as far as I could verify, has no "all stops of a line" endpoint, which is why the GTFS feed is used.
* Display rules: each street name is centred between two slanted lines, spanning the stops on that street; street
  names containing "Pętla" are not drawn (loops are not shown on the real displays); a "next stop" text that is too
  long for its space is not shrunk but scrolls to its end and back.
* The server never serves dot-files (`.git`, `.gitignore`), `api_token.txt`, `server.py`, `tools/` or the caches.
