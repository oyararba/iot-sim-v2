"""
ontology — the measurement ontology every MEASURES clause must use.

measurements.sql (next to this file) holds two tables, `quantity` and `unit`.
They are loaded once into an in-memory SQLite database. The validator calls
resolve() for every MEASURES clause; the generator calls it again to put the
quantity, unit and SI conversion into profiles.json.

    MEASURES Temperature IN celsius FOLLOWING AirTemp
             └ quantity   └ unit      └ distribution (DISTRIBUTION FOR AirTemp)

Names match case- and underscore-insensitively (relative_humidity =
RelativeHumidity); a unit may also be written as its symbol ("°C", "mg/kg").
"""

from __future__ import annotations

import difflib
import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'measurements.sql')


def _key(name: str) -> str:
    return str(name).replace('_', '').casefold()


@dataclass(frozen=True)
class Quantity:
    name:         str
    dimension:    str
    si_unit:      str
    default_unit: str
    min_si:       Optional[float]
    max_si:       Optional[float]


@dataclass(frozen=True)
class Unit:
    quantity: str
    name:     str
    symbol:   str
    scale:    float
    offset:   float

    def to_si(self, value: float) -> float:
        return value * self.scale + self.offset

    def from_si(self, value: float) -> float:
        return (value - self.offset) / self.scale


class OntologyError(ValueError):
    pass


class Ontology:
    def __init__(self, sql_file: str = SQL_FILE):
        db = sqlite3.connect(':memory:')
        with open(sql_file, encoding='utf-8') as f:
            db.executescript(f.read())
        self.db = db
        self.quantities: Dict[str, Quantity] = {}
        for row in db.execute('SELECT name, dimension, si_unit, default_unit, min_si, max_si '
                              'FROM quantity ORDER BY rowid'):
            q = Quantity(*row)
            self.quantities[_key(q.name)] = q
        self.units: Dict[str, List[Unit]] = {}
        for row in db.execute('SELECT quantity, name, symbol, scale, offset FROM unit '
                              'ORDER BY rowid'):
            u = Unit(*row)
            self.units.setdefault(_key(u.quantity), []).append(u)

    # ------------------------------------------------------------------
    def quantity(self, name: str) -> Quantity:
        q = self.quantities.get(_key(name))
        if q is None:
            names = [q.name for q in self.quantities.values()]
            close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
            hint = f" Did you mean {', '.join(close)}?" if close else ''
            raise OntologyError(
                f"'{name}' is not a quantity in the measurement ontology.{hint} "
                f"Known quantities: {', '.join(names)}")
        return q

    def unit(self, quantity: str, unit: Optional[str] = None) -> Unit:
        q = self.quantity(quantity)
        units = self.units[_key(q.name)]
        wanted = q.default_unit if unit is None else unit
        for u in units:
            if _key(u.name) == _key(wanted) or u.symbol == wanted:
                return u
        accepted = ', '.join(u.name for u in units)
        raise OntologyError(f"'{unit}' is not a unit of {q.name} (use one of: {accepted})")

    def resolve(self, quantity: str, unit: Optional[str] = None) -> Tuple[Quantity, Unit]:
        q = self.quantity(quantity)
        return q, self.unit(q.name, unit)

    def check_range(self, quantity: str, unit: Optional[str],
                    lo: float, hi: float) -> Optional[str]:
        """Why RANGE (lo, hi) is physically impossible for the quantity, or None."""
        q, u = self.resolve(quantity, unit)
        lo_si, hi_si = u.to_si(lo), u.to_si(hi)
        eps = 1e-9 * max(1.0, abs(lo_si), abs(hi_si))
        if q.min_si is not None and lo_si < q.min_si - eps:
            return (f"RANGE starts at {lo:g} {u.symbol}, below the physical minimum of "
                    f"{q.name} ({u.from_si(q.min_si):g} {u.symbol})")
        if q.max_si is not None and hi_si > q.max_si + eps:
            return (f"RANGE ends at {hi:g} {u.symbol}, above the physical maximum of "
                    f"{q.name} ({u.from_si(q.max_si):g} {u.symbol})")
        return None

    def describe(self) -> str:
        """The ontology as a table (compiler.py --list-quantities)."""
        lines = [f"{'Quantity':<24}{'Dim':<15}{'SI unit':<10}Units (write after IN)"]
        for q in self.quantities.values():
            units = ', '.join(u.name for u in self.units[_key(q.name)])
            lines.append(f'{q.name:<24}{q.dimension:<15}{q.si_unit:<10}{units}')
        return '\n'.join(lines)


_DEFAULT: Optional[Ontology] = None


def default_ontology() -> Ontology:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Ontology()
    return _DEFAULT
