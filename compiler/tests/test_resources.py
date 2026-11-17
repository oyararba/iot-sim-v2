"""
tests/test_resources.py

Enterprise-DB (RESOURCES) loading and user-defined fields (`zone ENUM *`).

Run from compiler/:
    python -m unittest discover -s tests -v
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lexer.tokenizer import Lexer
from parser.parser import Parser, ParseError
from validator.semantic import SemanticValidator
from codegen.generator import CodeGenerator
from resources.loader import (ResourceError, find_field_path, load_resources,
                              resolve, rows_from_mapping)
from tools.build_resources_db import build as build_db


SIM = {'start_time': 0, 'end_time': 60, 'time_unit': 'ticks',
       'tick_unit': 'minutes', 'ms_per_tick': 60000, 'seed': 1}

TABLES = """
CREATE TABLE zones   ( zone_id INTEGER PRIMARY KEY, zone_name STRING )
CREATE TABLE plots   ( plot_id INTEGER PRIMARY KEY,
                       zone_id INTEGER REFERENCES zones(zone_id),
                       soil_type ENUM )
CREATE TABLE devices ( device_id STRING PRIMARY KEY,
                       device_class STRING REFERENCES DEVICE_CLASS,
                       plot_id INTEGER REFERENCES plots(plot_id),
                       depth_cm INTEGER )
"""

STREAM = """
CREATE EVENTTYPE metric_avg ( EVENTTIME T INTEGER, device_id STRING, avg_value FLOAT )
CREATE EVENTTYPE sensor_reading ( EVENTTIME T INTEGER, device_id STRING,
                                  measure_type ENUM, value FLOAT, depth_cm INTEGER )
CREATE EVENTSTREAM s ( EVENTTYPE sensor_reading,
    ARRIVALS ( Moisture PERIODIC (1 HOUR) FAILURE 0.0,
               Dir      PERIODIC (1 MIN)  FAILURE 0.0 ) )
"""

CLASSES = """
CREATE DEVICE_CLASS SoilSensor (
    MEASURES VolumeFraction IN percent FOLLOWING Moisture RANGE (0.0, 100.0) RESOLUTION 0.1,
    zone ENUM *
)
CREATE DEVICE_CLASS VaneSensor (
    MEASURES Angle IN degree FOLLOWING Dir RANGE (0.0, 360.0) RESOLUTION 1.0,
    zone ENUM *
)
"""

DISTS = """
DISTRIBUTION FOR Moisture (
    WHERE zone = 1 ( NORMAL (mean = 60.0, std_dev = 5.0) PROB 1.0 )
    WHERE zone = 2 ( NORMAL (mean = 30.0, std_dev = 5.0) PROB 0.9
                     BELOW ( NORMAL (mean = -2.0, std_dev = 1.0) PROB 0.1 ) )
)
DISTRIBUTION FOR Dir (
    UNIFORM (low = 0.0, high = 360.0) PROB 1.0
)
"""

ROWS = {
    'zones':   [{'zone_id': 1, 'zone_name': 'A'}, {'zone_id': 2, 'zone_name': 'B'},
                {'zone_id': 3, 'zone_name': 'C'}],
    'plots':   [{'plot_id': 10, 'zone_id': 1, 'soil_type': 'LOAM'},
                {'plot_id': 20, 'zone_id': 2, 'soil_type': 'CLAY'},
                {'plot_id': 30, 'zone_id': 3, 'soil_type': 'LOAM'}],
    'devices': [{'device_id': 'S1', 'device_class': 'SoilSensor', 'plot_id': 10, 'depth_cm': 15},
                {'device_id': 'S2', 'device_class': 'SoilSensor', 'plot_id': 20, 'depth_cm': 30},
                {'device_id': 'V3', 'device_class': 'VaneSensor', 'plot_id': 30, 'depth_cm': None}],
}


def parse(src):
    return Parser(Lexer(src).tokenize()).parse()


def spec(classes=CLASSES, dists=DISTS, tables=TABLES, stream=STREAM):
    return parse(classes + tables + stream + dists)


def errors_of(prog):
    return [e.message for e in SemanticValidator(prog).validate() if e.severity == 'ERROR']


def resources_from(prog, data):
    rows, missing, errs = rows_from_mapping(data, prog.tables)
    return resolve(prog, rows, missing, errs, source='test')


def bindings(prog, data):
    return CodeGenerator(prog, resources_from(prog, data), SIM).generate()['bindings']


class ParsingTests(unittest.TestCase):
    def test_user_field_any_case(self):
        for decl in ('zone ENUM *', 'ZONE ENUM *', 'Zone ENUM*'):
            p = parse(f'CREATE DEVICE_CLASS X ( MEASURES Count IN count FOLLOWING M RANGE (0,1) '
                      f'RESOLUTION 1, {decl} )')
            self.assertEqual(p.device_classes[0].discriminator, 'zone')

    def test_unknown_token_in_device_class_raises_instead_of_hanging(self):
        with self.assertRaises(ParseError):
            parse('CREATE DEVICE_CLASS X ( MEASURES Count IN count FOLLOWING M RANGE (0,1) '
                  'RESOLUTION 1, zone )')

    def test_where_values_and_unconditional(self):
        p = spec()
        moisture, direction = p.distributions
        self.assertEqual([(w.field, w.value) for w in moisture.where_blocks],
                         [('zone', 1), ('zone', 2)])
        self.assertTrue(direction.is_unconditional)
        self.assertEqual(moisture.where_blocks[1].tiers[1].distribution.params['mean'], -2.0)

    def test_time_units(self):
        arr = {a.measure_name: a for a in spec().event_streams[0].arrivals}
        self.assertEqual(arr['Moisture'].interval_ms, 3_600_000)
        with self.assertRaises(ParseError):
            parse('CREATE EVENTSTREAM s ( EVENTTYPE e, ARRIVALS ( '
                  'M PERIODIC (1 FORTNIGHT) FAILURE 0.0 ) )')


class ValidationTests(unittest.TestCase):
    def test_valid_spec(self):
        self.assertEqual(errors_of(spec()), [])

    def test_schema_needs_a_device_table(self):
        # Rejected by the validator alone, before any data is loaded
        tables = TABLES.replace('STRING REFERENCES DEVICE_CLASS', 'STRING')
        self.assertTrue(any('REFERENCES DEVICE_CLASS' in e
                            for e in errors_of(spec(tables=tables))))

    def test_only_one_device_table(self):
        tables = TABLES + ('CREATE TABLE spares ( spare_id STRING PRIMARY KEY, '
                           'kind STRING REFERENCES DEVICE_CLASS )')
        self.assertTrue(any('More than one column' in e
                            for e in errors_of(spec(tables=tables))))

    def test_each_class_gets_its_own_event_type(self):
        stream = STREAM.replace('EVENTTYPE sensor_reading,',
                                'EVENTTYPE Moisture_Reading, Dir_Reading,')
        stream = ('CREATE EVENTTYPE Moisture_Reading ( EVENTTIME T INTEGER, device_id STRING, value FLOAT )\n'
                  'CREATE EVENTTYPE Dir_Reading ( EVENTTIME T INTEGER, device_id STRING, value FLOAT )\n'
                  + stream)
        prog = spec(stream=stream)
        self.assertEqual(errors_of(prog), [])
        types = {b['device_id']: b['event_type'] for b in bindings(prog, ROWS)}
        self.assertEqual(types, {'S1': 'Moisture_Reading', 'S2': 'Moisture_Reading',
                                 'V3': 'Dir_Reading'})

    def test_device_table_needs_one_primary_key(self):
        tables = TABLES.replace('device_id STRING PRIMARY KEY', 'device_id STRING')
        self.assertTrue(any('exactly one PRIMARY KEY' in e
                            for e in errors_of(spec(tables=tables))))

    def test_only_one_user_field(self):
        classes = CLASSES.replace('    zone ENUM *\n)\nCREATE DEVICE_CLASS Vane',
                                  '    zone ENUM *, soil_type ENUM *\n)\nCREATE DEVICE_CLASS Vane')
        self.assertTrue(any('only one' in e for e in errors_of(spec(classes=classes))))

    def test_where_needs_declared_field(self):
        classes = CLASSES.replace('RESOLUTION 0.1,\n    zone ENUM *', 'RESOLUTION 0.1')
        self.assertTrue(any("declares no 'zone ENUM *'" in e
                            for e in errors_of(spec(classes=classes))))

    def test_duplicate_where_value(self):
        dists = DISTS.replace('WHERE zone = 2', 'WHERE zone = 1')
        self.assertTrue(any('more than once' in e for e in errors_of(spec(dists=dists))))

    def test_measure_needs_distribution_and_arrivals(self):
        dists = DISTS[:DISTS.index('DISTRIBUTION FOR Dir')]
        stream = STREAM.replace(',\n               Dir      PERIODIC (1 MIN)  FAILURE 0.0', '')
        errs = errors_of(spec(dists=dists, stream=stream))
        self.assertTrue(any('no DISTRIBUTION FOR Dir' in e for e in errs))
        self.assertTrue(any('no ARRIVALS entry' in e for e in errs))


class ResourceTests(unittest.TestCase):
    def test_zone_resolved_through_references(self):
        prog = spec()
        res = resources_from(prog, ROWS)
        self.assertEqual(res.paths['zone'].describe('devices'), 'devices.plot_id → plots.zone_id')
        self.assertEqual({d.device_id: d.fields['zone'] for d in res.devices},
                         {'S1': 1, 'S2': 2, 'V3': 3})

    def test_zone_column_on_device_table(self):
        tables = """
        CREATE TABLE devices ( device_id STRING PRIMARY KEY,
                               device_class STRING REFERENCES DEVICE_CLASS, zone INTEGER )
        """
        prog = spec(tables=tables)
        res = resources_from(prog, {'devices': [
            {'device_id': 'S1', 'device_class': 'SoilSensor', 'zone': 2}]})
        self.assertEqual(res.paths['zone'].describe('devices'), 'devices.zone')
        self.assertEqual(res.devices[0].fields, {'zone': 2})

    def test_exact_name_beats_id_form(self):
        tables = TABLES.replace('soil_type ENUM )', 'soil_type ENUM, zone INTEGER )')
        path = find_field_path('zone', spec(tables=tables).tables, 'devices')
        self.assertEqual(path.describe('devices'), 'devices.plot_id → plots.zone')

    def test_ambiguous_field(self):
        tables = TABLES.replace(
            'depth_cm INTEGER )',
            'depth_cm INTEGER, site_id INTEGER REFERENCES sites(site_id) )') + """
        CREATE TABLE sites ( site_id INTEGER PRIMARY KEY, zone_id INTEGER )
        """
        with self.assertRaises(ResourceError) as ctx:
            find_field_path('zone', spec(tables=tables).tables, 'devices')
        self.assertIn('ambiguous', str(ctx.exception))

    def test_string_valued_field(self):
        classes = CLASSES.replace('RESOLUTION 0.1,\n    zone ENUM *',
                                  'RESOLUTION 0.1,\n    soil_type ENUM *')
        dists = DISTS.replace('WHERE zone = 1', 'WHERE soil_type = LOAM') \
                     .replace('WHERE zone = 2', 'WHERE soil_type = "CLAY"')
        prog = spec(classes=classes, dists=dists)
        self.assertEqual(errors_of(prog), [])
        b = {x['device_id']: x['measures'][0]['profile'] for x in bindings(prog, ROWS)}
        self.assertEqual(b['S1'], 'SoilSensor_Moisture_soil_typeLOAM')
        self.assertEqual(b['S2'], 'SoilSensor_Moisture_soil_typeCLAY')

    def test_bindings_attrs_and_unconditional_distribution(self):
        b = {x['device_id']: x for x in bindings(spec(), ROWS)}
        self.assertEqual(b['S1']['measures'][0]['profile'], 'SoilSensor_Moisture_zone1')
        self.assertEqual(b['S2']['measures'][0]['profile'], 'SoilSensor_Moisture_zone2')
        self.assertEqual(b['V3']['measures'][0]['profile'], 'VaneSensor_Dir_all')   # zone 3, no WHERE needed
        self.assertEqual(b['S2']['attrs'], {'depth_cm': 30})

    def test_device_in_zone_without_block_is_an_error(self):
        data = json.loads(json.dumps(ROWS))
        data['devices'].append({'device_id': 'S3', 'device_class': 'SoilSensor', 'plot_id': 30})
        with self.assertRaises(ResourceError) as ctx:
            bindings(spec(), data)
        self.assertIn("'S3' (SoilSensor) has zone = 3", str(ctx.exception))
        self.assertIn('(defined: 1, 2)', str(ctx.exception))

    def test_bad_rows_are_reported_together(self):
        data = json.loads(json.dumps(ROWS))
        data['devices'] += [{'device_id': 'X1', 'device_class': 'Nope', 'plot_id': 10},
                            {'device_id': 'X2', 'device_class': 'SoilSensor', 'plot_id': 99},
                            {'device_id': 'X3', 'device_class': 'SoilSensor', 'plot_id': None}]
        with self.assertRaises(ResourceError) as ctx:
            resources_from(spec(), data)
        self.assertEqual(len(ctx.exception.errors), 3)

    def test_json_extra_keys_ignored(self):
        data = json.loads(json.dumps(ROWS))
        data['plots'][0]['plot_name'] = 'not declared'
        data['unrelated_table'] = []
        self.assertEqual(len(resources_from(spec(), data).devices), 3)

    def test_readings_tagged_with_stream_eventtype(self):
        # metric_avg is declared before sensor_reading in STREAM
        prog = spec()
        compiled = CodeGenerator(prog, resources_from(prog, ROWS), SIM).generate()
        types = {p['event_type'] for p in compiled['profiles'].values()}
        self.assertEqual(types, {'sensor_reading'})


class SQLiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.schema = self.dir / 'spec.sql'
        self.schema.write_text(CLASSES + TABLES + STREAM + DISTS)
        (self.dir / 'rows.json').write_text(json.dumps(ROWS))
        self.db = self.dir / 'res.db'
        build_db(str(self.schema), str(self.dir / 'rows.json'), str(self.db))
        self.prog = spec()

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_from_sqlite(self):
        res = load_resources(self.prog, db=str(self.db))
        self.assertEqual(res.row_counts, {'zones': 3, 'plots': 3, 'devices': 3})
        self.assertEqual({d.device_id: d.fields['zone'] for d in res.devices},
                         {'S1': 1, 'S2': 2, 'V3': 3})

    def test_extra_db_columns_ignored(self):
        con = sqlite3.connect(self.db)
        con.execute('ALTER TABLE plots ADD COLUMN irrigation_schedule TEXT')
        con.commit(); con.close()
        self.assertEqual(len(load_resources(self.prog, db=str(self.db)).devices), 3)

    def test_missing_declared_column(self):
        con = sqlite3.connect(self.db)
        con.execute('ALTER TABLE plots DROP COLUMN soil_type')
        con.commit(); con.close()
        with self.assertRaises(ResourceError) as ctx:
            load_resources(self.prog, db=str(self.db))
        self.assertIn('soil_type', str(ctx.exception))

    def test_missing_needed_table(self):
        con = sqlite3.connect(self.db)
        con.execute('PRAGMA foreign_keys = OFF')
        con.execute('DROP TABLE plots')
        con.commit(); con.close()
        with self.assertRaises(ResourceError) as ctx:
            load_resources(self.prog, db=str(self.db))
        self.assertIn('table plots is needed', str(ctx.exception))


class ExampleTests(unittest.TestCase):
    def test_example_spec_and_db(self):
        ex = ROOT / 'examples'
        prog = parse((ex / 'precision_agriculture.sql').read_text())
        self.assertEqual(errors_of(prog), [])
        res = load_resources(prog, db=str(ex / 'agriculture_resources.db'))
        compiled = CodeGenerator(prog, res, SIM).generate()
        self.assertEqual(len(compiled['bindings']), 48)


if __name__ == '__main__':
    unittest.main()
