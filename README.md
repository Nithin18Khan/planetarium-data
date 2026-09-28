# Planetarium shared data

Public data files for the Planetarium app, refreshed by GitHub Actions and
served to every phone through the jsDelivr CDN. One download here replaces
millions of requests from phones to the original services.

| File (branch `data`) | Source | Refreshed |
|---|---|---|
| `celestrak/*.tle` (stations, starlink, weather, visual, active, archive) | [CelesTrak](https://celestrak.org) general-perturbations element sets | every 2 hours (CelesTrak's own update rate) |
| `apod/today.json` | [NASA APOD](https://apod.nasa.gov) via api.nasa.gov | daily |
| `ephemeris/ephemeris.json`, `ephemeris/physical/*.txt` | [NASA JPL Horizons](https://ssd.jpl.nasa.gov/horizons/) | monthly |
| `manifest.json` | time of each refresh | every run |

CDN base: `https://cdn.jsdelivr.net/gh/Nithin18Khan/planetarium-data@data/`

Data credit: CelesTrak (T.S. Kelso); NASA/JPL-Caltech; NASA APOD. APOD images
may carry their own copyright (see the `copyright` field). No app code lives here.
