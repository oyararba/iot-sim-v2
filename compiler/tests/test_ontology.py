"""
tests/test_ontology.py

MEASURES must use the measurement ontology (ontology/measurements.sql):
    MEASURES <quantity> [IN <unit>] [FOLLOWING <distribution>]

Run from compiler/:
    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lexer.tokenizer import Lexer
from parser.parser import Parser, ParseError
from validator.semantic import SemanticValidator
from codegen.generator import CodeGenerator
from resources.loader import resolve, rows_from_mapping
from ontology import OntologyError, default_ontology


def parse(src):
    return Parser(Lexer(src).tokenize()).parse()


def errors_of(src):
    return [e.message for e in SemanticValidator(parse(src)).validate()
            if e.severity == 'ERROR']


TAIL = """
CREATE TABLE devices ( device_id STRING PRIMARY KEY,
                       device_class STRING REFERENCES DEVICE_CLASS, zone INTEGER )
CREATE EVENTTYPE reading ( EVENTTIME T INTEGER, device_id STRING, value FLOAT )
CREATE EVENTSTREAM s ( EVENTTYPE reading, ARRIVALS ( {m} PERIODIC (1 MIN) ) )
DISTRIBUTION FOR {m} ( NORMAL (mean = 20.0, std_dev = 1.0) PROB 1.0 )
"""


def spec(measures, follows='AirTemp'):
    return f'CREATE DEVICE CLASS T ( {measures}, zone ENUM * )' + TAIL.format(m=follows)


class OntologyTableTests(unittest.TestCase):
    def setUp(self):
        self.onto = default_ontology()

    def test_si_base_quantities_present(self):
        for q in ('Length', 'Mass', 'Duration', 'ElectricCurrent', 'Temperature',
                  'AmountOfSubstance', 'LuminousIntensity'):
            self.assertIn(q.casefold(), self.onto.quantities)

    def test_every_quantity_has_its_default_unit(self):
        for q in self.onto.quantities.values():
            self.assertEqual(self.onto.unit(q.name).name, q.default_unit)

    def test_conversion_to_si(self):
        _, c = self.onto.resolve('Temperature', 'celsius')
        _, f = self.onto.resolve('Temperature', 'fahrenheit')
        self.assertAlmostEqual(c.to_si(0.0), 273.15)
        self.assertAlmostEqual(f.to_si(32.0), 273.15)
        _, ppm = self.onto.resolve('AmountFraction', 'ppm')
        self.assertAlmostEqual(ppm.to_si(400.0), 4e-4)

    def test_names_and_symbols(self):
        self.assertEqual(self.onto.resolve('relative_humidity', 'PERCENT')[1].name, 'percent')
        self.assertEqual(self.onto.resolve('Temperature', '°C')[1].name, 'celsius')

    def test_unknown_quantity_suggests(self):
        with self.assertRaises(OntologyError) as cm:
            self.onto.quantity('Temprature')
        self.assertIn('Did you mean Temperature', str(cm.exception))


class MeasuresSyntaxTests(unittest.TestCase):
    def test_full_form(self):
        m = parse(spec('MEASURES Temperature IN celsius FOLLOWING AirTemp '
                       'RANGE (-20.0, 60.0) RESOLUTION 0.1')).device_classes[0].measures[0]
        self.assertEqual((m.quantity, m.unit, m.name), ('Temperature', 'celsius', 'AirTemp'))

    def test_defaults(self):
        src = spec('MEASURES Temperature RANGE (250.0, 320.0) RESOLUTION 0.1', 'Temperature')
        m = parse(src).device_classes[0].measures[0]
        self.assertEqual((m.unit, m.name), (None, 'Temperature'))    # kelvin; follows itself
        self.assertEqual(errors_of(src), [])

    def test_quoted_symbol(self):
        src = spec('MEASURES Temperature IN "°C" FOLLOWING AirTemp RANGE (-20.0, 60.0) '
                   'RESOLUTION 0.1')
        self.assertEqual(errors_of(src), [])

    def test_old_unit_form_is_rejected_with_a_hint(self):
        with self.assertRaises(ParseError) as cm:
            parse(spec('MEASURES AirTemp UNIT celsius RANGE (-20.0, 60.0) RESOLUTION 0.1'))
        self.assertIn('FOLLOWING AirTemp', str(cm.exception))


class MeasuresValidationTests(unittest.TestCase):
    def test_unknown_quantity(self):
        errs = errors_of(spec('MEASURES AirTemp IN celsius RANGE (-20.0, 60.0) RESOLUTION 0.1'))
        self.assertTrue(any('not a quantity' in e for e in errs), errs)

    def test_unit_of_another_quantity(self):
        errs = errors_of(spec('MEASURES Temperature IN ppm FOLLOWING AirTemp '
                              'RANGE (0.0, 60.0) RESOLUTION 0.1'))
        self.assertTrue(any("'ppm' is not a unit of Temperature" in e for e in errs), errs)

    def test_range_below_absolute_zero(self):
        errs = errors_of(spec('MEASURES Temperature IN celsius FOLLOWING AirTemp '
                              'RANGE (-300.0, 60.0) RESOLUTION 0.1'))
        self.assertTrue(any('physical minimum' in e for e in errs), errs)

    def test_fraction_above_100_percent(self):
        errs = errors_of(spec('MEASURES RelativeHumidity IN percent FOLLOWING AirTemp '
                              'RANGE (0.0, 120.0) RESOLUTION 0.5'))
        self.assertTrue(any('physical maximum' in e for e in errs), errs)

    def test_one_distribution_one_unit(self):
        src = (spec('MEASURES Temperature IN celsius FOLLOWING AirTemp '
                    'RANGE (-20.0, 60.0) RESOLUTION 0.1') +
               'CREATE DEVICE CLASS U ( MEASURES Temperature IN fahrenheit FOLLOWING AirTemp '
               'RANGE (0.0, 140.0) RESOLUTION 0.1, zone ENUM * )')
        errs = errors_of(src)
        self.assertTrue(any('one quantity and one unit' in e for e in errs), errs)


class ProfileTests(unittest.TestCase):
    def test_profile_carries_quantity_and_si_conversion(self):
        prog = parse(spec('MEASURES Temperature IN celsius FOLLOWING AirTemp '
                          'RANGE (-20.0, 60.0) RESOLUTION 0.1'))
        self.assertEqual(errors_of(spec('MEASURES Temperature IN celsius FOLLOWING AirTemp '
                                        'RANGE (-20.0, 60.0) RESOLUTION 0.1')), [])
        rows, missing, errors = rows_from_mapping(
            {'devices': [{'device_id': 'D1', 'device_class': 'T', 'zone': 1}]}, prog.tables)
        res = resolve(prog, rows, missing, errors, source='test')
        cfg = {'start_time': 0, 'end_time': 10, 'time_unit': 'ticks', 'ms_per_tick': 60_000}
        profile = CodeGenerator(prog, res, cfg).generate()['profiles']['T_AirTemp_all']
        self.assertEqual(profile['quantity'], 'Temperature')
        self.assertEqual(profile['unit'], 'celsius')
        self.assertEqual(profile['si_unit'], 'K')
        self.assertEqual(profile['to_si'], {'scale': 1.0, 'offset': 273.15})


if __name__ == '__main__':
    unittest.main()
