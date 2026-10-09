# aurora-alert

Turns Govee lights into an aurora pattern when it's worth going outside.

## How it works

Every 15 minutes it checks:

| Check | Source | Default |
|---|---|---|
| Dark | Sun elevation (computed) | -10° or lower |
| Clear | [Open-Meteo](https://open-meteo.com/) cloud cover | 35% or less |
| Aurora | [NOAA OVATION](https://www.swpc.noaa.gov/products/aurora-30-minute-forecast) probability, up to 2° north | 15% or more |

A bright moon raises the aurora cutoff by up to `moon_penalty` (default 15):

```
need = aurora_min + moon_penalty × fraction lit × min(1, moon altitude / moon_full_alt)
```

When all checks pass, the lights stream the pattern until the next check fails.
No API keys needed.

## Hardware

Govee lights that support LAN Control and DreamView/razer streaming. Tested on
H6061, H6062 and H6076. Enable LAN Control in the Govee app.

## Setup

```bash
git clone https://github.com/m3tzg3r/aurora-alert.git
cd aurora-alert
pip install requests
cp config.example.json config.json   # set lat/lon
./aurora_watch.py --check
./aurora_watch.py --demo 60
```

Keep it running with cron (`flock` prevents duplicates):

```
*/15 * * * * flock -n /path/to/aurora-alert/.aurora.lock /path/to/aurora-alert/aurora_watch.py
```

Each check is logged to `aurora_watch.log`, including the aurora probability by
latitude, which helps tune `box_north` and `aurora_min` for your location.

## Config

| Key | Meaning |
|---|---|
| `lat`, `lon` | Location (required) |
| `check_interval_min` | Minutes between checks |
| `sun_max_elev` | Max sun elevation, degrees |
| `cloud_max` | Max cloud cover, % |
| `aurora_min` | Min aurora probability with no moon, % |
| `moon_penalty` | Extra % needed under a full, high moon |
| `moon_full_alt` | Moon altitude where the penalty peaks, degrees |
| `box_north`, `box_lon` | Search area: degrees north, ± degrees longitude |
| `zones` | Color zones per frame |
| `brightness` | 1-100 |
| `claim_file` | Optional. A file shared with other scripts driving the same lights; the most recent start wins. |
