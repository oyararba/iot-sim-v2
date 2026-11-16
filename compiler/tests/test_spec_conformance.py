"""
tests/test_spec_conformance.py

The behaviour the README and the MS defense slides specify, one test per
point (see SPEC_CONFORMANCE.md).

Run from compiler/:
    python -m unittest discover -s tests -v
"""

import json
import math
import random
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lexer.tokenizer import Lexer
from parser.parser import Parser, ParseError
from validator.semantic import SemanticValidator
from codegen.generator import CodeGenerator
from engine.fast_simulator import FastSimulator
from resources.loader import resolve, rows_from_mapping

FIXTURES = ROOT / 'tests' / 'fixtures'


def parse(src):
    return Parser(Lexer(src).tokenize()).parse()


def errors_of(prog):
    return [e.message for e in SemanticValidator(prog).validate() if e.severity == 'ERROR']


def simulate(src, rows, days=1.0, seed=1, ms_per_tick=60_000, **sim):
    """Parse, validate, bind, compile and run; returns the emitted events."""
    prog = parse(src)
    errs = errors_of(prog)
    assert not errs, errs
    r, missing, errors = rows_from_mapping(rows, prog.tables)
    res = resolve(prog, r, missing, errors, source='test')
    random.seed(seed)
    cfg = {'start_time': 0, 'end_time': int(days * 86_400_000 / ms_per_tick),
           'time_unit': 'ticks', 'ms_per_tick': ms_per_tick, 'seed': seed, **sim}
    compiled = CodeGenerator(prog, res, cfg).generate()
    return FastSimulator(compiled).run()


# A small deployment used by the engine tests -------------------------------

TABLES = """
CREATE TABLE zones   ( zone_id INTEGER PRIMARY KEY )
CREATE TABLE plots   ( plot_id INTEGER PRIMARY KEY, zone_id INTEGER REFERENCES zones(zone_id) )
CREATE TABLE devices ( device_id STRING PRIMARY KEY,
                       device_class STRING REFERENCES DEVICE_CLASS,
                       plot_id INTEGER REFERENCES plots(plot_id),
                       depth_cm INTEGER )
"""

EVENTS = """
CREATE EVENTTYPE sensor_reading (
    EVENTTIME T INTEGER,
    device_id: devices(device_id) FK,
    measure_type ENUM,
    value FLOAT, value2 FLOAT, value3 FLOAT,
    depth_cm INTEGER
)
CREATE EVENTTYPE alert  ( EVENTTIME T INTEGER, device_id STRING, alert_type ENUM, value FLOAT )
CREATE EVENTTYPE report ( EVENTTIME T INTEGER, device_id STRING, report_type ENUM )
CREATE EVENTTYPE metric_avg ( EVENTTIME T INTEGER, device_id STRING, measure_type ENUM,
                              avg_value FLOAT )
CREATE EVENTTYPE stats ( EVENTTIME T INTEGER, device_id STRING, lo FLOAT, hi FLOAT, n INTEGER )
"""

CLASSES = """
CREATE DEVICE CLASS SoilMoistureSensor (
    MEASURES Moisture UNIT percent_VWC RANGE (0.0, 100.0) RESOLUTION 0.1,
    zone ENUM *
)
CREATE DEVICE CLASS AirTemperatureSensor (
    MEASURES AirTemp UNIT celsius RANGE (-20.0, 60.0) RESOLUTION 0.1,
    zone ENUM *
)
CREATE DEVICE CLASS NPKSensor (
    MEASURES Nitrogen   UNIT mg_per_kg RANGE (0.0, 999.0) RESOLUTION 1.0,
    MEASURES Phosphorus UNIT mg_per_kg RANGE (0.0, 999.0) RESOLUTION 1.0,
    MEASURES Potassium  UNIT mg_per_kg RANGE (0.0, 999.0) RESOLUTION 1.0,
    zone ENUM *
)
"""

ROWS = {
    'zones':   [{'zone_id': 1}, {'zone_id': 2}],
    'plots':   [{'plot_id': 1, 'zone_id': 1}, {'plot_id': 2, 'zone_id': 2}],
    'devices': [
        {'device_id': 'SM1', 'device_class': 'SoilMoistureSensor', 'plot_id': 1, 'depth_cm': 15},
        {'device_id': 'SM2', 'device_class': 'SoilMoistureSensor', 'plot_id': 2, 'depth_cm': 30},
        {'device_id': 'AT1', 'device_class': 'AirTemperatureSensor', 'plot_id': 1},
        {'device_id': 'NPK1', 'device_class': 'NPKSensor', 'plot_id': 1, 'depth_cm': 20},
        {'device_id': 'NPK2', 'device_class': 'NPKSensor', 'plot_id': 2, 'depth_cm': 20},
    ],
}


def deployment(stream=None, dists=None, rules=''):
    stream = stream or """
CREATE EVENTSTREAM s ( EVENTTYPE sensor_reading,
    ARRIVALS ( Moisture   PERIODIC (10 MIN),
               AirTemp    PERIODIC (10 MIN),
               Nitrogen   PERIODIC (1 DAY),
               Phosphorus PERIODIC (1 DAY),
               Potassium  PERIODIC (1 DAY) ) )
"""
    dists = dists or """
DISTRIBUTION FOR Moisture (
    WHERE zone = 1 ( NORMAL (mean = 60.0, std_dev = 0.0) PROB 1.0 )
    WHERE zone = 2 ( NORMAL (mean = 10.0, std_dev = 0.0) PROB 1.0 )
)
DISTRIBUTION FOR AirTemp ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Nitrogen (
    WHERE zone = 1 ( NORMAL (mean = 50.0, std_dev = 0.0) PROB 1.0 )
    WHERE zone = 2 ( NORMAL (mean = 50.0, std_dev = 0.0) PROB 1.0 )
)
DISTRIBUTION FOR Phosphorus (
    WHERE zone = 1 ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
    WHERE zone = 2 ( NORMAL (mean = 90.0, std_dev = 0.0) PROB 1.0 )
)
DISTRIBUTION FOR Potassium ( NORMAL (mean = 160.0, std_dev = 0.0) PROB 1.0 )
"""
    return CLASSES + TABLES + EVENTS + stream + dists + rules


def triggered(events, label=None):
    return [e for e in events if 'triggered_by' in e
            and (label is None or e['triggered_by'] == label)]


# ---------------------------------------------------------------------------
# Language: everything the README and slides write must parse and validate
# ---------------------------------------------------------------------------

class SpecSheetSyntaxTests(unittest.TestCase):
    def setUp(self):
        self.prog = parse((FIXTURES / 'spec_sheet_snippets.sql').read_text())

    def test_everything_parses_and_validates(self):
        self.assertEqual(errors_of(self.prog), [])
        self.assertEqual(len(self.prog.device_classes), 5)
        self.assertEqual(len(self.prog.rules), 8)

    def test_device_class_forms_and_units(self):
        units = {m.name: m.unit for dc in self.prog.device_classes for m in dc.measures}
        self.assertEqual(units['Moisture'], 'percent_VWC')      # quoted in the README
        self.assertEqual(units['WindSpeed'], 'm_per_s')         # bare on the slides

    def test_eventtype_fk_and_ingesttime(self):
        reading = self.prog.event_types[0]
        self.assertEqual(reading.attributes[0].references, ('Device', 'device_id'))
        self.assertEqual(reading.ingest_attr, 'I')
        self.assertEqual(reading.max_delay_ms, 5 * 60_000)
        metric = [e for e in self.prog.event_types if e.name == 'metric_avg'][0]
        self.assertEqual(metric.attributes[0].references, ('Device', 'device_id'))  # no colon

    def test_arrivals_failure_optional(self):
        arr = {a.measure_name: a for a in self.prog.event_streams[0].arrivals}
        self.assertEqual(arr['WindDirection'].failure_prob, 0.0)
        self.assertEqual(arr['Nitrogen'].interval_ms, 86_400_000)      # `1 DAY`

    def test_distribution_failure_sticky_and_tier_order(self):
        air = self.prog.distributions[0]
        self.assertEqual(air.failure_lifetime_ms, 360 * 86_400_000)
        self.assertTrue(all(t.sticky for t in air.where_blocks[0].tiers))
        moisture = [d for d in self.prog.distributions if d.measure_name == 'Moisture'][0]
        self.assertEqual([t.tier for t in moisture.where_blocks[0].tiers], ['BELOW', 'NORMAL'])

    def test_failure_probability_inside_distribution_is_explained(self):
        with self.assertRaises(ParseError) as ctx:
            parse('DISTRIBUTION FOR M ( WHERE zone = 1 ( NORMAL (mean = 1.0, std_dev = 1.0) '
                  'PROB 1.0 ) FAILURE 0.04 )')
        self.assertIn('ARRIVALS', str(ctx.exception))

    def test_unknown_time_unit_is_an_error(self):
        with self.assertRaises(ParseError):
            parse('new a[tumbling(s, 3 WEEKS)](d) @ (s) :- e(d) @ z;')

    def test_spec_sheet_example_runs(self):
        rows = json.loads((FIXTURES / 'spec_sheet_rows.json').read_text())
        events = simulate((FIXTURES / 'spec_sheet_snippets.sql').read_text(), rows, days=3)
        labels = Counter(e['triggered_by'] for e in triggered(events))
        for label in ('metric_avg(Moisture)', 'alert(FROST_RISK)', 'alert(STORM)',
                      'alert(NITROGEN_DEFICIENCY)', 'report(NPK_CRITICAL)'):
            self.assertIn(label, labels)


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

class ReadingTests(unittest.TestCase):
    def test_event_shape_follows_eventtype(self):
        ev = next(e for e in simulate(deployment(), ROWS, days=0.1) if e['device_id'] == 'SM2')
        self.assertEqual(list(ev), ['event_type', 'event_time', 'device_id', 'measure_type',
                                    'value', 'depth_cm', 'tier'])
        self.assertEqual((ev['measure_type'], ev['value'], ev['depth_cm']), ('Moisture', 10.0, 30))

    def test_multi_measure_device_sends_one_packed_reading(self):
        events = [e for e in simulate(deployment(), ROWS, days=3) if e['device_id'] == 'NPK2']
        self.assertEqual(len(events), 4)                       # t = 0, 1, 2, 3 days
        e = events[0]
        self.assertEqual((e['measure_type'], e['value'], e['value2'], e['value3']),
                         ('Nitrogen', 50.0, 90.0, 160.0))
        self.assertEqual((e['tier'], e['tier2'], e['tier3']), ('NORMAL',) * 3)

    def test_resolution_and_range(self):
        src = deployment(dists="""
DISTRIBUTION FOR Moisture ( NORMAL (mean = 50.0, std_dev = 80.0) PROB 1.0 )
DISTRIBUTION FOR AirTemp ( NORMAL (mean = 20.0, std_dev = 3.0) PROB 1.0 )
DISTRIBUTION FOR Nitrogen ( POISSON (mean = 40.0) PROB 1.0 )
DISTRIBUTION FOR Phosphorus ( UNIFORM (low = 10.0, high = 10.0) PROB 1.0 )
DISTRIBUTION FOR Potassium ( EXPONENTIAL (mean = 100.0) PROB 1.0 )
""")
        values = [e['value'] for e in simulate(src, ROWS, days=5)
                  if e.get('measure_type') == 'Moisture']
        self.assertTrue(all(0.0 <= v <= 100.0 for v in values))
        self.assertTrue(all(abs(v * 10 - round(v * 10)) < 1e-9 for v in values))
        self.assertIn(0.0, values)                               # clamped at RANGE

    def test_failure_probability_drops_readings(self):
        stream = """
CREATE EVENTSTREAM s ( EVENTTYPE sensor_reading,
    ARRIVALS ( Moisture PERIODIC (1 MIN) FAILURE 0.25, AirTemp PERIODIC (1 DAY),
               Nitrogen PERIODIC (1 DAY), Phosphorus PERIODIC (1 DAY),
               Potassium PERIODIC (1 DAY) ) )
"""
        n = sum(1 for e in simulate(deployment(stream=stream), ROWS, days=5)
                if e.get('measure_type') == 'Moisture')
        expected = 2 * (5 * 1440 + 1)
        self.assertAlmostEqual(n / expected, 0.75, delta=0.02)


class StickyTests(unittest.TestCase):
    SRC = deployment(dists="""
DISTRIBUTION FOR Moisture (
    NORMAL (mean = 50.0, std_dev = 1.0) PROB 0.8 STICKY
    ABOVE ( NORMAL (mean = 90.0, std_dev = 1.0) PROB 0.1 STICKY )
    BELOW ( NORMAL (mean = 10.0, std_dev = 1.0) PROB 0.1 )
)
DISTRIBUTION FOR AirTemp ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Nitrogen ( NORMAL (mean = 50.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Phosphorus ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Potassium ( NORMAL (mean = 160.0, std_dev = 0.0) PROB 1.0 )
""", stream="""
CREATE EVENTSTREAM s ( EVENTTYPE sensor_reading,
    ARRIVALS ( Moisture PERIODIC (1 MIN), AirTemp PERIODIC (1 DAY),
               Nitrogen PERIODIC (1 DAY), Phosphorus PERIODIC (1 DAY),
               Potassium PERIODIC (1 DAY) ) )
""")

    def setUp(self):
        events = simulate(self.SRC, ROWS, days=40, seed=5)
        self.seq = [e for e in events if e['device_id'] == 'SM1']

    def test_declared_rates_hold(self):
        counts = Counter(e['tier'] for e in self.seq)
        n = len(self.seq)
        self.assertAlmostEqual(counts['NORMAL'] / n, 0.8, delta=0.02)
        self.assertAlmostEqual(counts['ABOVE'] / n, 0.1, delta=0.015)
        self.assertAlmostEqual(counts['BELOW'] / n, 0.1, delta=0.015)

    def test_tiers_persist(self):
        switches = sum(1 for a, b in zip(self.seq, self.seq[1:]) if a['tier'] != b['tier'])
        rate = switches / (len(self.seq) - 1)
        # chain: stay 0.9 / 0.9 / 0; redraw ∝ p(1 − stay) = (0.08, 0.01, 0.1)/0.19
        stay, p = [0.9, 0.9, 0.0], [0.8, 0.1, 0.1]
        r = [x * (1 - s) for x, s in zip(p, stay)]
        r = [x / sum(r) for x in r]
        expected = sum(pi * (1 - si) * (1 - ri) for pi, si, ri in zip(p, stay, r))
        self.assertAlmostEqual(rate, expected, delta=0.01)
        self.assertLess(rate, 1 - sum(x * x for x in p) - 0.15)   # far fewer than independent

    def test_mean_moves_gradually_into_a_sticky_tier(self):
        firsts = [b['value'] for a, b in zip(self.seq, self.seq[1:])
                  if a['tier'] == 'NORMAL' and b['tier'] == 'ABOVE']
        self.assertTrue(firsts)
        self.assertTrue(55 < sum(firsts) / len(firsts) < 85)      # between 50 and 90

    def test_leaving_a_sticky_tier_is_gradual_too(self):
        # README: transitions "into or out of" a STICKY tier are gradual, so
        # NORMAL (sticky) -> BELOW (not sticky) still ramps down from ~50
        intos = [b['value'] for a, b in zip(self.seq, self.seq[1:])
                 if a['tier'] == 'NORMAL' and b['tier'] == 'BELOW']
        self.assertTrue(intos)
        self.assertTrue(15 < sum(intos) / len(intos) < 45)

    def test_without_sticky_switches_are_instant(self):
        events = simulate(self.SRC.replace(' STICKY', ''), ROWS, days=10, seed=5)
        seq = [e for e in events if e['device_id'] == 'SM1']
        firsts = [b['value'] for a, b in zip(seq, seq[1:])
                  if a['tier'] == 'NORMAL' and b['tier'] == 'ABOVE']
        self.assertTrue(firsts)
        self.assertLess(abs(sum(firsts) / len(firsts) - 90.0), 0.5)
        switches = sum(1 for a, b in zip(seq, seq[1:]) if a['tier'] != b['tier'])
        self.assertAlmostEqual(switches / (len(seq) - 1), 1 - (0.64 + 0.01 + 0.01), delta=0.02)


class FailureLifetimeTests(unittest.TestCase):
    def test_sensors_fail_and_devices_stop(self):
        src = deployment(dists="""
DISTRIBUTION FOR Moisture (
    FAILURE 2 HOURS
    NORMAL (mean = 50.0, std_dev = 1.0) PROB 1.0
)
DISTRIBUTION FOR AirTemp ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Nitrogen ( NORMAL (mean = 50.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Phosphorus ( FAILURE 1 HOUR NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Potassium ( NORMAL (mean = 160.0, std_dev = 0.0) PROB 1.0 )
""")
        events = simulate(src, ROWS, days=2)
        last = {}
        for e in events:
            last[e['device_id']] = e['event_time']
        self.assertLess(max(last['SM1'], last['SM2']), 2 * 1440)     # moisture sensors died
        self.assertEqual(last['AT1'], 2 * 1440)                      # no lifetime: still running
        npk = [e for e in events if e['device_id'] == 'NPK1']
        self.assertEqual(len(npk), 3)                                # N and K keep the device alive
        self.assertNotIn('value2', npk[-1])                          # phosphorus sensor failed


class IngestTimeTests(unittest.TestCase):
    def test_delay_and_stale_flag(self):
        src = deployment().replace(
            'EVENTTIME T INTEGER,\n    device_id: devices(device_id) FK,',
            'EVENTTIME T INTEGER,\n    INGESTTIME I MAXDELAY 5 MIN,\n'
            '    device_id: devices(device_id) FK,')
        events = [e for e in simulate(src, ROWS, days=20) if e['event_type'] == 'sensor_reading']
        self.assertTrue(all(e['ingest_time'] >= e['event_time'] for e in events))
        stale = sum(e['stale'] for e in events) / len(events)
        self.assertAlmostEqual(stale, math.exp(-2), delta=0.015)
        self.assertTrue(all(e['stale'] == (e['ingest_time'] - e['event_time'] > 5)
                            for e in events if abs(e['ingest_time'] - e['event_time'] - 5) > 1e-3))


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

class RuleTests(unittest.TestCase):
    def run_rules(self, rules, dists=None, days=1.0, stream=None):
        return simulate(deployment(rules=rules, dists=dists, stream=stream), ROWS, days=days)

    def test_tumbling_window_start_fire_time_and_aggregates(self):
        events = self.run_rules("""
new alert[tumbling(s, 30 MIN)](device_id, DROUGHT_STRESS, (value = avg(value))) @ (s + 31 MIN) :-
    sensor_reading(device_id, MOISTURE, value) @ z,
    value < 25;
""", days=0.25)
        alerts = triggered(events)
        self.assertTrue(alerts)
        self.assertEqual({a['device_id'] for a in alerts}, {'SM2'})       # 10 < 25; SM1 is 60
        self.assertEqual([a['event_time'] for a in alerts][:3], [31, 61, 91])
        self.assertEqual(alerts[0]['alert_type'], 'DROUGHT_STRESS')
        self.assertEqual(alerts[0]['value'], 10.0)

    def test_rules_only_see_their_measure(self):
        events = self.run_rules("""
new alert[tumbling(s, 1 HOUR)](device_id, HEATWAVE, (value = avg(value))) @ (s + 1 HOUR) :-
    sensor_reading(device_id, AIR_TEMP, value) @ z, value > 15;
""")
        self.assertEqual({a['device_id'] for a in triggered(events)}, {'AT1'})

    def test_rules_with_the_same_head_fire_independently(self):
        events = self.run_rules("""
new alert[tumbling(s, 30 MIN)](device_id, DRY, (value = avg(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, Moisture, value) @ z, value < 25;
new alert[tumbling(s, 30 MIN)](device_id, WET, (value = avg(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, Moisture, value) @ z, value > 50;
""", days=0.5)
        kinds = Counter((a['alert_type'], a['device_id']) for a in triggered(events))
        self.assertEqual(set(kinds), {('DRY', 'SM2'), ('WET', 'SM1')})
        self.assertEqual(kinds[('DRY', 'SM2')], kinds[('WET', 'SM1')])

    def test_constraints_filter_readings_before_aggregation(self):
        events = self.run_rules("""
new alert[tumbling(s, 1 HOUR)](device_id, LOW, (value = avg(value))) @ (s + 1 HOUR) :-
    sensor_reading(device_id, MOISTURE, value) @ z, value < 30;
""", dists="""
DISTRIBUTION FOR Moisture ( UNIFORM (low = 0.0, high = 60.0) PROB 1.0 )
DISTRIBUTION FOR AirTemp ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Nitrogen ( NORMAL (mean = 50.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Phosphorus ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Potassium ( NORMAL (mean = 160.0, std_dev = 0.0) PROB 1.0 )
""", days=3)
        values = [a['value'] for a in triggered(events)]
        self.assertTrue(values)
        self.assertTrue(all(v < 30 for v in values))

    def test_sliding_count_window(self):
        events = self.run_rules("""
new metric_avg[sliding(s, 3)](device_id, Moisture, (avg_value = count(value))) @ (s + 7) :-
    sensor_reading(device_id, MOISTURE, value) @ z;
""", days=0.1)
        m = [e for e in triggered(events) if e['device_id'] == 'SM1']
        self.assertEqual(m[0]['event_time'], 20)              # 3rd reading (t = 0, 10, 20)
        self.assertEqual({e['avg_value'] for e in m}, {3})
        self.assertEqual(m[0]['measure_type'], 'Moisture')
        # s + 7 = 7 is before the 3rd reading: never emitted into the past
        self.assertTrue(all(e['event_time'] >= 20 for e in m))

    def test_sliding_time_window(self):
        events = self.run_rules("""
new metric_avg[sliding(s, 30 MIN)](device_id, Moisture, (avg_value = count(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, MOISTURE, value) @ z;
""", days=0.1)
        m = [e['avg_value'] for e in triggered(events) if e['device_id'] == 'SM1']
        self.assertEqual(m[:5], [1, 2, 3, 3, 3])

    def test_rule_without_window_fires_per_event(self):
        events = self.run_rules("""
new alert(device_id, FROST_RISK) @ (T + 1) :-
    sensor_reading(device_id, MOISTURE, value, _) @ T,
    value < 25;
""", days=0.05)
        alerts = triggered(events)
        self.assertEqual([a['event_time'] for a in alerts][:3], [1, 11, 21])
        self.assertEqual(alerts[0], {'event_type': 'alert', 'event_time': 1, 'device_id': 'SM2',
                                     'alert_type': 'FROST_RISK', 'value': 10.0,
                                     'triggered_by': 'alert(FROST_RISK)'})

    def test_join_and_chaining(self):
        events = self.run_rules("""
new alert[tumbling(s, 1 DAY)](device_id, NITROGEN_DEFICIENCY, (value = avg(value))) @ (s + 1 DAY) :-
    sensor_reading(device_id, NITROGEN, value, _, _) @ z, value < 80;
new alert[tumbling(s, 1 DAY)](device_id, PHOSPHORUS_DEFICIENCY, (value = avg(value2))) @ (s + 1 DAY) :-
    sensor_reading(device_id, NITROGEN, _, value2, _) @ z, value2 < 40;
new report[tumbling(s, 1 DAY)](device_id, NPK_CRITICAL) @ (s + 1 DAY) :-
    alert(device_id, NITROGEN_DEFICIENCY, _) @ z,
    alert(device_id, PHOSPHORUS_DEFICIENCY, _) @ z;
""", days=4)
        alerts = Counter((a['alert_type'], a['device_id']) for a in triggered(events)
                         if a['event_type'] == 'alert')
        self.assertIn(('NITROGEN_DEFICIENCY', 'NPK2'), alerts)
        self.assertNotIn(('PHOSPHORUS_DEFICIENCY', 'NPK2'), alerts)       # P = 90 there
        reports = triggered(events, 'report(NPK_CRITICAL)')
        self.assertTrue(reports)
        self.assertEqual({r['device_id'] for r in reports}, {'NPK1'})

    def test_several_aggregations(self):
        events = self.run_rules("""
new stats[tumbling(s, 1 HOUR)](device_id, (lo = min(value), hi = max(value), n = count(value))) @ (s + 1 HOUR) :-
    sensor_reading(device_id, MOISTURE, value) @ z;
""", dists="""
DISTRIBUTION FOR Moisture ( UNIFORM (low = 0.0, high = 60.0) PROB 1.0 )
DISTRIBUTION FOR AirTemp ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Nitrogen ( NORMAL (mean = 50.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Phosphorus ( NORMAL (mean = 20.0, std_dev = 0.0) PROB 1.0 )
DISTRIBUTION FOR Potassium ( NORMAL (mean = 160.0, std_dev = 0.0) PROB 1.0 )
""", days=0.25)
        rows = triggered(events)
        self.assertTrue(rows)
        self.assertTrue(all(r['n'] == 6 and r['lo'] <= r['hi'] for r in rows))

    def test_comparison_operators(self):
        for cond, devices in (('value <= 10', {'SM2'}), ('value >= 60', {'SM1'}),
                              ('value != 60', {'SM2'}), ('value == 60', {'SM1'}),
                              ('value = 10', {'SM2'})):
            events = self.run_rules(f"""
new alert(device_id, LOW) @ (T) :- sensor_reading(device_id, MOISTURE, value) @ T, {cond};
""", days=0.02)
            self.assertEqual({a['device_id'] for a in triggered(events)}, devices, cond)

    def test_constants_are_case_and_underscore_insensitive(self):
        for spelling in ('MOISTURE', 'Moisture', 'moisture'):
            events = self.run_rules(f"""
new alert(device_id, LOW) @ (T) :- sensor_reading(device_id, {spelling}, value) @ T, value < 25;
""", days=0.02)
            self.assertTrue(triggered(events), spelling)

    def test_rule_errors(self):
        bad = {
            'unknown measure':  "new alert[tumbling(s, 1 HOUR)](device_id, X, (value = avg(value))) "
                                "@ (s) :- sensor_reading(device_id, MOISTUR, value) @ z;",
            'agg needs window': "new alert(device_id, X, (value = avg(value))) @ (T) :- "
                                "sensor_reading(device_id, MOISTURE, value) @ T;",
            'join needs tumbling': "new report(device_id, X) @ (z) :- alert(device_id, A, _) @ z, "
                                   "alert(device_id, B, _) @ z;",
            'too many args':    "new alert(device_id, X) @ (T) :- "
                                "sensor_reading(device_id, MOISTURE, a, b, c, d, e) @ T;",
            'unbound head var': "new alert(foo, X) @ (T) :- sensor_reading(device_id, MOISTURE) @ T;",
        }
        for name, rule in bad.items():
            self.assertTrue(errors_of(parse(deployment(rules=rule))), name)


if __name__ == '__main__':
    unittest.main()
