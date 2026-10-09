#!/usr/bin/env python3
"""
Light Govee panels in an aurora pattern when it's dark, clear, and aurora is likely.

Usage:
  aurora_watch.py               run the watcher
  aurora_watch.py --check       print current conditions, no lights
  aurora_watch.py --demo SECS   show the pattern now
"""
import json
import logging
import logging.handlers
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from govee_razer import Claim, Panel

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "check_interval_min": 15,
    "sun_max_elev": -10.0,   # deg
    "cloud_max": 35,         # %
    "aurora_min": 15,        # % OVATION probability
    "moon_penalty": 15,      # extra % under a full, high moon
    "moon_full_alt": 30,     # deg; moon altitude where the penalty peaks
    "box_north": 2,          # deg of latitude north of home to search
    "box_lon": 4,            # +/- deg of longitude to search
    "zones": 10,
    "brightness": 40,        # 1-100
    "claim_file": None,      # optional, see README
}

OVATION_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
CLOUD_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=cloud_cover&timezone=UTC"
)
FRAME_INTERVAL = 0.2  # 5 fps


def load_config():
    path = os.path.join(HERE, "config.json")
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Missing {path}; copy config.example.json and set lat/lon.")
    cfg = {**DEFAULTS, **user}
    if "lat" not in cfg or "lon" not in cfg:
        sys.exit("config.json needs lat and lon")
    return cfg


CFG = load_config()

log = logging.getLogger("aurora")
log.setLevel(logging.INFO)
_handler = logging.handlers.RotatingFileHandler(
    os.path.join(HERE, "aurora_watch.log"), maxBytes=1_000_000, backupCount=3,
)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s]: %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(_handler)


def get_with_retries(url, retries=3):
    session = requests.Session()
    retry = Retry(total=retries, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    try:
        response = session.get(url, timeout=10)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException:
        return None


# ---------------------------------------------------------------- conditions

def sun_elevation(lat, lon, when=None):
    """Solar elevation in degrees (NOAA approximation)."""
    when = when or datetime.now(timezone.utc)
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqtime + 4 * lon
    ha = math.radians(tst / 4 - 180)
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def _rev(x):
    return x % 360.0


def moon_position(lat, lon, when=None):
    """(altitude deg, illuminated fraction). Schlyter's low-precision lunar theory."""
    when = when or datetime.now(timezone.utc)
    d = (when - datetime(1999, 12, 31, tzinfo=timezone.utc)).total_seconds() / 86400
    rad, deg = math.radians, math.degrees
    sin, cos = lambda x: math.sin(rad(x)), lambda x: math.cos(rad(x))
    # sun
    ws = 282.9404 + 4.70935e-5 * d
    es = 0.016709 - 1.151e-9 * d
    Ms = _rev(356.0470 + 0.9856002585 * d)
    Es = Ms + deg(es * sin(Ms) * (1 + es * cos(Ms)))
    sun_lon = _rev(deg(math.atan2(math.sqrt(1 - es * es) * sin(Es), cos(Es) - es)) + ws)
    Ls = _rev(Ms + ws)
    # moon orbital elements
    N = _rev(125.1228 - 0.0529538083 * d)
    i = 5.1454
    w = _rev(318.0634 + 0.1643573223 * d)
    a, e = 60.2666, 0.054900
    M = _rev(115.3654 + 13.0649929509 * d)
    E = M + deg(e * sin(M) * (1 + e * cos(M)))
    for _ in range(5):
        E = E - (E - deg(e * sin(E)) - M) / (1 - e * cos(E))
    x, y = a * (cos(E) - e), a * math.sqrt(1 - e * e) * sin(E)
    r, v = math.hypot(x, y), deg(math.atan2(y, x))
    xe = r * (cos(N) * cos(v + w) - sin(N) * sin(v + w) * cos(i))
    ye = r * (sin(N) * cos(v + w) + cos(N) * sin(v + w) * cos(i))
    ze = r * sin(v + w) * sin(i)
    mlon = deg(math.atan2(ye, xe))
    mlat = deg(math.atan2(ze, math.hypot(xe, ye)))
    # main perturbations
    Lm = _rev(M + w + N)
    D, F = Lm - Ls, Lm - N
    mlon += (-1.274 * sin(M - 2 * D) + 0.658 * sin(2 * D) - 0.186 * sin(Ms)
             - 0.059 * sin(2 * M - 2 * D) - 0.057 * sin(M - 2 * D + Ms) + 0.053 * sin(M + 2 * D)
             + 0.046 * sin(2 * D - Ms) + 0.041 * sin(M - Ms) - 0.035 * sin(D)
             - 0.031 * sin(M + Ms) - 0.015 * sin(2 * F - 2 * D) + 0.011 * sin(M - 4 * D))
    mlat += (-0.173 * sin(F - 2 * D) - 0.055 * sin(M - F - 2 * D) - 0.046 * sin(M + F - 2 * D)
             + 0.033 * sin(F + 2 * D) + 0.017 * sin(2 * M + F))
    # ecliptic -> equatorial
    ecl = 23.4393 - 3.563e-7 * d
    xq = cos(mlat) * cos(mlon)
    yq = cos(mlat) * sin(mlon) * cos(ecl) - sin(mlat) * sin(ecl)
    zq = cos(mlat) * sin(mlon) * sin(ecl) + sin(mlat) * cos(ecl)
    ra, dec = deg(math.atan2(yq, xq)), deg(math.atan2(zq, math.hypot(xq, yq)))
    # altitude, corrected for parallax
    ut = when.hour + when.minute / 60 + when.second / 3600
    ha = _rev(Ls + 180 + ut * 15 + lon - ra)
    alt = deg(math.asin(sin(lat) * sin(dec) + cos(lat) * cos(dec) * cos(ha)))
    alt -= deg(math.asin(1 / r)) * cos(alt)
    # illuminated fraction from elongation
    illum = (1 - cos(mlat) * cos(mlon - sun_lon)) / 2
    return alt, illum


def moon_penalty(alt, illum):
    """Extra aurora % required for moonlight, scaled by phase and altitude."""
    if alt <= 0:
        return 0.0
    return CFG["moon_penalty"] * illum * min(1.0, alt / CFG["moon_full_alt"])


PROFILE_NORTH = 5  # deg north of home to include in the log


def aurora_probability():
    """(max probability in the search box, {latitude: max probability})."""
    r = get_with_retries(OVATION_URL)
    if not r:
        log.error("Failed to retrieve OVATION aurora forecast")
        return None, None
    try:
        coords = r.json()["coordinates"]  # [lon 0..359, lat, prob]
    except (ValueError, KeyError) as e:
        log.error(f"Bad OVATION response: {e}")
        return None, None
    home_lon = CFG["lon"] % 360
    lo = math.floor(CFG["lat"]) - 1
    profile = {lat: 0 for lat in range(lo, math.floor(CFG["lat"]) + PROFILE_NORTH + 1)}
    for lon, lat, prob in coords:
        if lat in profile and abs((lon - home_lon + 180) % 360 - 180) <= CFG["box_lon"]:
            profile[lat] = max(profile[lat], prob)
    top = CFG["lat"] + CFG["box_north"]
    best = max(p for lat, p in profile.items() if lat <= top)
    return best, profile


def latest_kp():
    """Latest planetary Kp (logged only)."""
    r = get_with_retries(KP_URL)
    try:
        return r.json()[-1]["Kp"] if r else None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def cloud_cover():
    r = get_with_retries(CLOUD_URL.format(lat=CFG["lat"], lon=CFG["lon"]))
    if not r:
        log.error("Failed to retrieve Open-Meteo cloud cover")
        return None
    try:
        return r.json()["current"]["cloud_cover"]
    except (ValueError, KeyError) as e:
        log.error(f"Bad Open-Meteo response: {e}")
        return None


def check():
    """Return (go, summary)."""
    sun = sun_elevation(CFG["lat"], CFG["lon"])
    if sun > CFG["sun_max_elev"]:
        return False, f"sun {sun:.1f} deg"
    aurora, profile = aurora_probability()
    clouds = cloud_cover()
    kp = latest_kp()
    moon_alt, moon_illum = moon_position(CFG["lat"], CFG["lon"])
    need = CFG["aurora_min"] + moon_penalty(moon_alt, moon_illum)
    by_lat = " ".join(f"{lat}:{p}" for lat, p in profile.items()) if profile else "n/a"
    summary = (f"sun {sun:.1f} deg, clouds {clouds}%, aurora {aurora}% (need {need:.0f}), "
               f"moon {round(moon_alt)} deg {round(moon_illum * 100)}% lit, Kp {kp} [by lat {by_lat}]")
    if aurora is None or clouds is None:
        return False, summary + " (data missing)"
    return aurora >= need and clouds <= CFG["cloud_max"], summary


# ---------------------------------------------------------------- the look

GREEN = (20, 255, 70)
TEAL = (0, 190, 150)
VIOLET = (150, 0, 255)
PINK = (255, 30, 140)


def _mix(a, b, t):
    return tuple(a[k] + (b[k] - a[k]) * t for k in range(3))


def aurora_frame(t, n):
    """Drifting green/teal curtains with purple on the bright crests."""
    surge = 0.7 + 0.15 * (1 + math.sin(t * 0.045))
    zones = []
    for i in range(n):
        x = i / n
        curtain = 0.5 + 0.5 * math.sin(2 * math.pi * x * 1.3 - t * 0.35)
        ripple = 0.5 + 0.5 * math.sin(2 * math.pi * x * 3.1 + t * 0.9 + math.sin(t * 0.13) * 2)
        level = curtain * (0.65 + 0.35 * ripple)
        bright = 0.05 + 0.95 * min(1.0, level * 1.3) ** 1.6
        color = _mix(GREEN, TEAL, 0.5 + 0.5 * math.sin(t * 0.21 + i * 0.8))
        crest = min(1.0, (max(0.0, level - 0.45) / 0.55) * surge * 1.3)
        color = _mix(color, VIOLET, crest)
        color = _mix(color, PINK, max(0.0, crest - 0.6) * 0.8)
        zones.append(tuple(max(0, min(255, round(c * bright))) for c in color))
    return zones


def stream_aurora(seconds, claim_ts):
    """Stream for `seconds`. Returns False if another script took the panel."""
    claim = Claim(CFG["claim_file"])
    claim.write(claim_ts)
    panel = Panel()
    try:
        panel.start(CFG["brightness"])
        start = next_time = time.time()
        while time.time() - start < seconds:
            newer = claim.newer_than(claim_ts)
            if newer:
                log.info(f"preempted by a newer script (claim={newer:.3f})")
                return False
            panel.frame(aurora_frame(time.time() - start + claim_ts % 1000, CFG["zones"]))
            next_time += FRAME_INTERVAL
            time.sleep(max(0, next_time - time.time()))
        return True
    finally:
        panel.close()


# ---------------------------------------------------------------- main

def watch():
    interval = CFG["check_interval_min"] * 60
    log.info(f"started; every {CFG['check_interval_min']} min, sun <= {CFG['sun_max_elev']}, "
             f"clouds <= {CFG['cloud_max']}%, aurora >= {CFG['aurora_min']}%")
    showing = False
    while True:
        cycle_start = time.time()
        try:
            go, summary = check()
        except Exception as e:
            go, summary = False, f"check failed: {e!r}"
        if go and not showing:
            log.info(f"ALERT ON. {summary}")
        elif not go and showing:
            log.info(f"alert off. {summary}")
        else:
            log.info(("on. " if go else "off. ") + summary)
        showing = go

        if go:
            stream_aurora(interval, time.time())
        time.sleep(max(0, interval - (time.time() - cycle_start)))


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        go, summary = check()
        print(("GO: " if go else "NO-GO: ") + summary)
    elif len(sys.argv) == 3 and sys.argv[1] == "--demo":
        stream_aurora(float(sys.argv[2]), time.time())
    elif len(sys.argv) == 1:
        watch()
    else:
        print(__doc__)
        sys.exit(2)
