---
name: weather-planner
description: Decision-oriented weather for planning. Finds the best time windows for an activity (run, walk, bike, commute, picnic, outdoor meeting) by scoring rain, feels-like temperature, wind, UV and air quality; gives per-event weather advice for a list of calendar events without sending event titles anywhere; plus geocoding with ambiguity handling, multi-day forecasts and air quality. Uses Open-Meteo (no API key). Not for emergency or severe-weather warnings.
version: 1.0.0
author: XqFeng-Josie
license: MIT
metadata:
  hermes:
    tags: [weather, planning, calendar, productivity, open-meteo]
    requires_toolsets: [terminal]
---

# Weather Planner

Answer weather questions in terms of **decisions** ("when should I run tomorrow?", "do I need an
umbrella for my 3 pm meeting?"), not raw numbers. One standard-library Python script, no API key,
no install step.

## When to use

- The user asks when to do something outdoors, or whether the weather affects a plan.
- The user shares or asks about calendar events and weather (umbrella, jacket, move indoors?).
- A morning briefing or daily plan needs a forecast line.
- The user asks for a forecast or air quality for a named place.

Do **not** use it for severe-weather/emergency alerts, historical climate, or marine/aviation
forecasts. Say that this skill does not cover them.

## Quick reference

Run with Python 3.9+; every command prints JSON (add `--format text` for a one-paragraph summary).

```bash
P="${HERMES_SKILL_DIR}/scripts/weather_planner.py"
python3 "$P" geocode "Springfield"                                   # candidates + timezone
python3 "$P" forecast --place "Los Angeles" --first --days 3          # current + daily
python3 "$P" forecast --lat 47.61 --lon -122.33 --hourly --units imperial
python3 "$P" plan --place "Seattle" --country US --first --activity run          # today (or tomorrow if nothing is left today)
python3 "$P" plan --lat 34.05 --lon -118.24 --activity picnic --date 2026-11-21 --duration 2 --from 10:00 --to 18:00
echo '[{"label":"Team lunch","start":"2026-11-21T12:00","end":"2026-11-21T13:30","outdoor":true}]' \
  | python3 "$P" events --lat 34.05 --lon -118.24
python3 "$P" airquality --place "Delhi" --first --days 2
```

Activities: `run`, `walk`, `bike`, `commute`, `picnic`, `outdoor-meeting`. Except for `commute`,
hours outside daylight are excluded.

## Procedure

1. **Location.** Use the place the user named or their known home city. Run `geocode` or pass
   `--place`. **Exit code 3 means the name is ambiguous.** Show the user the `candidates` and ask
   which one they mean, then call again with `--lat/--lon` (or `--country`). Use `--first` only when
   the user's wording already identifies the place (e.g. "Los Angeles, California").
2. **Pick the command.** Use `plan` for "when should I…", `events` for calendar items, and
   `forecast` for general conditions. Use `airquality` when the user asks, or for sensitive users.
3. **Events.** Pass events as JSON on stdin: `start` (ISO 8601, with an offset or in the location's
   local time), optional `end`, `label`, `outdoor` (true/false), and optional `latitude`/`longitude`
   per event. **Only coordinates and dates are sent to the provider.** Event labels stay on this
   machine and appear only in the output.
4. **Answer from the JSON.**
   - Lead with the recommendation: `summary`, `best_windows[0]`, or each event's first `advice`
     line. Include the time window, its `rating`, and the main `concerns`.
   - Mention `worst_window` if it is much worse.
   - Give temperatures in the units the user prefers. `plan` reports feels-like temperatures in °C;
     convert them if needed.
   - Say the data comes from Open-Meteo.
5. **Provenance flags.** If `stale` is true, say the provider was unreachable and the data is
   `stale_age_minutes` old. If `synthetic` is true, say it is sample data. Never present either as
   live data.
6. **Failures.**
   - Exit code 4: the provider is unreachable and there is no usable cache. Say weather is
     unavailable right now.
   - Exit code 2: the input was invalid. Fix the arguments from the `error` text.
   - Never invent a forecast or reuse another day's data.

## Pitfalls

- `plan` without `--date` never recommends a past hour. When nothing is left today it plans
  tomorrow and sets `rolled_over_from`, so tell the user it is about tomorrow.
- The forecast range is 7 days. Events beyond it return `no_forecast: true`. Say so rather than
  guessing.
- Precipitation probability is a probability, not certainty.
- Scores (0–100) are heuristics for comparing hours. They are not safety guarantees.
- Do not put email text, attendee names or other personal context into `--place`. Only the place
  name and coordinates leave the machine.

## Verification

- `python3 "$P" --version` prints the version.
- Offline self-check (no network):
  `python3 "$P" plan --lat 34.05 --lon -118.24 --activity run --date 2026-11-17 --fixture "${HERMES_SKILL_DIR}/fixtures" --format text`
  The output must end with `(synthetic fixture data)`.
- Live: `python3 "$P" forecast --place "Los Angeles" --first --format text` prints today's
  conditions.
