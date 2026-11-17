"""
validator/semantic.py

Semantic validation pass over the AST.
Checks:
  0. Measurement ontology — every MEASURES names a quantity and a unit from
     ontology/measurements.sql, its RANGE is physically possible for that
     quantity, and measures that follow the same DISTRIBUTION agree on
     quantity and unit
  1. Device class integrity — each MEASURES references a valid event type
  2. Distribution integrity — measure_name exists in a device class,
     every measure has a DISTRIBUTION, WHERE <field> only on a field every
     device class measuring it declares as `<field> ENUM *`, no duplicate
     WHERE values, probabilities sum to 1.0 per block
  3. Event stream integrity — measure names exist, every measure has an
     ARRIVALS entry, failure 0.0-1.0
  User-defined fields — at most one `<field> ENUM *` per device class
  4. Event type integrity — no duplicate attribute names
  5. Table integrity — REFERENCES point to existing tables and columns, and
     exactly one table is the device table: one column REFERENCES
     DEVICE_CLASS, and that table has exactly one PRIMARY KEY
  6. Rule integrity — source event types exist, aggregation vars referenced
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Set

from ast_nodes.nodes import Program
from ontology import OntologyError, default_ontology
from resources.loader import ResourceError, find_device_table, value_key
from codegen.rules import MEASURE_ATTRS, compile_rule


@dataclass
class ValidationError:
    message:  str
    severity: str   # ERROR | WARN
    line:     int   = 0


class SemanticValidator:
    def __init__(self, program: Program):
        self.prog   = program
        self.errors: List[ValidationError] = []

        # Indexes built during validation
        self._device_classes: Dict[str, set] = {}   # name → set of measure names
        self._device_zone:    Set[str]        = set()  # device classes with ZONE ENUM *
        self._event_types:    Set[str]        = set()
        self._tables:         Dict[str, set]  = {}   # table → set of columns
        self._all_measures:   Set[str]        = set()

    def validate(self) -> List[ValidationError]:
        self._index_device_classes()
        self._validate_measures()
        self._validate_decay()
        self._index_event_types()
        self._index_tables()
        self._validate_distributions()
        self._validate_event_streams()
        self._validate_event_types()
        self._validate_tables()
        self._validate_rules()
        return self.errors

    @property
    def has_errors(self) -> bool:
        return any(e.severity == 'ERROR' for e in self.errors)

    def _err(self, msg: str, line: int = 0):
        self.errors.append(ValidationError(message=msg, severity='ERROR', line=line))

    def _warn(self, msg: str, line: int = 0):
        self.errors.append(ValidationError(message=msg, severity='WARN', line=line))

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _index_device_classes(self):
        for dc in self.prog.device_classes:
            if dc.name in self._device_classes:
                self._err(f"Duplicate DEVICE_CLASS name '{dc.name}'")
            measures = {m.name for m in dc.measures}
            self._device_classes[dc.name] = measures
            self._all_measures |= measures
            if dc.zone_enum:
                self._device_zone.add(dc.name)
            if len(dc.user_fields) > 1:
                self._err(f"DEVICE_CLASS '{dc.name}' declares {len(dc.user_fields)} "
                          f"user-defined fields ({', '.join(dc.user_fields)}); "
                          f"only one '<field> ENUM *' is allowed", dc.line)

    def _validate_measures(self):
        """Every MEASURES uses the measurement ontology (ontology/measurements.sql)."""
        onto = default_ontology()
        follows: Dict[str, tuple] = {}     # distribution name -> (quantity, unit, class)
        for dc in self.prog.device_classes:
            for m in dc.measures:
                where = f"DEVICE_CLASS '{dc.name}' MEASURES {m.quantity}"
                try:
                    q, u = onto.resolve(m.quantity, m.unit)
                except OntologyError as e:
                    self._err(f'{where}: {e}', m.line or dc.line)
                    continue
                if m.range_min > m.range_max:
                    self._err(f'{where}: RANGE ({m.range_min:g}, {m.range_max:g}) has '
                              f'min > max', m.line or dc.line)
                else:
                    why = onto.check_range(q.name, u.name, m.range_min, m.range_max)
                    if why:
                        self._err(f'{where}: {why}', m.line or dc.line)
                if m.resolution <= 0:
                    self._err(f'{where}: RESOLUTION must be > 0', m.line or dc.line)
                prev = follows.get(m.name)
                if prev and (prev[0], prev[1]) != (q.name, u.name):
                    self._err(f"{where}: follows DISTRIBUTION {m.name} in {u.name}, but "
                              f"DEVICE_CLASS '{prev[2]}' follows it as {prev[0]} in "
                              f"{prev[1]}; a distribution's parameters have one quantity "
                              f"and one unit", m.line or dc.line)
                follows.setdefault(m.name, (q.name, u.name, dc.name))

    _DECAY_PARAMS = {'NORMAL': ('mean', 'std_dev'), 'UNIFORM': ('low', 'high'),
                     'EXPONENTIAL': ('mean',)}

    def _validate_decay(self):
        """DECAY: a battery life distribution that gives positive lifetimes,
        and a NOISE factor of at least 1 (the spread only grows)."""
        for dc in self.prog.device_classes:
            d = dc.decay
            if d is None:
                continue
            where = f"DEVICE_CLASS '{dc.name}' DECAY"
            spec = d.lifetime
            need = self._DECAY_PARAMS.get(spec.dist_type)
            if need is None:
                self._err(f"{where}: battery life can follow NORMAL, UNIFORM or "
                          f"EXPONENTIAL, not {spec.dist_type}", d.line or dc.line)
            else:
                missing = [k for k in need if k not in spec.params]
                if missing:
                    self._err(f"{where}: {spec.dist_type} needs {', '.join(need)}",
                              d.line or dc.line)
                else:
                    p = spec.params
                    bad = (spec.dist_type in ('NORMAL', 'EXPONENTIAL') and p['mean'] <= 0
                           or spec.dist_type == 'NORMAL' and p['std_dev'] < 0
                           or spec.dist_type == 'UNIFORM'
                           and not (0 <= p['low'] <= p['high'] and p['high'] > 0))
                    if bad:
                        self._err(f"{where}: battery life must be positive "
                                  f"({spec.dist_type} {p})", d.line or dc.line)
            if d.noise < 1:
                self._err(f"{where}: NOISE {d.noise:g} must be >= 1 (the spread of "
                          f"readings grows as the battery drains)", d.line or dc.line)

    def _index_event_types(self):
        for et in self.prog.event_types:
            if et.name in self._event_types:
                self._err(f"Duplicate EVENTTYPE name '{et.name}'")
            self._event_types.add(et.name)

            # Check no duplicate attributes
            seen = set()
            for attr in et.attributes:
                if attr.name in seen:
                    self._err(f"Duplicate attribute '{attr.name}' in EVENTTYPE '{et.name}'")
                seen.add(attr.name)

    def _index_tables(self):
        for tbl in self.prog.tables:
            cols = {c.name for c in tbl.columns}
            self._tables[tbl.name] = cols

    # ------------------------------------------------------------------
    # Distribution validation
    # ------------------------------------------------------------------

    def _validate_distributions(self):
        declared_measures = set()

        for dist in self.prog.distributions:
            mn = dist.measure_name

            # Measure must exist in some device class
            if mn not in self._all_measures:
                self._err(f"DISTRIBUTION FOR '{mn}': no DEVICE_CLASS declares "
                          f"a measure FOLLOWING {mn}")

            # Check for duplicate distributions for same measure
            if mn in declared_measures:
                self._warn(f"Multiple DISTRIBUTION FOR '{mn}' — last one wins")
            declared_measures.add(mn)

            self._validate_where_fields(dist)
            if dist.failure_lifetime_ms is not None and dist.failure_lifetime_ms <= 0:
                self._err(f"DISTRIBUTION FOR '{mn}': FAILURE lifetime must be > 0", dist.line)

            # Validate each WHERE block
            for wb in dist.where_blocks:
                label = (f"DISTRIBUTION FOR '{mn}'" if wb.is_default else
                         f"DISTRIBUTION FOR '{mn}' WHERE {wb.field}={wb.value}")
                # Probabilities must sum to 1.0
                total = sum(t.probability for t in wb.tiers)
                if abs(total - 1.0) > 0.001:
                    self._err(
                        f"{label}: probabilities sum to {total:.4f}, must be 1.0",
                        wb.line)

                # Must have exactly one NORMAL tier
                normals = [t for t in wb.tiers if t.tier == 'NORMAL']
                if len(normals) != 1:
                    self._err(
                        f"{label}: must have exactly one NORMAL tier, found {len(normals)}",
                        wb.line)

                # Validate distribution parameters
                for tier in wb.tiers:
                    self._validate_dist_spec(tier.distribution, mn, wb.zone_id)

        # Every measure a device can produce needs a distribution
        for dc in self.prog.device_classes:
            for m in dc.measures:
                if m.name not in declared_measures:
                    self._err(f"DEVICE_CLASS '{dc.name}' measure {m.name} has no "
                              f"DISTRIBUTION FOR {m.name}", dc.line)

    def _validate_where_fields(self, dist):
        """WHERE <field> must be the user-defined field of every class measuring it."""
        mn = dist.measure_name
        fields = {wb.field for wb in dist.where_blocks if wb.field}
        if len(fields) > 1:
            self._err(f"DISTRIBUTION FOR '{mn}' mixes WHERE fields "
                      f"({', '.join(sorted(fields))}); use one field", dist.line)
            return
        if not fields:
            return
        fname = next(iter(fields))

        seen = {}
        for wb in dist.where_blocks:
            key = value_key(wb.value)
            if key in seen:
                self._err(f"DISTRIBUTION FOR '{mn}': WHERE {fname} = {wb.value} "
                          f"appears more than once", wb.line)
            seen[key] = wb

        for dc in self.prog.device_classes:
            if mn not in {m.name for m in dc.measures}:
                continue
            if dc.discriminator is None:
                self._err(f"DISTRIBUTION FOR '{mn}' uses WHERE {fname}, but DEVICE_CLASS "
                          f"'{dc.name}' declares no '{fname} ENUM *'", dist.line)
            elif dc.discriminator != fname:
                self._err(f"DISTRIBUTION FOR '{mn}' uses WHERE {fname}, but DEVICE_CLASS "
                          f"'{dc.name}' declares '{dc.discriminator} ENUM *'", dist.line)

    def _validate_dist_spec(self, spec, measure_name, zone_id):
        required = {
            'NORMAL':      {'mean', 'std_dev'},
            'UNIFORM':     {'low', 'high'},
            'EXPONENTIAL': {'mean'},
            'POISSON':     {'mean'},
            'BINOMIAL':    {'n', 'p'},
        }
        needed = required.get(spec.dist_type, set())
        missing = needed - set(spec.params.keys())
        if missing:
            self._err(
                f"DISTRIBUTION FOR '{measure_name}' zone={zone_id} "
                f"{spec.dist_type}: missing params {missing}"
            )

        # Sanity on values. A zero spread is allowed: NORMAL (mean = 0.0,
        # std_dev = 0.0) and UNIFORM (low = 0, high = 0) are a point mass
        # (README §6.4 uses the former for "no rain").
        p = spec.params
        if spec.dist_type == 'NORMAL' and p.get('std_dev', 1) < 0:
            self._err(f"NORMAL std_dev must be >= 0 in distribution for '{measure_name}'")
        if spec.dist_type == 'UNIFORM' and p.get('low', 0) > p.get('high', 1):
            self._err(f"UNIFORM: low must be <= high in distribution for '{measure_name}'")
        if spec.dist_type in ('EXPONENTIAL', 'POISSON') and p.get('mean', 1) < 0:
            self._err(f"{spec.dist_type}: mean must be >= 0 for '{measure_name}'")
        if spec.dist_type == 'BINOMIAL':
            if not (0 <= p.get('p', 0.5) <= 1):
                self._err(f"BINOMIAL: p must be in [0,1] for '{measure_name}'")
            n = p.get('n', 1)
            if n < 0 or float(n) != int(n):
                self._err(f"BINOMIAL: n must be a whole number >= 0 for '{measure_name}'")

    # ------------------------------------------------------------------
    # Event stream validation
    # ------------------------------------------------------------------

    def _validate_event_streams(self):
        for es in self.prog.event_streams:
            # All referenced event types must exist
            for et_name in es.event_types:
                if et_name not in self._event_types:
                    self._err(f"EVENTSTREAM '{es.name}': references unknown "
                              f"EVENTTYPE '{et_name}'")

            # All measure names in ARRIVALS must exist
            for arr in es.arrivals:
                if arr.measure_name not in self._all_measures:
                    self._warn(f"EVENTSTREAM '{es.name}' ARRIVALS: "
                               f"'{arr.measure_name}' not found in any DEVICE_CLASS")

                # Failure probability must be 0.0 to 1.0
                if not (0.0 <= arr.failure_prob <= 1.0):
                    self._err(f"EVENTSTREAM '{es.name}' ARRIVALS '{arr.measure_name}': "
                              f"FAILURE must be 0.0–1.0, got {arr.failure_prob}")

                # PERIODIC must have interval
                if arr.mode == 'PERIODIC' and arr.interval_ms is None:
                    self._err(f"EVENTSTREAM '{es.name}' ARRIVALS '{arr.measure_name}': "
                              f"PERIODIC requires an interval")

                # ON_CHANGE and ON_THRESHOLD must have threshold
                if arr.mode in ('ON_CHANGE', 'ON_THRESHOLD') and arr.threshold is None:
                    self._err(f"EVENTSTREAM '{es.name}' ARRIVALS '{arr.measure_name}': "
                              f"{arr.mode} requires a threshold value")

        if len(self.prog.event_streams) > 1:
            self._warn('More than one EVENTSTREAM: the spec sheet lists multiple '
                       'streams as future work; all are simulated as one')
        for es in self.prog.event_streams:
            if len(es.event_types) > 1:
                self._warn(f"EVENTSTREAM '{es.name}' lists {len(es.event_types)} event types; "
                           f"the spec sheet streams one (sensor_reading). Devices are "
                           f"assigned to them by name fragments (soil / environment ...)")

        # Multi-measure devices send one packed reading on the first measure's schedule
        by_measure = {a.measure_name: a for es in self.prog.event_streams for a in es.arrivals}
        for dc in self.prog.device_classes:
            if len(dc.measures) < 2 or dc.measures[0].name not in by_measure:
                continue
            first = by_measure[dc.measures[0].name]
            for m in dc.measures[1:]:
                other = by_measure.get(m.name)
                if other and (other.mode, other.interval_ms, other.threshold,
                              other.failure_prob) != (first.mode, first.interval_ms,
                                                      first.threshold, first.failure_prob):
                    self._warn(f"DEVICE_CLASS '{dc.name}': ARRIVALS for {m.name} differs from "
                               f"{dc.measures[0].name}; its packed readings follow "
                               f"{dc.measures[0].name}'s ARRIVALS", dc.line)

        # Every measure a device can produce needs exactly one ARRIVALS entry
        arrivals = [a.measure_name for es in self.prog.event_streams for a in es.arrivals]
        for name in sorted({n for n in arrivals if arrivals.count(n) > 1}):
            self._warn(f"ARRIVALS lists '{name}' more than once — last one wins")
        for dc in self.prog.device_classes:
            for m in dc.measures:
                if m.name not in arrivals:
                    self._err(f"DEVICE_CLASS '{dc.name}' measure {m.name} has no "
                              f"ARRIVALS entry in any EVENTSTREAM", dc.line)

    # ------------------------------------------------------------------
    # Table validation
    # ------------------------------------------------------------------

    def _validate_device_table(self):
        """Exactly one device table, checked on the schema alone (the loader
        applies the same function when it reads the data)."""
        try:
            find_device_table(self.prog.tables)
        except ResourceError as e:
            for msg in e.errors:
                self._err(msg)

    def _validate_tables(self):
        self._validate_device_table()
        for tbl in self.prog.tables:
            for col in tbl.columns:
                if col.references:
                    ref_table, ref_col = col.references
                    # DEVICE_CLASS is a schema construct, not a table - skip
                    if ref_table == 'DEVICE_CLASS':
                        continue
                    if ref_table not in self._tables:
                        self._err(f"Table '{tbl.name}' column '{col.name}': "
                                  f"REFERENCES unknown table '{ref_table}'")
                    elif ref_col not in self._tables[ref_table]:
                        self._err(f"Table '{tbl.name}' column '{col.name}': "
                                  f"REFERENCES unknown column '{ref_table}.{ref_col}'")

    # ------------------------------------------------------------------
    # Rule validation
    # ------------------------------------------------------------------

    def _validate_event_types(self):
        streamed = [et for es in self.prog.event_streams for et in es.event_types]
        table_names = {t.name.lower(): t for t in self.prog.tables}
        device_tables = {t.name.lower() for t in self.prog.tables for c in t.columns
                         if c.references and c.references[0] == 'DEVICE_CLASS'}
        for et in self.prog.event_types:
            for a in et.attributes:
                if not a.references:
                    continue
                tbl, col = a.references
                t = table_names.get(tbl.lower())
                if t is None:
                    self._err(f"EVENTTYPE '{et.name}': {a.name} references table '{tbl}', "
                              f"which no CREATE TABLE declares", et.line)
                elif col.lower() not in {c.name.lower() for c in t.columns}:
                    self._err(f"EVENTTYPE '{et.name}': {a.name} references {tbl}({col}), "
                              f"but {tbl} has no column {col}", et.line)
                elif a.name == 'device_id' and device_tables and tbl.lower() not in device_tables:
                    self._err(f"EVENTTYPE '{et.name}': device_id references {tbl}, but the "
                              f"device table (the one with REFERENCES DEVICE_CLASS) is "
                              f"{next(iter(device_tables))}", et.line)
            if et.max_delay_ms is not None and et.max_delay_ms <= 0:
                self._err(f"EVENTTYPE '{et.name}': MAXDELAY must be > 0", et.line)
            if et.ingest_attr and et.name not in streamed:
                self._warn(f"EVENTTYPE '{et.name}': INGESTTIME only applies to transmitted "
                           f"events; rule-computed '{et.name}' events have none", et.line)

        # The streamed type must have room for every device's measures
        by_name = {et.name: et for et in self.prog.event_types}
        for name in set(streamed):
            et = by_name.get(name)
            if et is None:
                continue
            attrs = {a.name for a in et.attributes}
            if 'device_id' not in attrs:
                self._err(f"EVENTTYPE '{name}' (streamed) needs a device_id attribute", et.line)
            fields = [f for f in ('value', 'value2', 'value3') if f in attrs]
            for dc in self.prog.device_classes:
                if len(dc.measures) > len(fields):
                    self._err(f"DEVICE_CLASS '{dc.name}' has {len(dc.measures)} measures but "
                              f"EVENTTYPE '{name}' has {len(fields)} value fields "
                              f"(value, value2, value3 hold them in MEASURES order)", dc.line)

    def _validate_rules(self):
        streamed = next((et for es in self.prog.event_streams for et in es.event_types), None)
        for i, rule in enumerate(self.prog.rules):
            _, errors, warnings = compile_rule(rule, i, self.prog, ms_per_tick=None,
                                               streamed_type=streamed)
            for msg in errors:
                self._err(msg, rule.line)
            for msg in warnings:
                self._warn(msg, rule.line)
