"""
tests/test_event_count.py

Stopping by count: --num-events N (simulation.max_events in profiles.json).

    count only       : no time limit, exactly N events, the first N of the stream
    count + sim-end  : whichever comes first
    neither          : the 90-day default (compiler.py)

Run from compiler/:
    python -m unittest discover -s tests -v
"""

import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lexer.tokenizer import Lexer
from parser.parser import Parser
from validator.semantic import SemanticValidator
from codegen.generator import CodeGenerator
from engine.fast_simulator import FastSimulator
from resources.loader import resolve, rows_from_mapping


# One probe class, readings every minute, and a tumbling rule: the rule's
# window timer re-arms forever, which is what a count-only run must not chase
# once the devices have stopped.
def spec(failure=0.05, decay=''):
    return f"""
CREATE TABLE devices ( device_id STRING PRIMARY KEY,
                       device_class STRING REFERENCES DEVICE_CLASS, zone INTEGER )
CREATE DEVICE CLASS Probe (
    MEASURES Temperature IN celsius FOLLOWING Temp RANGE (-100.0, 200.0) RESOLUTION 0.01,
    {decay}
    zone ENUM *
)
CREATE EVENTTYPE reading ( EVENTTIME T INTEGER, device_id STRING, value FLOAT )
CREATE EVENTTYPE hot     ( EVENTTIME T INTEGER, device_id STRING, value FLOAT )
CREATE EVENTSTREAM s ( EVENTTYPE reading,
                       ARRIVALS ( Temp PERIODIC (1 MIN) FAILURE {failure} ) )
DISTRIBUTION FOR Temp ( NORMAL (mean = 20.0, std_dev = 5.0) PROB 1.0 )

new hot[tumbling(s, 10 MIN)](device_id, (value = max(value))) @ (s + 10 MIN) :-
    reading(device_id, value) @ t,
    value > 25;
"""


def simulate(src, end=None, max_events=None, n_devices=3, seed=7, stall_limit=None):
    prog = Parser(Lexer(src).tokenize()).parse()
    errs = [e.message for e in SemanticValidator(prog).validate() if e.severity == 'ERROR']
    assert not errs, errs
    rows = {'devices': [{'device_id': f'P{i}', 'device_class': 'Probe', 'zone': 1}
                        for i in range(n_devices)]}
    r, missing, errors = rows_from_mapping(rows, prog.tables)
    res = resolve(prog, r, missing, errors, source='test')
    random.seed(seed)
    cfg = {'start_time': 0, 'end_time': end, 'max_events': max_events,
           'time_unit': 'ticks', 'ms_per_tick': 60_000, 'seed': seed}
    sim = FastSimulator(CodeGenerator(prog, res, cfg).generate())
    if stall_limit is not None:
        sim.stall_limit = stall_limit
    return sim, sim.run()


class CountOnlyTests(unittest.TestCase):
    def test_exactly_n_events_and_they_are_the_first_n_of_the_stream(self):
        timed_sim, timed = simulate(spec(), end=3 * 1440)
        self.assertGreater(len(timed), 5000)
        for n in (1, 777, 5000):
            sim, events = simulate(spec(), max_events=n)
            self.assertEqual(len(events), n)
            self.assertEqual(sim.stop_reason, 'count')
            self.assertEqual(events, timed[:n])          # same seed: a prefix of the timed run

    def test_rule_events_count_toward_n(self):
        _, events = simulate(spec(), max_events=3000)
        kinds = {e['event_type'] for e in events}
        self.assertEqual(kinds, {'reading', 'hot'})
        self.assertEqual(len(events), 3000)

    def test_output_stays_in_event_time_order(self):
        _, events = simulate(spec(), max_events=4000)
        times = [e['event_time'] for e in events]
        self.assertEqual(times, sorted(times))

    def test_stops_when_every_device_has_stopped(self):
        # Batteries last exactly one day; the tumbling timer would re-arm forever.
        decay = 'DECAY OVER UNIFORM (low = 1, high = 1) DAYS NOISE 2.0,'
        sim, events = simulate(spec(decay=decay), max_events=10 ** 9)
        self.assertEqual(sim.stop_reason, 'stopped')
        self.assertLessEqual(max(e['event_time'] for e in events), 1440)
        # Nothing lost at the end: the last window's rule events are still written
        _, timed = simulate(spec(decay=decay), end=3 * 1440)
        self.assertEqual(events, timed)

    def test_gives_up_when_no_event_can_ever_come(self):
        sim, events = simulate(spec(failure=1.0), max_events=100, stall_limit=5000)
        self.assertEqual(events, [])
        self.assertEqual(sim.stop_reason, 'stalled')

    def test_needs_a_time_or_a_count(self):
        with self.assertRaises(ValueError):
            simulate(spec(), end=None, max_events=None)


class CountAndTimeTests(unittest.TestCase):
    def test_time_limit_first(self):
        sim, events = simulate(spec(), end=60, max_events=10 ** 6)
        self.assertEqual(sim.stop_reason, 'end')
        self.assertLess(len(events), 10 ** 6)
        self.assertLessEqual(max(e['event_time'] for e in events), 60)

    def test_count_first(self):
        sim, events = simulate(spec(), end=3 * 1440, max_events=500)
        self.assertEqual((sim.stop_reason, len(events)), ('count', 500))

    def test_time_only_runs_are_unchanged(self):
        sim, events = simulate(spec(), end=1440)
        _, again = simulate(spec(), end=1440, max_events=None)
        self.assertEqual(events, again)
        self.assertIsNone(sim._max_events)


class CliTests(unittest.TestCase):
    def test_num_events_writes_n_lines(self):
        ex = ROOT / 'examples'
        with tempfile.TemporaryDirectory() as out:
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'compiler.py'),
                 '--schema', str(ex / 'precision_agriculture.sql'),
                 '--devices', str(ex / 'agriculture_resources.json'),
                 '--num-events', '2500', '--seed', '1', '--output', out],
                capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            lines = (Path(out) / 'events.jsonl').read_text().splitlines()
            self.assertEqual(len(lines), 2500)
            sim = json.loads((Path(out) / 'profiles.json').read_text())['simulation']
            self.assertEqual((sim['end_time'], sim['max_events']), (None, 2500))
            self.assertIn('Stopped at 2,500 events', proc.stdout)

    def test_num_events_must_be_positive(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / 'compiler.py'),
             '--schema', str(ROOT / 'examples' / 'precision_agriculture.sql'),
             '--num-events', '0'],
            capture_output=True, text=True, cwd=ROOT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('--num-events must be at least 1', proc.stderr)


if __name__ == '__main__':
    unittest.main()
