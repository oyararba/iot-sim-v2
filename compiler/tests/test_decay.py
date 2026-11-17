"""
tests/test_decay.py

Battery DECAY on a device class:
    DECAY OVER <dist> (<params>) <unit> NOISE <factor> [CURVE LINEAR|QUADRATIC|EXPONENTIAL]

Run from compiler/:
    python -m unittest discover -s tests -v
"""

import random
import statistics
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lexer.tokenizer import Lexer
from parser.parser import Parser, ParseError
from validator.semantic import SemanticValidator
from codegen.generator import CodeGenerator
from engine.fast_simulator import Device, FastSimulator
from resources.loader import resolve, rows_from_mapping


def parse(src):
    return Parser(Lexer(src).tokenize()).parse()


def errors_of(src):
    return [e.message for e in SemanticValidator(parse(src)).validate()
            if e.severity == 'ERROR']


def spec(decay='DECAY OVER UNIFORM (low = 10, high = 10) DAYS NOISE 5.0'):
    return f"""
CREATE TABLE devices ( device_id STRING PRIMARY KEY,
                       device_class STRING REFERENCES DEVICE_CLASS, zone INTEGER )
CREATE DEVICE CLASS Probe (
    MEASURES Temperature IN celsius FOLLOWING Temp RANGE (-100.0, 200.0) RESOLUTION 0.01,
    {decay},
    zone ENUM *
)
CREATE EVENTTYPE reading ( EVENTTIME T INTEGER, device_id STRING, value FLOAT )
CREATE EVENTSTREAM s ( EVENTTYPE reading, ARRIVALS ( Temp PERIODIC (1 MIN) ) )
DISTRIBUTION FOR Temp ( NORMAL (mean = 20.0, std_dev = 1.0) PROB 1.0 )
"""


def simulate(src, n_devices=1, days=12, seed=3):
    prog = parse(src)
    errs = [e.message for e in SemanticValidator(prog).validate() if e.severity == 'ERROR']
    assert not errs, errs
    rows = {'devices': [{'device_id': f'P{i}', 'device_class': 'Probe', 'zone': 1}
                        for i in range(n_devices)]}
    r, missing, errors = rows_from_mapping(rows, prog.tables)
    res = resolve(prog, r, missing, errors, source='test')
    random.seed(seed)
    cfg = {'start_time': 0, 'end_time': days * 1440, 'time_unit': 'ticks',
           'ms_per_tick': 60_000, 'seed': seed}
    compiled = CodeGenerator(prog, res, cfg).generate()
    return compiled, FastSimulator(compiled).run()


class DecaySyntaxTests(unittest.TestCase):
    def test_full_clause(self):
        d = parse(spec('DECAY OVER NORMAL (mean = 180, std_dev = 20) DAYS NOISE 4.0 '
                       'CURVE EXPONENTIAL')).device_classes[0].decay
        self.assertEqual((d.lifetime.dist_type, d.lifetime.params['mean']), ('NORMAL', 180.0))
        self.assertEqual((d.unit_ms, d.noise, d.curve), (86_400_000, 4.0, 'EXPONENTIAL'))

    def test_curve_defaults_to_linear(self):
        self.assertEqual(parse(spec()).device_classes[0].decay.curve, 'LINEAR')

    def test_no_decay_clause(self):
        src = spec().replace('DECAY OVER UNIFORM (low = 10, high = 10) DAYS NOISE 5.0,', '')
        self.assertIsNone(parse(src).device_classes[0].decay)

    def test_noise_is_required(self):
        with self.assertRaises(ParseError):
            parse(spec('DECAY OVER UNIFORM (low = 10, high = 10) DAYS'))

    def test_unknown_curve(self):
        with self.assertRaises(ParseError):
            parse(spec('DECAY OVER UNIFORM (low = 10, high = 10) DAYS NOISE 2 CURVE WOBBLY'))


class DecayValidationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(errors_of(spec()), [])

    def test_noise_below_one(self):
        errs = errors_of(spec('DECAY OVER UNIFORM (low = 10, high = 10) DAYS NOISE 0.5'))
        self.assertTrue(any('must be >= 1' in e for e in errs), errs)

    def test_lifetime_distribution_kind(self):
        errs = errors_of(spec('DECAY OVER POISSON (mean = 10) DAYS NOISE 2'))
        self.assertTrue(any('NORMAL, UNIFORM or EXPONENTIAL' in e for e in errs), errs)

    def test_lifetime_must_be_positive(self):
        errs = errors_of(spec('DECAY OVER NORMAL (mean = -5, std_dev = 1) DAYS NOISE 2'))
        self.assertTrue(any('must be positive' in e for e in errs), errs)


class DecayEngineTests(unittest.TestCase):
    def test_spread_grows_then_device_goes_silent(self):
        compiled, events = simulate(spec())          # battery: exactly 10 days, NOISE 5
        self.assertEqual(compiled['bindings'][0]['decay']['noise'], 5.0)
        life = 10 * 1440
        self.assertLess(max(e['event_time'] for e in events), life)   # silent after
        first = [e['value'] for e in events if e['event_time'] < 1440]
        last = [e['value'] for e in events if e['event_time'] >= life - 1440]
        self.assertAlmostEqual(statistics.pstdev(first), 1.2, delta=0.3)  # ~1.0-1.4x
        self.assertGreater(statistics.pstdev(last), 4.0)                  # ~4.6-5x
        self.assertAlmostEqual(statistics.mean(last), 20.0, delta=0.5)    # centred
        self.assertAlmostEqual(events[0]['battery'], 1.0, places=3)
        self.assertLess(events[-1]['battery'], 0.01)

    def test_lifetimes_differ_per_device(self):
        _, events = simulate(spec('DECAY OVER NORMAL (mean = 5, std_dev = 1) DAYS NOISE 2'),
                             n_devices=30)
        last = {}
        for e in events:
            last[e['device_id']] = e['event_time'] / 1440
        self.assertGreater(len({round(v, 1) for v in last.values()}), 10)
        self.assertAlmostEqual(statistics.mean(last.values()), 5.0, delta=0.6)

    def test_curves(self):
        dev = object.__new__(Device)
        dev.born, dev.life, dev.noise = 0, 100.0, 9.0
        for curve, mid in (('LINEAR', 5.0), ('QUADRATIC', 3.0), ('EXPONENTIAL', 3.0)):
            dev.curve = curve
            self.assertEqual(dev.spread(0), (1.0, 1.0))
            self.assertAlmostEqual(dev.spread(50)[1], mid)
            self.assertAlmostEqual(dev.spread(100)[1], 9.0)

    def test_no_decay_no_battery_field(self):
        src = spec().replace('DECAY OVER UNIFORM (low = 10, high = 10) DAYS NOISE 5.0,', '')
        _, events = simulate(src, days=1)
        self.assertNotIn('battery', events[0])


if __name__ == '__main__':
    unittest.main()
