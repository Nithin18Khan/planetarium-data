"""Builds the bundled JPL Horizons ephemeris, so phones never query JPL.

    python tool/build_ephemeris.py

Writes assets/ephemeris/ephemeris.json and assets/ephemeris/physical/*.txt.
Run it before each release (a GitHub Action does it monthly; see
docs/SCALING.md). The app answers every position request inside the covered
window from these tables and only calls JPL live outside it.

For each target:
  * heliocentric ecliptic positions (AU) from 370 days ago to 735 days ahead,
    at the coarsest step (1 d or 6 h) whose 4-point Lagrange interpolation
    stays within MAX_HELIO_ERR_AU of JPL's own finer samples;
  * for objects near Earth (< 0.05 AU: Webb, SOHO, TESS, Chandra…) also
    geocentric positions (km) at the coarsest step (1 d … 1 h) that keeps the
    error under 1 % of the distance;
  * for the outer probes (> 10 AU) a 20-year trail at 90-day steps.
"""
import datetime as dt
import json
import os
import re
import time
import urllib.parse
import urllib.request

import numpy as np

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT_DIR = os.path.join(ROOT, "assets", "ephemeris")
API = "https://ssd.jpl.nasa.gov/api/horizons.api"
PAST_DAYS, FUTURE_DAYS = 370, 735
GEO_PAST_DAYS = 185
MAX_HELIO_ERR_AU = 2e-6  # ≈ 300 km
PLANETS = {"199": "mercury", "299": "venus", "399": "earth", "499": "mars", "599": "jupiter", "699": "saturn", "799": "uranus", "899": "neptune", "999": "pluto"}
PHYSICAL = ["10", "199", "299", "399", "301", "499", "599", "699", "799", "899", "999"]


def dart_targets():
    """Every Horizons command the app uses: read from the Dart sources, or, in
    the public data repo (no app code), from the previously published table."""
    cmds = []
    sources = [os.path.join(ROOT, rel) for rel in ("lib/constants/artifact_locations.dart", "lib/constants/small_bodies.dart")]
    if not all(os.path.exists(p) for p in sources):
        previous = os.path.join(OUT_DIR, "ephemeris.json")
        return [tuple(t) for t in json.load(open(previous))["targets"] if t[0] not in PLANETS]
    for path in sources:
        text = open(path, encoding="utf-8").read()
        for m in re.finditer(r"HorizonsSpacecraft\('([^']+)',\s*'([^']+)'\)", text):
            if m.group(1) not in [c for c, _ in cmds]:
                cmds.append((m.group(1), m.group(2)))
    # Live craft only: skip entries that are commented out or retired.
    return cmds


def horizons(params, attempt=0):
    q = {"format": "json", **{k: f"'{v}'" for k, v in params.items()}}
    url = API + "?" + urllib.parse.urlencode(q)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "planetarium-ephemeris-builder"})
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read())
    except Exception as e:  # 503 when JPL is busy: back off and retry
        if attempt < 4:
            time.sleep(5 * (attempt + 1))
            return horizons(params, attempt + 1)
        raise RuntimeError(f"Horizons failed for {params.get('COMMAND')}: {e}")
    time.sleep(1.0)  # one request at a time, as JPL asks
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _jpl_date(text):
    y, mon, d = text.split("-")
    return dt.datetime(int(y), MONTHS[mon.upper()], int(d))


def vectors(command, center, start, stop, step, km):
    """Like _vectors, but clamps the window to the span JPL actually covers
    (missions end: Juno's ephemeris stops in October 2028)."""
    for _ in range(3):
        try:
            return _vectors(command, center, start, stop, step, km)
        except RuntimeError as e:
            after = re.search(r"after A\.D\. (\d{4}-[A-Za-z]{3}-\d{2})", str(e))
            prior = re.search(r"prior to A\.D\. (\d{4}-[A-Za-z]{3}-\d{2})", str(e))
            if after:
                stop = _jpl_date(after.group(1)) - dt.timedelta(days=1)
            elif prior:
                start = _jpl_date(prior.group(1)) + dt.timedelta(days=1)
            else:
                raise
            print(f"    {command}: JPL covers only to {stop:%Y-%m-%d} / from {start:%Y-%m-%d}; window clamped")
    return _vectors(command, center, start, stop, step, km)


def _vectors(command, center, start, stop, step, km):
    text = horizons({
        "COMMAND": command, "OBJ_DATA": "NO", "MAKE_EPHEM": "YES", "EPHEM_TYPE": "VECTORS",
        "CENTER": center, "START_TIME": start.strftime("%Y-%m-%d %H:%M"), "STOP_TIME": stop.strftime("%Y-%m-%d %H:%M"),
        "STEP_SIZE": step, "OUT_UNITS": "KM-S" if km else "AU-D", "REF_PLANE": "ECLIPTIC", "VEC_TABLE": "1", "CSV_FORMAT": "YES",
    })
    if "$$SOE" not in text:
        raise RuntimeError(f"no ephemeris for {command}: {text[-300:]}")
    rows = text.split("$$SOE")[1].split("$$EOE")[0].strip().splitlines()
    jd, xyz = [], []
    for row in rows:
        cols = [c.strip() for c in row.split(",")]
        jd.append(float(cols[0]))
        xyz.append([float(cols[2]), float(cols[3]), float(cols[4])])
    return np.array(jd), np.array(xyz)


def lagrange_error(jd, xyz, factor):
    """Max error when keeping every `factor`-th sample and interpolating the rest."""
    kept_jd, kept = jd[::factor], xyz[::factor]
    if len(kept_jd) < 5:
        return float("inf")
    worst = 0.0
    h = kept_jd[1] - kept_jd[0]
    for i, t in enumerate(jd):
        if i % factor == 0:
            continue
        k = int((t - kept_jd[0]) / h)
        k = min(max(k - 1, 0), len(kept_jd) - 4)
        ts, ps = kept_jd[k:k + 4], kept[k:k + 4]
        p = np.zeros(3)
        for a in range(4):
            w = 1.0
            for b in range(4):
                if a != b:
                    w *= (t - ts[b]) / (ts[a] - ts[b])
            p += w * ps[a]
        worst = max(worst, float(np.linalg.norm(p - xyz[i])))
    return worst


def nearest(jd, xyz, target_jd):
    return xyz[int(np.argmin(np.abs(jd - target_jd)))]


def series(command, center, jd, xyz, step_days, decimals):
    return {
        "cmd": command, "center": center, "jd0": round(float(jd[0]), 6), "step": step_days, "n": int(len(jd)),
        "xyz": [round(float(v), decimals) for v in xyz.reshape(-1)],
    }


def moon_series(start, stop):
    """The Moon around Earth (km). The sky view uses it instead of the
    low-precision series (~0.3°); the step is the coarsest within 5 km."""
    jd, xyz = vectors("301", "500@399", start, stop, "1 h", km=True)
    for factor, days in ((6, 0.25), (3, 0.125), (2, 1 / 12)):
        err = lagrange_error(jd, xyz, factor)
        if err < 5:
            print(f"{'Moon':32} geo   step {days * 24:g} h  (error {err:.2f} km)")
            return series("301", "earth", jd[::factor], xyz[::factor], days, 1)
    print(f"{'Moon':32} geo   step 1 h")
    return series("301", "earth", jd, xyz, 1 / 24, 1)


def add_moon_only():
    """python tool/build_ephemeris.py moon: add or refresh just the Moon."""
    path = os.path.join(OUT_DIR, "ephemeris.json")
    doc = json.load(open(path))
    today = dt.datetime.strptime(doc["generated"], "%Y-%m-%d")
    doc["series"] = [s for s in doc["series"] if not (s["cmd"] == "301" and s["center"] == "earth")]
    doc["series"].append(moon_series(today - dt.timedelta(days=PAST_DAYS), today + dt.timedelta(days=FUTURE_DAYS)))
    json.dump(doc, open(path, "w"), separators=(",", ":"))
    print(f"wrote {path}: {os.path.getsize(path) // 1024} KB")


def main():
    os.makedirs(os.path.join(OUT_DIR, "physical"), exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    start, stop = today - dt.timedelta(days=PAST_DAYS), today + dt.timedelta(days=FUTURE_DAYS)
    targets = [(c, n) for c, n in PLANETS.items()] + dart_targets()
    out, earth = [], None
    jd_today = (today - dt.datetime(2000, 1, 1, 12)).total_seconds() / 86400 + 2451545.0

    for command, name in targets:
        jd6, xyz6 = vectors(command, "500@10", start, stop, "6 h", km=False)
        if command == "399":
            earth = (jd6, xyz6)
        err = lagrange_error(jd6, xyz6, 4)
        if err < MAX_HELIO_ERR_AU:
            out.append(series(command, "sun", jd6[::4], xyz6[::4], 1.0, 8))
            step = "1 d"
        else:
            out.append(series(command, "sun", jd6, xyz6, 0.25, 8))
            step = "6 h"
        here = nearest(jd6, xyz6, jd_today)
        r_now = float(np.linalg.norm(here))
        print(f"{name:32} helio {step}  (1 d error {err * 1.496e8:,.0f} km)  r={r_now:.2f} AU")

        if command in PLANETS:
            continue
        if earth is not None and float(np.linalg.norm(here - nearest(earth[0], earth[1], jd_today))) < 0.05:
            g_start = today - dt.timedelta(days=GEO_PAST_DAYS)
            jdg, xyzg = vectors(command, "500@399", g_start, stop, "1 h", km=True)
            dist = float(np.median(np.linalg.norm(xyzg, axis=1)))
            chosen = (1, 1 / 24)
            for factor, days in ((24, 1.0), (6, 0.25), (3, 0.125)):
                if lagrange_error(jdg, xyzg, factor) < 0.01 * dist:
                    chosen = (factor, days)
                    break
            out.append(series(command, "earth", jdg[::chosen[0]], xyzg[::chosen[0]], chosen[1], 1))
            print(f"{'':32} geo   step {chosen[1] * 24:g} h  (median distance {dist:,.0f} km)")
        if r_now > 10:
            jdf, xyzf = vectors(command, "500@10", today - dt.timedelta(days=365 * 20), today, "90 d", km=False)
            out.append(series(command, "sun-trail", jdf, xyzf, 90.0, 6))
            print(f"{'':32} 20-year trail at 90 d")

    out.append(moon_series(start, stop))
    doc = {
        "generated": today.strftime("%Y-%m-%d"),
        "source": "NASA JPL Horizons (ssd.jpl.nasa.gov), ecliptic J2000; 'sun' = heliocentric AU, 'earth' = geocentric km.",
        "targets": [list(t) for t in targets],
        "series": out,
    }
    path = os.path.join(OUT_DIR, "ephemeris.json")
    json.dump(doc, open(path, "w"), separators=(",", ":"))
    print(f"wrote {path}: {len(out)} series, {os.path.getsize(path) // 1024} KB")

    for code in PHYSICAL:
        text = horizons({"COMMAND": code, "OBJ_DATA": "YES", "MAKE_EPHEM": "NO"})
        open(os.path.join(OUT_DIR, "physical", f"{code}.txt"), "w", encoding="utf-8").write(text)
    print(f"wrote {len(PHYSICAL)} physical-data files")


if __name__ == "__main__":
    import sys
    add_moon_only() if sys.argv[1:] == ["moon"] else main()
