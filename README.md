# hermes-weather-planner

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill tap with one skill,
**`weather-planner`**. It turns forecasts into decisions:

- **`plan`**: the best 1–4 hour windows for an activity (run, walk, bike, commute, picnic, outdoor
  meeting). Each hour is scored on rain probability, feels-like temperature, wind, UV and air
  quality. Hours outside daylight are excluded, past hours are never suggested, and when nothing is
  left today it plans tomorrow.
- **`events`**: advice per calendar event (umbrella, jacket, heat, wind, UV, air quality, move
  indoors). **Event titles never leave your machine**; only coordinates and dates are sent to the
  weather provider.
- **`geocode`**: place name → coordinates. Ambiguous names ("Springfield") return the candidates
  with exit code 3 instead of a guess.
- **`forecast`**: current conditions and up to 7 days, with optional hourly data, in °C/km/h or
  °F/mph.
- **`airquality`**: daily maximum US AQI, PM2.5 and category.

Reliability matters for an agent that runs unattended. Requests use bounded retries with backoff
and an on-disk cache. If the provider is down, cached data up to 24 hours old is returned and
marked `stale` with its age. An offline `--fixture` mode is available and its output is marked
`synthetic`. The skill is one Python file using only the standard library (Python 3.9+), needs no
API key, and has no install step.

## Install (Hermes skill tap)

```bash
hermes skills tap add XqFeng-Josie/hermes-weather-planner
hermes skills search weather-planner --source github
hermes skills install XqFeng-Josie/hermes-weather-planner/skills/weather-planner
```

Then ask your agent, for example:

- "When is the best time for a run in Seattle tomorrow?"
- "I have an outdoor team lunch at 12:00 on Friday in Los Angeles. Anything to plan for?"
- "What's the air quality in Delhi today?"

## Use without Hermes

```bash
P=skills/weather-planner/scripts/weather_planner.py
python3 $P plan --place "Seattle" --country US --first --activity run --format text
# No suitable time left today (2026-11-16); planning tomorrow. Best time for run on 2026-11-17: 12:00–13:00 (great, score 98). Avoid 07:00–08:00 (cold (feels 1°C)).
python3 $P forecast --place "Springfield"        # exit 3 + candidates (ambiguous)
echo '[{"label":"Team lunch","start":"2026-11-21T12:00","end":"2026-11-21T13:30","outdoor":true}]' \
  | python3 $P events --lat 34.05 --lon -118.24 --format text
```

Exit codes: `0` ok, `2` invalid input, `3` ambiguous place, `4` provider unavailable and no
usable cache. The cache is stored in `~/.cache/weather-planner` (change it with `--cache-dir`); turn it off
with `--no-cache`. The script reads no environment variables and needs no secrets.

## How `plan` scores an hour

Each hour starts at 100 and loses points for the following:

| Factor | Penalty |
| --- | --- |
| Rain | rain probability × activity sensitivity, plus 20 if ≥ 0.2 mm is forecast |
| Thunderstorm | 60 |
| Snow or ice | 25 |
| Feels-like temperature outside the activity's comfort range | 4 per °C, at most 40 |
| Wind above the activity's tolerance | 1.5 per km/h, at most 25 |
| UV above the activity's limit | 6 per point, at most 20 |
| US AQI above 100 | (AQI − 100) × 0.4, at most 30 |
| US AQI between 51 and 100 | (AQI − 50) × 0.1 |

A multi-hour window scores as its worst hour. The ratings are great (≥ 80), good (≥ 60),
fair (≥ 40) and poor. The comfort profiles are in `PROFILES` in the script.

## Development

```bash
python3 -m unittest discover -s tests -v   # 35 offline tests, no network
```

The fixtures in `skills/weather-planner/fixtures/` are real Open-Meteo responses recorded on
2026-11-16 for Los Angeles and "Springfield". CI runs the tests on Python 3.9, 3.12 and 3.13.

## Data and license

Weather, geocoding and air-quality data come from [Open-Meteo](https://open-meteo.com/) under
CC BY 4.0. Free use is limited to non-commercial use within their fair-use limits. This skill is
not a severe-weather warning service.

The code is MIT licensed. It was written as a CSCI 599 (USC) Assignment 3 advanced-integration
deliverable, developed with AI assistance (Claude Code) and reviewed by the author.
