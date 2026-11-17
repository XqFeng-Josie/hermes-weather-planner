"""Offline tests for skills/weather-planner (no network): python -m unittest discover -s tests"""
import copy
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'weather-planner'
SCRIPT = SKILL / 'scripts' / 'weather_planner.py'
FIXTURES = SKILL / 'fixtures'

spec = importlib.util.spec_from_file_location('weather_planner', SCRIPT)
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

LA = ['--lat', '34.0522', '--lon', '-118.2437', '--name', 'Los Angeles']
NOW = ['--now', '2026-11-16T15:00:00Z']  # 07:00 in Los Angeles; keeps plan tests date-independent


def fixture(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


class FakeFetcher(wp.Fetcher):
    """Serves (possibly modified) fixtures and records every outgoing request."""

    def __init__(self, overrides=None):
        super().__init__(use_cache=False)
        self.data = {k: fixture(k) for k in ('geocode', 'forecast', 'airquality')}
        self.data.update(overrides or {})
        self.requests = []

    def get(self, kind, url, params):
        self.requests.append((kind, url, dict(params)))
        self.notes.append({'kind': kind, 'synthetic': True})
        return copy.deepcopy(self.data[kind])


def run(argv, fetcher=None, stdin=None):
    out = io.StringIO()
    if stdin is not None:
        old, sys.stdin = sys.stdin, io.StringIO(stdin)
    try:
        code = wp.main(argv, fetcher=fetcher or FakeFetcher(), stdout=out)
    finally:
        if stdin is not None:
            sys.stdin = old
    text = out.getvalue()
    return code, (json.loads(text) if text.lstrip().startswith('{') else text)


def rainy_forecast(day='2026-11-17', hours=range(12, 15), prob=80, code=63, mm=2.0):
    data = fixture('forecast')
    h = data['hourly']
    for i, t in enumerate(h['time']):
        if t[:10] == day and int(t[11:13]) in hours:
            h['precipitation_probability'][i] = prob
            h['weather_code'][i] = code
            h['precipitation'][i] = mm
    return data


class GeocodeTests(unittest.TestCase):
    def test_lists_candidates_with_labels(self):
        code, out = run(['geocode', 'Springfield'])
        self.assertEqual(code, 0)
        self.assertTrue(out['ambiguous'])
        self.assertEqual(out['candidates'][0]['label'], 'Springfield, Missouri, United States')
        self.assertIn('timezone', out['candidates'][0])

    def test_ambiguous_place_is_never_guessed(self):
        code, out = run(['forecast', '--place', 'Springfield'])
        self.assertEqual(code, wp.EXIT_AMBIGUOUS)
        self.assertEqual(len(out['candidates']), 5)
        self.assertIn('ambiguous', out['error'])

    def test_first_flag_accepts_top_match(self):
        code, out = run(['forecast', '--place', 'Springfield', '--first'])
        self.assertEqual(code, 0)
        self.assertEqual(out['location']['name'], 'Springfield, Missouri, United States')

    def test_name_validation(self):
        code, out = run(['geocode', 'x'])
        self.assertEqual(code, wp.EXIT_USAGE)


class ForecastTests(unittest.TestCase):
    def test_metric_forecast_shape(self):
        code, out = run(['forecast', *LA])
        self.assertEqual(code, 0)
        self.assertEqual(out['location']['timezone'], 'America/Los_Angeles')
        self.assertEqual(out['units']['temperature'], '°C')
        self.assertEqual(out['daily'][0]['date'], '2026-11-16')
        self.assertEqual(out['daily'][0]['condition'], 'drizzle')
        self.assertTrue(out['synthetic'])
        self.assertIn('Los Angeles: now clear sky', out['summary'])

    def test_imperial_conversion(self):
        code, out = run(['forecast', *LA, '--units', 'imperial'])
        self.assertEqual(out['units']['temperature'], '°F')
        self.assertEqual(out['daily'][0]['high'], round(21.9 * 9 / 5 + 32))
        self.assertEqual(out['units']['wind'], 'mph')

    def test_hourly_starts_at_current_hour(self):
        code, out = run(['forecast', *LA, '--hourly'])
        self.assertEqual(len(out['hourly']), 24)
        self.assertEqual(out['hourly'][0]['time'], '2026-11-16T18:00')

    def test_coordinate_and_days_validation(self):
        self.assertEqual(run(['forecast', '--lat', '91', '--lon', '0'])[0], wp.EXIT_USAGE)
        self.assertEqual(run(['forecast', *LA, '--days', '8'])[0], wp.EXIT_USAGE)
        self.assertEqual(run(['forecast'])[0], wp.EXIT_USAGE)

    def test_incomplete_provider_data_is_rejected(self):
        bad = fixture('forecast')
        bad['hourly']['uv_index'] = bad['hourly']['uv_index'][:5]
        code, out = run(['forecast', *LA], FakeFetcher({'forecast': bad}))
        self.assertEqual(code, wp.EXIT_UNAVAILABLE)


class PlanTests(unittest.TestCase):
    def test_run_windows_are_in_daylight_and_ranked(self):
        code, out = run(['plan', *LA, *NOW, '--activity', 'run', '--date', '2026-11-17'])
        self.assertEqual(code, 0)
        self.assertEqual(out['sunset'], '16:47')
        starts = [w['start'] for w in out['best_windows']]
        self.assertEqual(len(starts), 3)
        self.assertTrue(all('06:00' <= s <= '16:00' for s in starts))
        scores = [w['score'] for w in out['best_windows']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(out['hours_excluded_dark'], 0)

    def test_rain_moves_the_recommendation(self):
        fetcher = FakeFetcher({'forecast': rainy_forecast(hours=range(9, 17))})
        code, out = run(['plan', *LA, *NOW, '--activity', 'picnic', '--date', '2026-11-17'], fetcher)
        best = out['best_windows'][0]
        self.assertFalse(9 <= int(best['start'][:2]) < 17, best)
        self.assertIn('rain', ' '.join(out['worst_window']['concerns']))

    def test_thunderstorm_ranks_poor(self):
        fetcher = FakeFetcher({'forecast': rainy_forecast(hours=range(0, 24), prob=90, code=95)})
        code, out = run(['plan', *LA, *NOW, '--activity', 'walk', '--date', '2026-11-17'], fetcher)
        self.assertEqual(out['best_windows'][0]['rating'], 'poor')
        self.assertIn('thunderstorm', out['best_windows'][0]['concerns'])

    def test_unhealthy_air_is_a_concern(self):
        air = fixture('airquality')
        air['hourly']['us_aqi'] = [180] * len(air['hourly']['us_aqi'])
        code, out = run(['plan', *LA, *NOW, '--activity', 'run', '--date', '2026-11-17'],
                        FakeFetcher({'airquality': air}))
        self.assertTrue(out['air_quality_used'])
        self.assertIn('air quality AQI 180', out['best_windows'][0]['concerns'])
        code2, out2 = run(['plan', *LA, *NOW, '--activity', 'run', '--date', '2026-11-17', '--no-air'],
                          FakeFetcher({'airquality': air}))
        self.assertFalse(out2['air_quality_used'])

    def test_multi_hour_window_and_commute_at_night(self):
        code, out = run(['plan', *LA, *NOW, '--activity', 'commute', '--date', '2026-11-17',
                         '--from', '17:00', '--to', '22:00', '--duration', '2'])
        self.assertEqual(code, 0)
        self.assertEqual(out['hours_excluded_dark'], 0)
        w = out['best_windows'][0]
        self.assertEqual(int(w['end'][:2]) - int(w['start'][:2]), 2)

    def test_today_starts_from_now(self):
        code, out = run(['plan', *LA, '--activity', 'walk', '--now', '2026-11-16T20:30:00Z'])
        self.assertEqual(out['date'], '2026-11-16')
        self.assertEqual(out['search_window'][0], '12:00')
        self.assertTrue(all(w['start'] >= '12:00' for w in out['best_windows']))

    def test_after_dark_rolls_over_to_tomorrow(self):
        code, out = run(['plan', *LA, '--activity', 'run', '--now', '2026-11-17T03:00:00Z'])  # 19:00 LA
        self.assertEqual(out['date'], '2026-11-17')
        self.assertEqual(out['rolled_over_from'], '2026-11-16')
        self.assertTrue(out['summary'].startswith('No suitable time left today'))
        self.assertTrue(out['best_windows'])

    def test_explicit_today_does_not_roll_over(self):
        code, out = run(['plan', *LA, '--activity', 'run', '--date', '2026-11-16',
                         '--now', '2026-11-17T03:00:00Z'])
        self.assertEqual(out['best_windows'], [])
        self.assertIsNone(out['rolled_over_from'])

    def test_no_window_after_dark(self):
        code, out = run(['plan', *LA, *NOW, '--activity', 'run', '--date', '2026-11-17', '--from', '19:00'])
        self.assertEqual(out['best_windows'], [])
        self.assertIn('outside daylight', out['summary'])

    def test_plan_validation(self):
        self.assertEqual(run(['plan', *LA, *NOW, '--activity', 'run', '--date', '2026-12-30'])[0], wp.EXIT_USAGE)
        self.assertEqual(run(['plan', *LA, *NOW, '--activity', 'run', '--duration', '5'])[0], wp.EXIT_USAGE)
        self.assertEqual(run(['plan', *LA, *NOW, '--activity', 'run', '--from', '10:00', '--to', '09:00'])[0], wp.EXIT_USAGE)


class EventTests(unittest.TestCase):
    EVENTS = [{'label': 'Private: dentist with Dr. Example', 'start': '2026-11-17T12:00',
               'end': '2026-11-17T13:30', 'outdoor': True},
              {'label': 'Late call', 'start': '2026-11-18T23:00'},
              {'label': 'Next month', 'start': '2026-12-20T10:00'}]

    def test_titles_never_reach_the_provider(self):
        fetcher = FakeFetcher()
        code, out = run(['events', *LA], fetcher, json.dumps(self.EVENTS))
        self.assertEqual(code, 0)
        sent = json.dumps(fetcher.requests)
        self.assertNotIn('dentist', sent)
        self.assertNotIn('Late call', sent)
        self.assertEqual({k for k, _, _ in fetcher.requests}, {'forecast', 'airquality'})

    def test_advice_and_horizon(self):
        fetcher = FakeFetcher({'forecast': rainy_forecast(hours=[12, 13])})
        code, out = run(['events', *LA], fetcher, json.dumps(self.EVENTS))
        lunch, call, later = out['events']
        self.assertIn('consider moving indoors', lunch['advice'][0])
        self.assertEqual(lunch['max_rain_probability_pct'], 80)
        self.assertIn('umbrella', call['advice'][0])  # 69% at 23:00 in the recorded data
        self.assertTrue(later['no_forecast'])
        self.assertIn('2 with weather advice', out['summary'])

    def test_timezone_offsets_are_converted(self):
        events = [{'label': 'utc', 'start': '2026-11-17T20:00:00Z'}]  # 12:00 in Los Angeles
        code, out = run(['events', *LA], FakeFetcher(), json.dumps(events))
        self.assertEqual(out['events'][0]['start'], '2026-11-17 12:00')

    def test_events_with_own_coordinates_group_by_location(self):
        events = [{'label': 'a', 'start': '2026-11-17T09:00', 'latitude': 40.71, 'longitude': -74.0},
                  {'label': 'b', 'start': '2026-11-17T10:00', 'latitude': 40.71, 'longitude': -74.0}]
        fetcher = FakeFetcher()
        code, out = run(['events', '--no-air'], fetcher, json.dumps({'events': events}))
        self.assertEqual(code, 0)
        self.assertEqual(len(fetcher.requests), 1)

    def test_event_validation(self):
        self.assertEqual(run(['events'], FakeFetcher(), json.dumps([{'start': '2026-11-17T09:00'}]))[0], wp.EXIT_USAGE)
        self.assertEqual(run(['events', *LA], FakeFetcher(), 'not json')[0], wp.EXIT_USAGE)
        bad = [{'start': '2026-11-17T09:00', 'end': '2026-11-17T08:00'}]
        self.assertEqual(run(['events', *LA], FakeFetcher(), json.dumps(bad))[0], wp.EXIT_USAGE)
        many = [{'start': '2026-11-17T09:00'}] * (wp.MAX_EVENTS + 1)
        self.assertEqual(run(['events', *LA], FakeFetcher(), json.dumps(many))[0], wp.EXIT_USAGE)


class AirQualityTests(unittest.TestCase):
    def test_daily_maxima(self):
        code, out = run(['airquality', *LA, '--days', '3'])
        self.assertEqual(out['daily'][0], {'date': '2026-11-16', 'max_us_aqi': 82, 'max_at': '11:00',
                                           'max_pm2_5': out['daily'][0]['max_pm2_5'], 'category': 'moderate'})

    def test_categories(self):
        self.assertEqual(wp.aqi_category(40), 'good')
        self.assertEqual(wp.aqi_category(120), 'unhealthy for sensitive groups')
        self.assertEqual(wp.aqi_category(400), 'hazardous')


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetcherTests(unittest.TestCase):
    def make(self, responses, tmp, **kw):
        calls, sleeps = [], []

        def opener(request, timeout):
            calls.append(request.full_url)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return _Resp(json.dumps(item).encode())
        clock = kw.pop('clock', lambda: 1000.0)
        f = wp.Fetcher(cache_dir=tmp, opener=opener, sleep=sleeps.append, clock=clock, **kw)
        return f, calls, sleeps

    def test_retries_transient_errors_with_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, calls, sleeps = self.make([urllib.error.URLError('timeout'), TimeoutError(), {'ok': 1}], tmp)
            self.assertEqual(f.get('forecast', wp.FORECAST_API, {'a': 1}), {'ok': 1})
            self.assertEqual(len(calls), 3)
            self.assertEqual(sleeps, [1.0, 2.0])

    def test_client_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = urllib.error.HTTPError('u', 400, 'bad', {}, None)
            f, calls, _ = self.make([err, {'ok': 1}], tmp)
            with self.assertRaises(wp.PlannerError):
                f.get('forecast', wp.FORECAST_API, {'a': 1})
            self.assertEqual(len(calls), 1)

    def test_fresh_cache_skips_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, calls, _ = self.make([{'v': 1}], tmp)
            f.get('forecast', wp.FORECAST_API, {'a': 1})
            g, calls2, _ = self.make([], tmp, clock=lambda: 1000.0 + 60)
            self.assertEqual(g.get('forecast', wp.FORECAST_API, {'a': 1}), {'v': 1})
            self.assertEqual(calls2, [])
            self.assertTrue(g.notes[0]['cached'])

    def test_stale_cache_is_used_and_marked_when_provider_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, _, _ = self.make([{'v': 1}], tmp)
            f.get('forecast', wp.FORECAST_API, {'a': 1})
            down = [urllib.error.URLError('down')] * 3
            g, calls, _ = self.make(down, tmp, clock=lambda: 1000.0 + 3 * 3600)
            self.assertEqual(g.get('forecast', wp.FORECAST_API, {'a': 1}), {'v': 1})
            self.assertEqual(len(calls), 3)
            self.assertEqual(g.notes[-1], {'kind': 'forecast', 'stale': True, 'age_s': 10800})

    def test_too_old_cache_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, _, _ = self.make([{'v': 1}], tmp)
            f.get('forecast', wp.FORECAST_API, {'a': 1})
            g, _, _ = self.make([urllib.error.URLError('down')] * 3, tmp, clock=lambda: 1000.0 + 25 * 3600)
            with self.assertRaises(wp.PlannerError) as ctx:
                g.get('forecast', wp.FORECAST_API, {'a': 1})
            self.assertEqual(ctx.exception.code, wp.EXIT_UNAVAILABLE)

    def test_provider_error_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, calls, _ = self.make([{'error': True, 'reason': 'Latitude must be in range'}], tmp)
            with self.assertRaises(wp.PlannerError) as ctx:
                f.get('forecast', wp.FORECAST_API, {'a': 1})
            self.assertIn('Latitude', str(ctx.exception))
            self.assertEqual(len(calls), 1)

    def test_stale_flag_reaches_output(self):
        f = FakeFetcher()
        f.get = lambda kind, url, params: (f.notes.append({'kind': kind, 'stale': True, 'age_s': 600}),
                                           fixture(kind))[1]
        code, out = run(['forecast', *LA], f)
        self.assertTrue(out['stale'])
        self.assertEqual(out['stale_age_minutes'], 10)


class CliTests(unittest.TestCase):
    def test_end_to_end_with_fixture_flag(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'plan', *LA, '--activity', 'bike',
                                 '--date', '2026-11-17', '--fixture', str(FIXTURES), '--format', 'text'],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Best time for bike on 2026-11-17', result.stdout)
        self.assertIn('(synthetic fixture data)', result.stdout)

    def test_version(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--version'], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), wp.VERSION)


if __name__ == '__main__':
    unittest.main()
