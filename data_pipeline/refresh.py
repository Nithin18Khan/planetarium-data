"""Refreshes the shared data files every phone reads from the CDN.

Runs in GitHub Actions (see .github/workflows/refresh.yml); one run serves
every user, so CelesTrak and NASA see a handful of requests a day no matter
how many people use the app.

    python refresh.py celestrak   # every 2 h  (CelesTrak updates every 2 h)
    python refresh.py apod        # daily
    python refresh.py ephemeris   # monthly: extends the JPL position tables

Output goes to ./out (the 'data' branch, served by jsDelivr). A source that
fails or returns something unexpected leaves the previous file in place, so
phones keep the last good copy.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

OUT = os.environ.get("OUT_DIR", "out")
UA = {"User-Agent": "planetarium-data-refresh (GitHub Actions; one shared download for all app users)"}


def get(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def write(rel, data: bytes):
    path = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    open(tmp, "wb").write(data)
    os.replace(tmp, path)
    print(f"  wrote {rel} ({len(data) // 1024} KB)")


def stamp(key):
    path = os.path.join(OUT, "manifest.json")
    manifest = json.load(open(path)) if os.path.exists(path) else {}
    manifest[key] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write("manifest.json", json.dumps(manifest, indent=1).encode())


def valid_tle(text):
    lines = [l for l in text.splitlines() if l.strip()]
    return sum(1 for l in lines if l.startswith("1 ")) > 0 and all(len(l.rstrip()) >= 69 for l in lines if l[:2] in ("1 ", "2 "))


def celestrak():
    base = "https://celestrak.org/NORAD/elements/gp.php"
    ok = 0
    for group in ("stations", "starlink", "weather", "visual", "active"):
        try:
            text = get(f"{base}?GROUP={group}&FORMAT=TLE").decode("ascii", "replace")
        except Exception as e:  # 403 = "not updated since your last download": keep the old file
            print(f"  {group}: kept previous file ({e})")
            continue
        if not valid_tle(text):
            print(f"  {group}: unexpected response, kept previous file")
            continue
        write(f"celestrak/{group}.tle", text.encode())
        ok += 1
        time.sleep(2)
    # The satellites the app tracks by name (ISS, Hubble, Jason-1…), some of
    # them retired and so missing from the 'active' group: one combined file.
    ids = json.load(open(os.path.join(os.path.dirname(__file__), "archive_norad.json")))["ids"]
    records = []
    for norad in ids:
        try:
            text = get(f"{base}?CATNR={norad}&FORMAT=TLE").decode("ascii", "replace").strip()
        except Exception as e:
            print(f"  {norad}: skipped ({e})")
            continue
        if valid_tle(text):
            records.append(text)
        time.sleep(1)
    if len(records) >= len(ids) // 2:
        write("celestrak/archive.tle", ("\n".join(records) + "\n").encode())
        ok += 1
    if ok:
        stamp("celestrak")


def apod():
    key = os.environ.get("NASA_API_KEY") or "DEMO_KEY"  # one call a day fits DEMO_KEY
    try:
        body = json.loads(get(f"https://api.nasa.gov/planetary/apod?api_key={urllib.parse.quote(key)}&thumbs=true"))
    except Exception as e:
        print(f"  apod: kept previous file ({e})")
        return
    if not isinstance(body, dict) or "url" not in body or "date" not in body:
        print("  apod: unexpected response, kept previous file")
        return
    write("apod/today.json", json.dumps(body).encode())
    stamp("apod")


def ephemeris():
    # Same builder the app release uses; it writes into ../assets/ephemeris.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tool"))
    import build_ephemeris  # noqa: E402
    build_ephemeris.OUT_DIR = os.path.join(OUT, "ephemeris")
    build_ephemeris.main()
    stamp("ephemeris")


if __name__ == "__main__":
    jobs = {"celestrak": celestrak, "apod": apod, "ephemeris": ephemeris}
    for name in sys.argv[1:] or ["celestrak", "apod"]:
        print(name)
        jobs[name]()
