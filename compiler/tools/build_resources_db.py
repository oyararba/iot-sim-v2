"""
tools/build_resources_db.py

Build the enterprise DB (a SQLite file) that `compiler.py --db` reads.

The tables are created from the schema's own CREATE TABLE blocks, so the
database always has exactly the columns the spec declares, and the rows are
read from a JSON file keyed by table name:

    { "zones":   [ {"zone_id": 1, "zone_name": "North Field", ...}, ... ],
      "plots":   [ ... ],
      "devices": [ ... ] }

Usage:
    python tools/build_resources_db.py \
        --schema examples/precision_agriculture.sql \
        --json   examples/agriculture_resources.json \
        --db     examples/agriculture_resources.db

Type mapping: INTEGER → INTEGER, FLOAT → REAL, STRING / ENUM → TEXT.
PRIMARY KEY and REFERENCES t(c) become SQL constraints; REFERENCES
DEVICE_CLASS has no SQL counterpart and is left as a plain column (the
compiler checks it against the declared device classes).
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lexer.tokenizer import Lexer
from parser.parser import Parser
from resources.loader import DEVICE_CLASS_REF, rows_from_mapping

SQL_TYPES = {'INTEGER': 'INTEGER', 'FLOAT': 'REAL', 'STRING': 'TEXT', 'ENUM': 'TEXT'}


def create_table_sql(tbl) -> str:
    cols, constraints = [], []
    for c in tbl.columns:
        line = f'"{c.name}" {SQL_TYPES.get(c.dtype.upper(), "TEXT")}'
        if c.primary_key:
            line += ' PRIMARY KEY'
        cols.append(line)
        if c.references and c.references[0] != DEVICE_CLASS_REF:
            rt, rc = c.references
            constraints.append(f'FOREIGN KEY ("{c.name}") REFERENCES "{rt}"("{rc}")')
    body = ',\n    '.join(cols + constraints)
    return f'CREATE TABLE "{tbl.name}" (\n    {body}\n)'


def table_order(tables):
    """Referenced tables first, so foreign keys point at existing rows."""
    by_name, done, order = {t.name: t for t in tables}, set(), []

    def visit(name, stack=()):
        if name in done or name not in by_name or name in stack:
            return
        for c in by_name[name].columns:
            if c.references and c.references[0] != DEVICE_CLASS_REF:
                visit(c.references[0], stack + (name,))
        done.add(name)
        order.append(by_name[name])

    for t in tables:
        visit(t.name)
    return order


def build(schema_path: str, json_path: str, db_path: str) -> dict:
    program = Parser(Lexer(Path(schema_path).read_text()).tokenize()).parse()
    if not program.tables:
        raise SystemExit(f'{schema_path}: no CREATE TABLE blocks')

    data = json.loads(Path(json_path).read_text())
    rows, missing, errors = rows_from_mapping(data, program.tables, source=Path(json_path).name)
    if errors:
        raise SystemExit('\n'.join(errors))

    db = Path(db_path)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute('PRAGMA foreign_keys = ON')
    counts = {}
    with con:
        for tbl in table_order(program.tables):
            con.execute(create_table_sql(tbl))
            names = [c.name for c in tbl.columns]
            marks = ', '.join('?' for _ in names)
            cols  = ', '.join(f'"{n}"' for n in names)
            table_rows = rows.get(tbl.name, [])
            con.executemany(f'INSERT INTO "{tbl.name}" ({cols}) VALUES ({marks})',
                            [[r.get(n) for n in names] for r in table_rows])
            counts[tbl.name] = len(table_rows)
    con.close()
    return {'tables': counts, 'missing': missing}


def main():
    ap = argparse.ArgumentParser(description='Build a SQLite enterprise DB from a schema + JSON rows')
    ap.add_argument('--schema', required=True, help='Schema file with the CREATE TABLE blocks')
    ap.add_argument('--json',   required=True, help='Rows keyed by table name')
    ap.add_argument('--db',     required=True, help='SQLite file to write (overwritten)')
    args = ap.parse_args()
    result = build(args.schema, args.json, args.db)
    print(f'Wrote {args.db}')
    for name, n in result['tables'].items():
        print(f'  {name:<10} {n} rows')
    for name in result['missing']:
        print(f'  WARN: no rows for declared table {name}')


if __name__ == '__main__':
    main()
