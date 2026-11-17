#!/usr/bin/env python3
"""Weather Planner: decision-oriented weather for an agent, using Open-Meteo (no API key).

Subcommands (all print JSON by default, or a short text summary with --format text):
  geocode     place name -> candidate coordinates (ambiguity is reported, never guessed)
  forecast    current conditions + daily (and optional hourly) forecast, up to 7 days
  plan        best time windows for an activity on a date (rain, feels-like, wind, UV, air quality)
  events      per-event advice for a list of timed events read from stdin (titles stay local)
  airquality  hourly US AQI and PM2.5 summary

Reliability: bounded retries with backoff, an on-disk cache, and a clearly marked stale-cache
fallback when the provider is unreachable. Offline mode: --fixture DIR (output is marked
synthetic). Standard library only; Python 3.9+.

Data: Open-Meteo (https://open-meteo.com/), CC BY 4.0. Not for emergency or severe-weather use.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

VERSION = '1.1.0'
FORECAST_API = 'https://api.open-meteo.com/v1/forecast'
GEOCODE_API = 'https://geocoding-api.open-meteo.com/v1/search'
AIR_API = 'https://air-quality-api.open-meteo.com/v1/air-quality'
USER_AGENT = f'hermes-weather-planner/{VERSION}'
MAX_BYTES = 2 * 1024 * 1024
MAX_DAYS = 7
MAX_EVENTS = 50
MAX_LOCATIONS = 10
STALE_LIMIT_S = 24 * 3600
TTL = {'geocode': 30 * 86400, 'forecast': 30 * 60, 'airquality': 60 * 60}

EXIT_USAGE, EXIT_AMBIGUOUS, EXIT_UNAVAILABLE = 2, 3, 4

# WMO weather interpretation codes (Open-Meteo documentation).
WMO = {
    0: 'clear sky', 1: 'mainly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'fog',
    48: 'depositing rime fog', 51: 'light drizzle', 53: 'drizzle', 55: 'dense drizzle',
    56: 'light freezing drizzle', 57: 'freezing drizzle', 61: 'light rain', 63: 'rain',
    65: 'heavy rain', 66: 'light freezing rain', 67: 'freezing rain', 71: 'light snow',
    73: 'snow', 75: 'heavy snow', 77: 'snow grains', 80: 'light rain showers',
    81: 'rain showers', 82: 'violent rain showers', 85: 'snow showers', 86: 'heavy snow showers',
    95: 'thunderstorm', 96: 'thunderstorm with hail', 99: 'thunderstorm with heavy hail'}
THUNDER = {95, 96, 99}
SNOW_ICE = {56, 57, 66, 67, 71, 73, 75, 77, 85, 86}

# Activity profiles: comfortable feels-like range (°C), wind tolerance (km/h), UV limit,
# rain sensitivity (0..1 multiplier on probability), whether daylight is required.
PROFILES = {
    'run':             {'ideal': (6, 18),  'wind': 25, 'uv': 6, 'rain': 0.8, 'daylight': True},
    'walk':            {'ideal': (12, 25), 'wind': 30, 'uv': 7, 'rain': 0.7, 'daylight': True},
    'bike':            {'ideal': (12, 25), 'wind': 20, 'uv': 7, 'rain': 0.9, 'daylight': True},
    'commute':         {'ideal': (5, 28),  'wind': 35, 'uv': 9, 'rain': 0.6, 'daylight': False},
    'picnic':          {'ideal': (18, 27), 'wind': 20, 'uv': 6, 'rain': 1.0, 'daylight': True},
    'outdoor-meeting': {'ideal': (16, 26), 'wind': 20, 'uv': 6, 'rain': 1.0, 'daylight': True},
}


class PlannerError(Exception):
    def __init__(self, message, code=EXIT_USAGE, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# --------------------------------------------------------------------------- fetching

class Fetcher:
    """GET JSON with retries, a cache and stale fallback; or read recorded fixtures."""

    def __init__(self, *, fixture_dir=None, cache_dir=None, use_cache=True, retries=2,
                 timeout=10.0, opener=None, sleep=time.sleep, clock=time.time):
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.use_cache = use_cache and not self.fixture_dir
        self.retries = max(0, min(int(retries), 5))
        self.timeout = max(1.0, min(float(timeout), 60.0))
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.clock = clock
        self.notes = []  # provenance for the output: stale / synthetic markers

    def get(self, kind, url, params):
        if self.fixture_dir:
            path = self.fixture_dir / f'{kind}.json'
            if not path.is_file():
                raise PlannerError(f'Fixture {path.name} not found', EXIT_UNAVAILABLE)
            self.notes.append({'kind': kind, 'synthetic': True})
            return json.loads(path.read_text(encoding='utf-8'))
        full = url + '?' + urllib.parse.urlencode(params)
        key = hashlib.sha256(full.encode()).hexdigest()[:32]
        cached = self._cache_read(key) if self.use_cache else None
        now = self.clock()
        if cached and now - cached['saved_at'] < TTL[kind]:
            self.notes.append({'kind': kind, 'cached': True, 'age_s': round(now - cached['saved_at'])})
            return cached['data']
        try:
            data = self._fetch(full)
        except PlannerError:
            if cached and now - cached['saved_at'] < STALE_LIMIT_S:
                self.notes.append({'kind': kind, 'stale': True, 'age_s': round(now - cached['saved_at'])})
                return cached['data']
            raise
        if self.use_cache:
            self._cache_write(key, data)
        self.notes.append({'kind': kind, 'live': True})
        return data

    def _fetch(self, url):
        last = None
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(min(8.0, 1.0 * 2 ** (attempt - 1)))
            try:
                request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise PlannerError('Provider response too large', EXIT_UNAVAILABLE)
                data = json.loads(raw)
                if isinstance(data, dict) and data.get('error'):
                    raise PlannerError('Provider rejected the request: ' + str(data.get('reason', ''))[:200])
                return data
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code < 500 and exc.code != 429:
                    raise PlannerError(f'Provider HTTP {exc.code}', EXIT_UNAVAILABLE) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = exc
        raise PlannerError(f'Weather provider unreachable after {self.retries + 1} attempts '
                           f'({type(last).__name__})', EXIT_UNAVAILABLE)

    def _cache_read(self, key):
        try:
            item = json.loads((self.cache_dir / f'{key}.json').read_text(encoding='utf-8'))
            return item if isinstance(item, dict) and 'saved_at' in item else None
        except (OSError, ValueError):
            return None

    def _cache_write(self, key, data):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix='.tmp')
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump({'saved_at': self.clock(), 'data': data}, handle)
            os.replace(tmp, self.cache_dir / f'{key}.json')
        except OSError:
            pass  # caching is best effort


def default_cache_dir():
    # No environment variables are read (the skill needs no secrets); override with --cache-dir.
    return Path.home() / '.cache' / 'weather-planner'


# --------------------------------------------------------------------------- locations

def check_coords(lat, lon):
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        raise PlannerError('latitude and longitude are required (or use --place)')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise PlannerError('latitude must be in [-90, 90] and longitude in [-180, 180]')
    return round(lat, 4), round(lon, 4)


US_STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California',
    'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware', 'FL': 'Florida', 'GA': 'Georgia',
    'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland', 'MA': 'Massachusetts',
    'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana',
    'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont',
    'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
    'DC': 'District of Columbia'}


def _qualifier_matches(q, r):
    q = q.strip().lower()
    q_full = US_STATES.get(q.upper(), q).lower()
    fields = [str(r.get(k) or '').lower() for k in ('admin1', 'admin2', 'country', 'country_code')]
    return any(f and (f == q or f == q_full or (len(q) > 3 and f.startswith(q))) for f in fields)


def geocode(fetcher, name, count=5, country=None):
    """Candidates for a place. "City, Region[, Country]" qualifiers filter the results locally;
    only the city part is sent to the geocoder."""
    name = (name or '').strip()
    if not 2 <= len(name) <= 100:
        raise PlannerError('place name must be 2-100 characters')
    city, *qualifiers = [p.strip() for p in name.split(',') if p.strip()] or ['']
    if country:
        qualifiers.append(country)
    data = fetcher.get('geocode', GEOCODE_API, {'name': city, 'count': 10 if qualifiers else max(1, min(count, 10)),
                                                 'language': 'en', 'format': 'json'})
    results = []
    for r in data.get('results') or []:
        if not all(_qualifier_matches(q, r) for q in qualifiers):
            continue
        label = ', '.join(x for x in (r.get('name'), r.get('admin1'), r.get('country')) if x)
        results.append({'name': r.get('name'), 'label': label, 'latitude': r.get('latitude'),
                        'longitude': r.get('longitude'), 'timezone': r.get('timezone'),
                        'country_code': r.get('country_code'), 'population': r.get('population'),
                        'exact': str(r.get('name', '')).lower() == city.lower()})
    return results[:max(1, min(count, 10))]


DOMINANCE = 10


def pick_unambiguous(found):
    """The single intended place, or None. Exact name matches win over partial ones
    ("Seattle" vs "Seattle Hill"); a dominant exact match wins with a note."""
    exact = [c for c in found if c['exact']] or found
    distinct = {(round(c['latitude'], 1), round(c['longitude'], 1)) for c in exact}
    if len(distinct) == 1:
        return exact[0]
    # Among several exact matches, one >= DOMINANCE x more populous than every other is "the"
    # place ("Paris" -> France); comparable ones ("Springfield") stay ambiguous.
    ranked = sorted(exact, key=lambda c: -(c.get('population') or 0))
    top_pop = ranked[0].get('population') or 0
    rest_pop = max((c.get('population') or 0) for c in ranked[1:])
    if top_pop > 0 and top_pop >= DOMINANCE * rest_pop:
        others = '; '.join(c['label'] for c in ranked[1:5])
        return dict(ranked[0], note='Assumed the most populous match. Other places with this name: '
                    + others + '. Mention this if the user might mean another one.')
    return None


def resolve_location(fetcher, args):
    if getattr(args, 'place', None):
        found = geocode(fetcher, args.place, count=5, country=getattr(args, 'country', None))
        if not found:
            raise PlannerError(f'No place found for "{args.place}"')
        c = found[0] if getattr(args, 'first', False) else pick_unambiguous(found)
        if c is None:
            raise PlannerError(f'"{args.place}" is ambiguous. Do not guess: show these candidates to the '
                               'user, ask which one they mean, then call again with --lat/--lon or a '
                               'qualified name such as "City, State"', EXIT_AMBIGUOUS,
                               {'candidates': [{k: v for k, v in x.items() if k != 'exact'} for x in found]})
        lat, lon = check_coords(c['latitude'], c['longitude'])
        loc = {'name': c['label'], 'latitude': lat, 'longitude': lon}
        if c.get('note'):
            loc['note'] = c['note']
        return loc
    lat, lon = check_coords(args.lat, args.lon)
    return {'name': getattr(args, 'name', None) or f'{lat}, {lon}', 'latitude': lat, 'longitude': lon}


# --------------------------------------------------------------------------- forecast data

HOURLY = ('temperature_2m', 'apparent_temperature', 'precipitation_probability', 'precipitation',
          'weather_code', 'wind_speed_10m', 'uv_index')
DAILY = ('weather_code', 'temperature_2m_max', 'temperature_2m_min', 'precipitation_probability_max',
         'precipitation_sum', 'uv_index_max', 'sunrise', 'sunset', 'wind_speed_10m_max')
CURRENT = ('temperature_2m', 'apparent_temperature', 'weather_code', 'wind_speed_10m', 'precipitation')


def fetch_forecast(fetcher, loc, days):
    data = fetcher.get('forecast', FORECAST_API, {
        'latitude': loc['latitude'], 'longitude': loc['longitude'], 'timezone': 'auto',
        'forecast_days': days, 'current': ','.join(CURRENT), 'hourly': ','.join(HOURLY),
        'daily': ','.join(DAILY)})
    for section, keys in (('hourly', HOURLY), ('daily', DAILY)):
        block = data.get(section) or {}
        n = len(block.get('time') or [])
        if not n or any(len(block.get(k) or []) != n for k in keys):
            raise PlannerError(f'Incomplete {section} forecast from provider', EXIT_UNAVAILABLE)
    try:
        ZoneInfo(data['timezone'])
    except Exception as exc:
        raise PlannerError('Provider returned an unknown timezone', EXIT_UNAVAILABLE) from exc
    return data


def fetch_air(fetcher, loc, days):
    try:
        data = fetcher.get('airquality', AIR_API, {
            'latitude': loc['latitude'], 'longitude': loc['longitude'], 'timezone': 'auto',
            'forecast_days': min(days, 5), 'hourly': 'us_aqi,pm2_5'})
    except PlannerError:
        return {}  # air quality is optional input to planning
    block = data.get('hourly') or {}
    return {t: {'us_aqi': a, 'pm2_5': p} for t, a, p in
            zip(block.get('time') or [], block.get('us_aqi') or [], block.get('pm2_5') or [])}


def hours(data, air=None):
    h = data['hourly']
    out = []
    for i, t in enumerate(h['time']):
        row = {k: h[k][i] for k in HOURLY}
        row['time'] = t
        aq = (air or {}).get(t, {})
        row['us_aqi'], row['pm2_5'] = aq.get('us_aqi'), aq.get('pm2_5')
        out.append(row)
    return out


def daylight(data):
    d = data['daily']
    return {day: (sr, ss) for day, sr, ss in zip(d['time'], d['sunrise'], d['sunset'])}


def condition(code):
    return WMO.get(code, f'code {code}') if code is not None else 'unknown'


# --------------------------------------------------------------------------- units

def temp(v, units):
    if v is None:
        return None
    return round(v * 9 / 5 + 32) if units == 'imperial' else round(v, 1)


def speed(v, units):
    if v is None:
        return None
    return round(v * 0.621371, 1) if units == 'imperial' else round(v, 1)


def unit_labels(units):
    return ({'temperature': '°F', 'wind': 'mph', 'precipitation': 'in'} if units == 'imperial'
            else {'temperature': '°C', 'wind': 'km/h', 'precipitation': 'mm'})


def precip(v, units):
    if v is None:
        return None
    return round(v / 25.4, 2) if units == 'imperial' else round(v, 1)


# --------------------------------------------------------------------------- scoring

def score_hour(row, profile, is_day):
    """0..100 suitability of one hour for an activity, plus the reasons that cost points."""
    if profile['daylight'] and not is_day:
        return None, ['dark']
    penalty, reasons = 0.0, []
    pp = row.get('precipitation_probability') or 0
    rain_cost = pp * profile['rain'] * 0.8
    if (row.get('precipitation') or 0) >= 0.2:
        rain_cost += 20
    if rain_cost >= 8:
        reasons.append(f'rain {pp}%')
    penalty += rain_cost
    code = row.get('weather_code')
    if code in THUNDER:
        penalty += 60
        reasons.append('thunderstorm')
    elif code in SNOW_ICE:
        penalty += 25
        reasons.append(condition(code))
    feels = row.get('apparent_temperature')
    lo, hi = profile['ideal']
    if feels is not None and not lo <= feels <= hi:
        gap = (lo - feels) if feels < lo else (feels - hi)
        penalty += min(40, gap * 4)
        reasons.append(('cold' if feels < lo else 'hot') + f' (feels {round(feels)}°C)')
    wind = row.get('wind_speed_10m') or 0
    if wind > profile['wind']:
        penalty += min(25, (wind - profile['wind']) * 1.5)
        reasons.append(f'wind {round(wind)} km/h')
    uv = row.get('uv_index') or 0
    if uv > profile['uv']:
        penalty += min(20, (uv - profile['uv']) * 6)
        reasons.append(f'UV {round(uv, 1)}')
    aqi = row.get('us_aqi')
    if aqi is not None and aqi > 50:
        cost = min(30, (aqi - 100) * 0.4) if aqi > 100 else (aqi - 50) * 0.1
        penalty += cost
        if aqi > 100:
            reasons.append(f'air quality AQI {aqi}')
    return max(0, round(100 - penalty)), reasons


def rating(score):
    return 'great' if score >= 80 else 'good' if score >= 60 else 'fair' if score >= 40 else 'poor'


def is_daytime(t, light):
    sr, ss = light.get(t[:10], (None, None))
    if not sr or not ss:
        return True
    # an hour counts as daylight if it starts after sunrise-30min and ends before sunset+30min
    start = datetime.fromisoformat(t)
    return (datetime.fromisoformat(sr) - timedelta(minutes=30) <= start and
            start + timedelta(hours=1) <= datetime.fromisoformat(ss) + timedelta(minutes=30))


def plan_windows(rows, light, activity, day, start_hm, end_hm, duration, top=3):
    profile = PROFILES[activity]
    lo = datetime.fromisoformat(f'{day}T{start_hm}')
    hi = datetime.fromisoformat(f'{day}T{end_hm}')
    scored = []
    for r in rows:
        t = datetime.fromisoformat(r['time'])
        if lo <= t and t + timedelta(hours=1) <= hi:
            s, why = score_hour(r, profile, is_daytime(r['time'], light))
            scored.append((t, s, why, r))
    windows = []
    for i in range(len(scored) - duration + 1):
        chunk = scored[i:i + duration]
        if any(c[1] is None for c in chunk):
            continue
        if any(chunk[k + 1][0] - chunk[k][0] != timedelta(hours=1) for k in range(duration - 1)):
            continue
        s = min(c[1] for c in chunk)  # a window is as good as its worst hour
        why = sorted({w for c in chunk for w in c[2]})
        rs = [c[3] for c in chunk]
        windows.append({'start': chunk[0][0].strftime('%H:%M'),
                        'end': (chunk[-1][0] + timedelta(hours=1)).strftime('%H:%M'),
                        'score': s, 'rating': rating(s), 'concerns': why,
                        'max_rain_probability_pct': max((r.get('precipitation_probability') or 0) for r in rs),
                        'feels_like_c': [round(min(r['apparent_temperature'] for r in rs), 1),
                                         round(max(r['apparent_temperature'] for r in rs), 1)],
                        'max_wind_kmh': round(max((r.get('wind_speed_10m') or 0) for r in rs), 1),
                        'max_uv': round(max((r.get('uv_index') or 0) for r in rs), 1),
                        'max_us_aqi': max((r['us_aqi'] for r in rs if r.get('us_aqi') is not None), default=None)})
    best, taken = [], []
    for w in sorted(windows, key=lambda w: (-w['score'], w['start'])):
        a, b = w['start'], w['end']
        if all(b <= x or a >= y for x, y in taken):
            best.append(w)
            taken.append((a, b))
        if len(best) == top:
            break
    worst = min(windows, key=lambda w: (w['score'], w['start'])) if windows else None
    dark = sum(1 for c in scored if c[1] is None)
    return best, worst, len(scored), dark


# --------------------------------------------------------------------------- events

def parse_event_time(value, zone):
    if not isinstance(value, str) or len(value) > 40:
        raise PlannerError('event times must be ISO 8601 strings')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise PlannerError(f'invalid event time: {value!r}') from exc
    return dt.astimezone(zone).replace(tzinfo=None) if dt.tzinfo else dt


def advise(rows, outdoor, light):
    pp = max((r.get('precipitation_probability') or 0) for r in rows)
    mm = sum((r.get('precipitation') or 0) for r in rows)
    feels = [r['apparent_temperature'] for r in rows if r.get('apparent_temperature') is not None]
    wind = max((r.get('wind_speed_10m') or 0) for r in rows)
    uv = max((r.get('uv_index') or 0) for r in rows)
    aqi = max((r['us_aqi'] for r in rows if r.get('us_aqi') is not None), default=None)
    codes = {r.get('weather_code') for r in rows}
    advice = []
    if codes & THUNDER:
        advice.append('Thunderstorm possible: avoid open areas; plan an indoor alternative.' if outdoor
                      else 'Thunderstorm possible around this time; allow extra travel time.')
    if pp >= 60 or mm >= 1:
        advice.append('Rain likely: bring an umbrella' + ('; consider moving indoors or a backup venue.' if outdoor else '.'))
    elif pp >= 30:
        advice.append('Chance of rain: a compact umbrella is a good idea.')
    if codes & SNOW_ICE:
        advice.append('Snow or ice possible: allow extra travel time.')
    if feels and min(feels) < 8:
        advice.append(f'Cold (feels {round(min(feels))}°C): bring a warm layer.')
    elif feels and min(feels) < 14 and outdoor:
        advice.append(f'Cool (feels {round(min(feels))}°C): bring a jacket.')
    if feels and max(feels) >= 30:
        advice.append(f'Hot (feels {round(max(feels))}°C): water and shade' + (' for attendees.' if outdoor else '.'))
    if wind >= 35:
        advice.append(f'Strong wind ({round(wind)} km/h).')
    if outdoor and uv >= 6 and any(is_daytime(r['time'], light) for r in rows):
        advice.append(f'High UV ({round(uv, 1)}): sunscreen and shade.')
    if outdoor and aqi is not None and aqi > 100:
        advice.append(f'Air quality AQI {aqi}: unhealthy for sensitive groups; consider indoors.')
    if not advice:
        advice.append('No weather concerns.')
    return {'max_rain_probability_pct': pp, 'precipitation_mm': round(mm, 1),
            'feels_like_c': [round(min(feels), 1), round(max(feels), 1)] if feels else None,
            'max_wind_kmh': round(wind, 1), 'max_uv': round(uv, 1), 'max_us_aqi': aqi,
            'conditions': sorted({condition(c) for c in codes}), 'advice': advice}


def load_events(stream):
    raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise PlannerError('events input too large')
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PlannerError('events input must be JSON (a list, or {"events": [...]})') from exc
    events = data.get('events') if isinstance(data, dict) else data
    if not isinstance(events, list) or not events:
        raise PlannerError('no events supplied')
    if len(events) > MAX_EVENTS:
        raise PlannerError(f'at most {MAX_EVENTS} events per call')
    return events


# --------------------------------------------------------------------------- commands

def envelope(command, fetcher, loc=None, tz=None, units='metric', **body):
    notes = fetcher.notes
    out = {'tool': 'weather-planner', 'version': VERSION, 'command': command,
           'source': 'Open-Meteo (open-meteo.com, CC BY 4.0)',
           'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'synthetic': any(n.get('synthetic') for n in notes),
           'stale': any(n.get('stale') for n in notes)}
    if out['stale']:
        out['stale_age_minutes'] = max(round(n['age_s'] / 60) for n in notes if n.get('stale'))
    if loc:
        out['location'] = dict(loc, timezone=tz) if tz else loc
    if command != 'geocode':
        out['units'] = unit_labels(units)
    out.update(body)
    return out


def cmd_geocode(f, a):
    found = geocode(f, a.name, count=a.count, country=a.country)
    summary = (f'{len(found)} match(es) for "{a.name}": ' + '; '.join(c['label'] for c in found)) if found \
        else f'No place found for "{a.name}".'
    return envelope('geocode', f, candidates=found, ambiguous=bool(found) and pick_unambiguous(found) is None,
                    summary=summary)


def cmd_forecast(f, a):
    loc = resolve_location(f, a)
    days = check_days(a.days)
    data = fetch_forecast(f, loc, days)
    u = a.units
    cur = data.get('current') or {}
    current = {'time': cur.get('time'), 'condition': condition(cur.get('weather_code')),
               'temperature': temp(cur.get('temperature_2m'), u),
               'feels_like': temp(cur.get('apparent_temperature'), u),
               'wind': speed(cur.get('wind_speed_10m'), u), 'precipitation': precip(cur.get('precipitation'), u)}
    d = data['daily']
    daily = [{'date': d['time'][i], 'condition': condition(d['weather_code'][i]),
              'high': temp(d['temperature_2m_max'][i], u), 'low': temp(d['temperature_2m_min'][i], u),
              'rain_probability_pct': d['precipitation_probability_max'][i],
              'precipitation': precip(d['precipitation_sum'][i], u), 'uv_index_max': d['uv_index_max'][i],
              'wind_max': speed(d['wind_speed_10m_max'][i], u),
              'sunrise': d['sunrise'][i][11:], 'sunset': d['sunset'][i][11:]} for i in range(len(d['time']))]
    body = {'current': current, 'daily': daily}
    if a.hourly:
        now = (cur.get('time') or d['time'][0] + 'T00:00')[:13]
        body['hourly'] = [{'time': r['time'], 'condition': condition(r['weather_code']),
                           'temperature': temp(r['temperature_2m'], u), 'feels_like': temp(r['apparent_temperature'], u),
                           'rain_probability_pct': r['precipitation_probability'], 'wind': speed(r['wind_speed_10m'], u),
                           'uv_index': r['uv_index']} for r in hours(data) if r['time'][:13] >= now][:24]
    lab = unit_labels(u)['temperature']
    t0 = daily[0]
    body['summary'] = (f"{loc['name']}: now {current['condition']}, {current['temperature']}{lab} "
                       f"(feels {current['feels_like']}{lab}). Today {t0['condition']}, "
                       f"{t0['low']}–{t0['high']}{lab}, rain chance {t0['rain_probability_pct']}%, UV max {t0['uv_index_max']}.")
    return envelope('forecast', f, loc, data['timezone'], u, **body)


def check_days(days):
    if not 1 <= days <= MAX_DAYS:
        raise PlannerError(f'--days must be 1..{MAX_DAYS}')
    return days


def check_hm(value, name):
    try:
        datetime.strptime(value, '%H:%M')
    except ValueError as exc:
        raise PlannerError(f'{name} must be HH:MM') from exc
    return value


def local_today(tzname, now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(tzname)).date()


def cmd_plan(f, a):
    if a.activity not in PROFILES:
        raise PlannerError('activity must be one of: ' + ', '.join(PROFILES))
    if not 1 <= a.duration <= 4:
        raise PlannerError('--duration must be 1..4 hours')
    start_hm, end_hm = check_hm(a.start, '--from'), check_hm(a.end, '--to')
    if end_hm <= start_hm:
        raise PlannerError('--to must be after --from')
    loc = resolve_location(f, a)
    data = fetch_forecast(f, loc, MAX_DAYS)
    tz = data['timezone']
    now = a.now or datetime.now(timezone.utc)
    today = local_today(tz, now)
    day = a.date or today.isoformat()
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise PlannerError('--date must be YYYY-MM-DD') from exc
    if day not in data['daily']['time']:
        raise PlannerError(f'{day} is outside the {MAX_DAYS}-day forecast range '
                           f"({data['daily']['time'][0]}..{data['daily']['time'][-1]})")
    air = {} if a.no_air else fetch_air(f, loc, MAX_DAYS)
    rows = hours(data, air)
    requested_start, rolled = start_hm, None

    def search(d, s):
        if d == today.isoformat():  # never recommend a time that has already passed
            s = max(s, now.astimezone(ZoneInfo(tz)).strftime('%H:00'))
        if s >= end_hm:
            return [], None, 0, 0, s
        return (*plan_windows(rows, daylight(data), a.activity, d, s, end_hm, a.duration), s)

    best, worst, considered, dark, start_hm = search(day, start_hm)
    if not best and not a.date:  # nothing left today: look at tomorrow instead
        tomorrow = (today + timedelta(days=1)).isoformat()
        if tomorrow in data['daily']['time']:
            rolled, day = day, tomorrow
            best, worst, considered, dark, start_hm = search(day, requested_start)
    light = daylight(data).get(day, ('', ''))
    if best:
        top = best[0]
        tail = f" (concerns: {', '.join(top['concerns'])})" if top['concerns'] else ''
        summary = (f"Best time for {a.activity} on {day}: {top['start']}–{top['end']} "
                   f"({top['rating']}, score {top['score']}){tail}.")
        if worst and worst['score'] < top['score'] - 15:
            summary += f" Avoid {worst['start']}–{worst['end']} ({', '.join(worst['concerns']) or 'lower score'})."
    else:
        summary = (f'No suitable {a.duration}-hour window for {a.activity} on {day} between '
                   f'{start_hm} and {end_hm}' + (' (outside daylight).' if dark else '.'))
    if rolled:
        summary = f'No suitable time left today ({rolled}); planning tomorrow. ' + summary
    return envelope('plan', f, loc, tz, 'metric', date=day, rolled_over_from=rolled, activity=a.activity,
                    duration_hours=a.duration, search_window=[start_hm, end_hm],
                    sunrise=light[0][11:], sunset=light[1][11:], hours_considered=considered,
                    hours_excluded_dark=dark, air_quality_used=bool(air),
                    best_windows=best, worst_window=worst, summary=summary)


def cmd_events(f, a):
    events = load_events(a.input)
    default = None
    if a.place or a.lat is not None:
        default = resolve_location(f, a)
    groups = {}
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise PlannerError(f'event {i} must be an object')
        if ev.get('latitude') is not None or ev.get('longitude') is not None:
            lat, lon = check_coords(_num(ev.get('latitude')), _num(ev.get('longitude')))
            loc = {'name': f'{lat}, {lon}', 'latitude': lat, 'longitude': lon}
        elif default:
            loc = default
        else:
            raise PlannerError(f'event {i} has no coordinates and no default --lat/--lon/--place')
        groups.setdefault((loc['latitude'], loc['longitude']), (loc, []))[1].append((i, ev))
    if len(groups) > MAX_LOCATIONS:
        raise PlannerError(f'at most {MAX_LOCATIONS} distinct event locations per call')
    results = [None] * len(events)
    for loc, items in groups.values():
        # Only coordinates and dates go to the provider; event titles never leave this process.
        data = fetch_forecast(f, loc, MAX_DAYS)
        zone = ZoneInfo(data['timezone'])
        air = {} if a.no_air else fetch_air(f, loc, MAX_DAYS)
        rows = hours(data, air)
        light = daylight(data)
        index = {r['time']: r for r in rows}
        for i, ev in items:
            start = parse_event_time(ev.get('start'), zone)
            end = parse_event_time(ev['end'], zone) if ev.get('end') else start + timedelta(hours=1)
            if end <= start or end - start > timedelta(hours=24):
                raise PlannerError(f'event {i}: end must be after start and within 24 hours')
            label = str(ev.get('label') or ev.get('title') or f'event {i + 1}')[:120]
            slot, covered = start.replace(minute=0, second=0, microsecond=0), []
            while slot < end:
                if slot.strftime('%Y-%m-%dT%H:%M') in index:
                    covered.append(index[slot.strftime('%Y-%m-%dT%H:%M')])
                slot += timedelta(hours=1)
            item = {'label': label, 'start': start.strftime('%Y-%m-%d %H:%M'), 'end': end.strftime('%Y-%m-%d %H:%M'),
                    'timezone': data['timezone'], 'outdoor': bool(ev.get('outdoor')),
                    'location': {'latitude': loc['latitude'], 'longitude': loc['longitude']}}
            if covered:
                item.update(advise(covered, item['outdoor'], light))
            else:
                item.update({'advice': ['No forecast available yet for this time (outside the '
                                        f'{MAX_DAYS}-day range or in the past).'], 'no_forecast': True})
            results[i] = item
    flagged = [r for r in results if r.get('advice') and r['advice'] != ['No weather concerns.']
               and not r.get('no_forecast')]
    summary = (f'{len(results)} event(s) checked; {len(flagged)} with weather advice'
               + (': ' + '; '.join(f"{r['label']} — {r['advice'][0]}" for r in flagged[:5]) if flagged else '.'))
    return envelope('events', f, events=results, summary=summary)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cmd_airquality(f, a):
    loc = resolve_location(f, a)
    days = min(check_days(a.days), 5)
    data = f.get('airquality', AIR_API, {'latitude': loc['latitude'], 'longitude': loc['longitude'],
                                         'timezone': 'auto', 'forecast_days': days, 'hourly': 'us_aqi,pm2_5'})
    h = data.get('hourly') or {}
    per_day = {}
    for t, aqi, pm in zip(h.get('time') or [], h.get('us_aqi') or [], h.get('pm2_5') or []):
        if aqi is None:
            continue
        d = per_day.setdefault(t[:10], {'date': t[:10], 'max_us_aqi': aqi, 'max_at': t[11:16], 'max_pm2_5': pm})
        if aqi > d['max_us_aqi']:
            d.update(max_us_aqi=aqi, max_at=t[11:16])
        if pm is not None and (d['max_pm2_5'] is None or pm > d['max_pm2_5']):
            d['max_pm2_5'] = pm
    daily = list(per_day.values())
    for d in daily:
        d['category'] = aqi_category(d['max_us_aqi'])
    summary = (f"{loc['name']}: " + '; '.join(f"{d['date']} max AQI {d['max_us_aqi']} ({d['category']}) at {d['max_at']}"
                                             for d in daily)) if daily else 'No air-quality data.'
    return envelope('airquality', f, loc, data.get('timezone'), 'metric', daily=daily, summary=summary)


def aqi_category(aqi):
    for limit, name in ((50, 'good'), (100, 'moderate'), (150, 'unhealthy for sensitive groups'),
                        (200, 'unhealthy'), (300, 'very unhealthy')):
        if aqi <= limit:
            return name
    return 'hazardous'


# --------------------------------------------------------------------------- CLI

def parse_now(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_parser():
    p = argparse.ArgumentParser(prog='weather_planner.py', description=__doc__.split('\n')[0])
    p.add_argument('--version', action='version', version=VERSION)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--format', choices=('json', 'text'), default='json')
    common.add_argument('--fixture', metavar='DIR', help='offline mode: read recorded responses from DIR')
    common.add_argument('--no-cache', action='store_true')
    common.add_argument('--cache-dir')
    common.add_argument('--retries', type=int, default=2)
    common.add_argument('--timeout', type=float, default=10.0)
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument('--place', help='place name, optionally "City, State/Country" (ambiguous names exit 3)')
    where.add_argument('--country', help='country code/name filter for --place')
    where.add_argument('--first', action='store_true', help='accept the first --place match')
    where.add_argument('--lat', type=float)
    where.add_argument('--lon', type=float)
    where.add_argument('--name', help='display name for --lat/--lon')
    sub = p.add_subparsers(dest='command', required=True)

    g = sub.add_parser('geocode', parents=[common], help='place name -> coordinates')
    g.add_argument('name')
    g.add_argument('--count', type=int, default=5)
    g.add_argument('--country')
    g.set_defaults(run=cmd_geocode)

    fc = sub.add_parser('forecast', parents=[common, where], help='current + daily forecast')
    fc.add_argument('--days', type=int, default=3)
    fc.add_argument('--hourly', action='store_true', help='include the next 24 hours')
    fc.add_argument('--units', choices=('metric', 'imperial'), default='metric')
    fc.set_defaults(run=cmd_forecast)

    pl = sub.add_parser('plan', parents=[common, where], help='best time windows for an activity')
    pl.add_argument('--activity', required=True, choices=sorted(PROFILES))
    pl.add_argument('--date', help='YYYY-MM-DD in the location timezone (default: today there)')
    pl.add_argument('--from', dest='start', default='06:00')
    pl.add_argument('--to', dest='end', default='22:00')
    pl.add_argument('--duration', type=int, default=1, help='window length in hours (1-4)')
    pl.add_argument('--no-air', action='store_true', help='skip air-quality scoring')
    pl.add_argument('--now', type=parse_now, help=argparse.SUPPRESS)  # deterministic tests
    pl.set_defaults(run=cmd_plan)

    ev = sub.add_parser('events', parents=[common, where], help='advice for events read from stdin (JSON)')
    ev.add_argument('--input', type=argparse.FileType('r'), default=sys.stdin)
    ev.add_argument('--no-air', action='store_true')
    ev.set_defaults(run=cmd_events)

    aq = sub.add_parser('airquality', parents=[common, where], help='US AQI / PM2.5 by day')
    aq.add_argument('--days', type=int, default=2)
    aq.set_defaults(run=cmd_airquality)
    return p


def main(argv=None, fetcher=None, stdout=sys.stdout):
    args = build_parser().parse_args(argv)
    f = fetcher or Fetcher(fixture_dir=args.fixture, cache_dir=args.cache_dir, use_cache=not args.no_cache,
                           retries=args.retries, timeout=args.timeout)
    try:
        result = args.run(f, args)
        code = 0
    except PlannerError as exc:
        result = {'tool': 'weather-planner', 'version': VERSION, 'command': args.command,
                  'error': str(exc), **exc.details}
        code = exc.code
    if args.format == 'text':
        stdout.write((result.get('summary') or result.get('error', '')) + '\n')
        if result.get('candidates') and result.get('error'):
            for c in result['candidates']:
                stdout.write(f"  - {c['label']} ({c['latitude']}, {c['longitude']})\n")
        if result.get('synthetic'):
            stdout.write('(synthetic fixture data)\n')
        if result.get('stale'):
            stdout.write(f"(provider unreachable; cached data {result['stale_age_minutes']} min old)\n")
    else:
        json.dump(result, stdout, ensure_ascii=False, indent=1)
        stdout.write('\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
