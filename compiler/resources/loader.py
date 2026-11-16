"""
resources/loader.py

RESOURCES: the enterprise database that says where devices are deployed.

The schema's CREATE TABLE blocks describe the enterprise tables. This module
loads their rows from a SQLite database (--db) or a JSON file (--devices),
reading only the columns each CREATE TABLE declares (extra columns in the
database are ignored), and resolves for every device:

  * its id            the PRIMARY KEY of the device table
  * its device class  the column declared `REFERENCES DEVICE_CLASS`
                      (the table that has this column is the device table)
  * its user-defined  e.g. `zone ENUM *` on the device class. The value is
    field value       read from the nearest column named `zone` or `zone_id`,
                      found by following REFERENCES out of the device table:

                          devices.plot_id → plots.plot_id   (row lookup)
                          plots.zone_id                     (value)

Every problem is collected and reported together; nothing is defaulted or
dropped silently.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ast_nodes.nodes import Program, TableNode


DEVICE_CLASS_REF = 'DEVICE_CLASS'


class ResourceError(Exception):
    """One or more problems with the resource data. `errors` lists them all."""

    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__('\n'.join(self.errors))


def value_key(value: Any) -> str:
    """
    Normalised form used to compare a DB value with a WHERE value, so that
    1, 1.0 and "1" all match `WHERE zone = 1`.
    """
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else repr(float(value))
    text = str(value).strip()
    try:
        num = float(text)
    except ValueError:
        return text
    return str(int(num)) if num.is_integer() else repr(num)


# -----------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------

@dataclass
class Hop:
    """Follow `from_table.fk_column` to the row of `to_table` whose `to_column` matches."""
    from_table: str
    fk_column:  str
    to_table:   str
    to_column:  str


@dataclass
class FieldPath:
    """Where a user-defined field's value comes from, starting at the device table."""
    field:  str
    hops:   List[Hop]
    table:  str
    column: str

    def describe(self, device_table: str) -> str:
        if not self.hops:
            return f'{device_table}.{self.column}'
        parts = [f'{h.from_table}.{h.fk_column}' for h in self.hops]
        return ' → '.join(parts + [f'{self.table}.{self.column}'])


@dataclass
class DeviceInstance:
    device_id:    Any
    device_class: str
    row:          Dict[str, Any]                       # declared columns of the device table
    fields:       Dict[str, Any] = field(default_factory=dict)   # e.g. {'zone': 1}


@dataclass
class Resources:
    source:       str
    device_table: str
    id_column:    str
    class_column: str
    devices:      List[DeviceInstance]
    paths:        Dict[str, FieldPath]
    row_counts:   Dict[str, int]
    warnings:     List[str] = field(default_factory=list)


# -----------------------------------------------------------------------
# Loading rows
# -----------------------------------------------------------------------

def load_sqlite(db_path: str, tables: List[TableNode]
                ) -> Tuple[Dict[str, List[dict]], List[str], List[str]]:
    """
    Read every declared table from a SQLite file.
    Returns (rows by table name, tables not found, errors).
    """
    path = Path(db_path)
    if not path.is_file():
        raise ResourceError([f'--db {db_path}: file not found'])

    try:
        con = sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True)
        existing = {name.lower(): name for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    except sqlite3.DatabaseError as exc:
        raise ResourceError([f'--db {db_path}: not a readable SQLite database ({exc})'])

    rows: Dict[str, List[dict]] = {}
    missing_tables: List[str] = []
    errors: List[str] = []

    try:
        for tbl in tables:
            actual = existing.get(tbl.name.lower())
            if actual is None:
                missing_tables.append(tbl.name)
                continue
            db_cols = {r[1].lower(): r[1]
                       for r in con.execute(f'PRAGMA table_info("{actual}")')}
            absent = [c.name for c in tbl.columns if c.name.lower() not in db_cols]
            if absent:
                errors.append(f'table {tbl.name}: column(s) {", ".join(absent)} are '
                              f'declared in CREATE TABLE but missing from {path.name}')
                continue
            select = ', '.join(f'"{db_cols[c.name.lower()]}"' for c in tbl.columns)
            names  = [c.name for c in tbl.columns]
            rows[tbl.name] = [dict(zip(names, r))
                              for r in con.execute(f'SELECT {select} FROM "{actual}"')]
    finally:
        con.close()

    return rows, missing_tables, errors


def rows_from_mapping(data: Any, tables: List[TableNode], source: str = 'JSON'
                      ) -> Tuple[Dict[str, List[dict]], List[str], List[str]]:
    """
    Read declared tables from a mapping {table_name: [ {column: value}, ... ]}.
    Keys that no CREATE TABLE declares are ignored; declared columns that a
    row leaves out are read as NULL.
    """
    if not isinstance(data, dict):
        raise ResourceError([f'{source}: expected an object keyed by table name'])

    by_lower = {str(k).lower(): v for k, v in data.items()}
    rows: Dict[str, List[dict]] = {}
    missing_tables: List[str] = []
    errors: List[str] = []

    for tbl in tables:
        items = by_lower.get(tbl.name.lower())
        if items is None:
            missing_tables.append(tbl.name)
            continue
        if not isinstance(items, list):
            errors.append(f'{source}: "{tbl.name}" must be a list of rows')
            continue
        table_rows = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f'{source}: {tbl.name}[{i}] is not an object')
                continue
            lower_item = {str(k).lower(): v for k, v in item.items()}
            table_rows.append({c.name: lower_item.get(c.name.lower())
                               for c in tbl.columns})
        rows[tbl.name] = table_rows

    return rows, missing_tables, errors


def load_json(json_path: str, tables: List[TableNode]):
    path = Path(json_path)
    if not path.is_file():
        raise ResourceError([f'--devices {json_path}: file not found'])
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ResourceError([f'--devices {json_path}: invalid JSON ({exc})'])
    return rows_from_mapping(data, tables, source=path.name)


# -----------------------------------------------------------------------
# Resolving devices
# -----------------------------------------------------------------------

def _find_device_table(tables: List[TableNode]) -> Tuple[TableNode, str, str]:
    owners = [(t, c) for t in tables for c in t.columns
              if c.references and c.references[0] == DEVICE_CLASS_REF]
    if not owners:
        raise ResourceError([
            'No CREATE TABLE has a column declared `REFERENCES DEVICE_CLASS`, so '
            'there is no way to tell which table lists the devices.'])
    if len(owners) > 1:
        names = ', '.join(f'{t.name}.{c.name}' for t, c in owners)
        raise ResourceError([
            f'More than one column is declared `REFERENCES DEVICE_CLASS` ({names}); '
            f'exactly one table may list the devices.'])
    table, class_col = owners[0]
    pks = [c.name for c in table.columns if c.primary_key]
    if len(pks) != 1:
        raise ResourceError([
            f'Device table {table.name} needs exactly one PRIMARY KEY column '
            f'(the device id); found {len(pks)}.'])
    return table, pks[0], class_col.name


def find_field_path(field_name: str, tables: List[TableNode],
                    device_table: str) -> FieldPath:
    """
    Breadth-first search out of the device table along REFERENCES for the
    nearest column named `<field>` or `<field>_id`. An exact name wins over
    the `_id` form at the same distance; two candidates at the same distance
    are ambiguous.
    """
    by_name = {t.name: t for t in tables}
    exact, with_id = field_name.lower(), f'{field_name.lower()}_id'

    frontier: List[Tuple[str, List[Hop]]] = [(device_table, [])]
    visited = {device_table}

    while frontier:
        exact_hits, id_hits = [], []
        for tname, hops in frontier:
            for col in by_name[tname].columns:
                if col.name.lower() == exact:
                    exact_hits.append((tname, col.name, hops))
                elif col.name.lower() == with_id:
                    id_hits.append((tname, col.name, hops))
        hits = exact_hits or id_hits
        if len(hits) == 1:
            tname, cname, hops = hits[0]
            return FieldPath(field=field_name, hops=hops, table=tname, column=cname)
        if len(hits) > 1:
            where = ', '.join(f'{t}.{c}' for t, c, _ in hits)
            raise ResourceError([
                f'User-defined field `{field_name}` is ambiguous: {where} are equally '
                f'close to {device_table}. Rename one of the columns.'])

        next_frontier = []
        for tname, hops in frontier:
            for col in by_name[tname].columns:
                if not col.references or col.references[0] == DEVICE_CLASS_REF:
                    continue
                ref_table, ref_col = col.references
                if ref_table in by_name and ref_table not in visited:
                    visited.add(ref_table)
                    next_frontier.append(
                        (ref_table, hops + [Hop(tname, col.name, ref_table, ref_col)]))
        frontier = next_frontier

    raise ResourceError([
        f'User-defined field `{field_name}`: no column named `{exact}` or '
        f'`{with_id}` can be reached from table {device_table} through REFERENCES.'])


def resolve(program: Program, rows: Dict[str, List[dict]],
            missing_tables: List[str], load_errors: List[str],
            source: str) -> Resources:
    errors   = list(load_errors)
    warnings: List[str] = []

    device_tbl, id_col, class_col = _find_device_table(program.tables)
    classes = {dc.name: dc for dc in program.device_classes}

    # Schema-level: one path per user-defined field in use
    paths: Dict[str, FieldPath] = {}
    for dc in program.device_classes:
        fname = dc.discriminator
        if fname and fname not in paths:
            try:
                paths[fname] = find_field_path(fname, program.tables, device_tbl.name)
            except ResourceError as exc:
                errors.extend(exc.errors)

    required = {device_tbl.name}
    for p in paths.values():
        required.update(h.to_table for h in p.hops)
    for t in missing_tables:
        if t in required:
            errors.append(f'{source}: table {t} is needed but was not found')
        else:
            warnings.append(f'{source}: table {t} is declared but not present (not needed)')
    if errors:
        raise ResourceError(errors)

    # Row indexes for each hop target
    index: Dict[Tuple[str, str], Dict[str, dict]] = {}
    for p in paths.values():
        for h in p.hops:
            key = (h.to_table, h.to_column)
            if key in index:
                continue
            idx: Dict[str, dict] = {}
            for r in rows.get(h.to_table, []):
                v = r.get(h.to_column)
                if v is None:
                    continue
                k = value_key(v)
                if k in idx:
                    errors.append(f'{h.to_table}.{h.to_column} = {v!r} appears on more '
                                  f'than one row, so {h.from_table}.{h.fk_column} '
                                  f'cannot pick one')
                idx[k] = r
            index[key] = idx

    devices: List[DeviceInstance] = []
    seen_ids = set()
    for r in rows.get(device_tbl.name, []):
        did, cls = r.get(id_col), r.get(class_col)
        label = f'{device_tbl.name} row {did!r}'
        if did is None:
            errors.append(f'{device_tbl.name}: a row has NULL {id_col}')
            continue
        if value_key(did) in seen_ids:
            errors.append(f'{label}: duplicate {id_col}')
            continue
        seen_ids.add(value_key(did))
        if cls not in classes:
            errors.append(f'{label}: {class_col} = {cls!r} is not a declared DEVICE_CLASS')
            continue

        dev = DeviceInstance(device_id=did, device_class=cls, row=dict(r))
        fname = classes[cls].discriminator
        if fname and fname in paths:
            path, cur, ok = paths[fname], r, True
            for h in path.hops:
                fk = cur.get(h.fk_column)
                if fk is None:
                    errors.append(f'{label}: {h.from_table}.{h.fk_column} is NULL, so '
                                  f'`{fname}` cannot be resolved')
                    ok = False
                    break
                nxt = index[(h.to_table, h.to_column)].get(value_key(fk))
                if nxt is None:
                    errors.append(f'{label}: {h.from_table}.{h.fk_column} = {fk!r} has no '
                                  f'matching row in {h.to_table}.{h.to_column}')
                    ok = False
                    break
                cur = nxt
            if ok:
                val = cur.get(path.column)
                if val is None:
                    errors.append(f'{label}: `{fname}` resolves to NULL '
                                  f'({path.describe(device_tbl.name)})')
                else:
                    dev.fields[fname] = val
        devices.append(dev)

    if not devices and not errors:
        errors.append(f'{source}: table {device_tbl.name} has no rows')
    if errors:
        raise ResourceError(errors)

    return Resources(
        source=source, device_table=device_tbl.name, id_column=id_col,
        class_column=class_col, devices=devices, paths=paths,
        row_counts={t: len(v) for t, v in rows.items()}, warnings=warnings,
    )


def load_resources(program: Program, db: Optional[str] = None,
                   json_path: Optional[str] = None) -> Resources:
    """Entry point: load from exactly one of --db / --devices and resolve."""
    if bool(db) == bool(json_path):
        raise ResourceError(['Give exactly one of --db (SQLite) or --devices (JSON).'])
    if db:
        rows, missing, errs = load_sqlite(db, program.tables)
        source = Path(db).name
    else:
        rows, missing, errs = load_json(json_path, program.tables)
        source = Path(json_path).name
    return resolve(program, rows, missing, errs, source)
