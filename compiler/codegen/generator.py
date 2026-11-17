"""
codegen/generator.py

Takes a validated AST and generates profiles.json for the engine:
  simulation  — tick configuration, STICKY defaults
  stream      — the streamed EVENTTYPE and the shape of a reading
  profiles    — one statistical model per (device class, measure, WHERE block)
  bindings    — one entry per device: its measures, the profile each uses,
                where each value goes (value / value2 / value3), its
                sampling (ARRIVALS) and the event fields read from the DB
  rules       — compiled IoTDL rules (codegen/rules.py)

Device rows and their user-defined field values (e.g. `zone ENUM *`) come
from the enterprise DB via resources/loader.py:

    devices.device_class                     → DEVICE_CLASS
    devices.plot_id → plots.zone_id  (zone)  → DISTRIBUTION ... WHERE zone = <value>

A device whose value has no matching WHERE block is a compile error.

Multi-measure devices (README §6.1) send ONE reading per transmission with
their measures packed in MEASURES order: value, value2, value3. The
reading's timing, arrival mode and FAILURE come from the first measure's
ARRIVALS entry.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ast_nodes.nodes import (
    Program, DeviceClassNode, DistributionNode,
    WhereBlock, TierSpec, DistributionSpec,
    ArrivalSpec, RuleNode,
)
from resources.loader import Resources, ResourceError, value_key
from codegen.rules import MEASURE_ATTRS, compile_rule, event_attributes

VALUE_FIELDS = ['value', 'value2', 'value3']
TIER_FIELDS  = ['tier', 'tier2', 'tier3']

# STICKY defaults (README §6.4 / §8.2 give the behaviour, not the numbers)
DEFAULT_STICKY_PERSISTENCE = 0.9   # chance a STICKY tier holds for the next reading
DEFAULT_STICKY_RAMP        = 5     # readings over which the mean moves to a new tier


def profile_name(dc_name: str, measure_name: str, wb: WhereBlock) -> str:
    """<DeviceClass>_<Measure>_<field><value>, e.g. SoilMoistureSensor_Moisture_zone1,
    or <DeviceClass>_<Measure>_all for a DISTRIBUTION without WHERE."""
    if wb.is_default:
        return f'{dc_name}_{measure_name}_all'
    return f'{dc_name}_{measure_name}_{wb.field}{value_key(wb.value)}'


def dist_moments(dist_type: str, params: Dict[str, float]):
    """(mean, variance) of a tier distribution before clamping."""
    t = dist_type.upper()
    if t == 'NORMAL':
        return params['mean'], params['std_dev'] ** 2
    if t == 'UNIFORM':
        lo, hi = params['low'], params['high']
        return (lo + hi) / 2, (hi - lo) ** 2 / 12
    if t == 'EXPONENTIAL':
        return params['mean'], params['mean'] ** 2
    if t == 'POISSON':
        return params['mean'], params['mean']
    if t == 'BINOMIAL':
        n, p = params['n'], params['p']
        return n * p, n * p * (1 - p)
    return 0.0, 0.0


def sticky_chain(tiers: List[TierSpec], persistence: float) -> Optional[Dict[str, List[float]]]:
    """
    Markov chain over tiers whose long-run frequencies equal the declared
    PROB values. A STICKY tier holds for the next reading with probability
    `persistence`; otherwise (and always for a non-STICKY tier) the next
    tier is redrawn from r, with r_i ∝ p_i (1 − stay_i). Then
    π_i ∝ r_i / (1 − stay_i) = p_i, and the chain is reversible.
    Returns None when no tier is STICKY (independent draws, as before).
    """
    if not any(t.sticky for t in tiers):
        return None
    stay = [persistence if t.sticky else 0.0 for t in tiers]
    w = [t.probability * (1 - s) for t, s in zip(tiers, stay)]
    total = sum(w)
    redraw = [x / total for x in w] if total > 0 else [t.probability for t in tiers]
    return {'stay': stay, 'redraw': redraw}


# -----------------------------------------------------------------------
# Code generator
# -----------------------------------------------------------------------

class CodeGenerator:
    """
    Parameters
    ----------
    program         : validated AST
    resources       : devices resolved from the enterprise DB (resources/loader.py)
    simulation_cfg  : dict with start_time, end_time, time_unit, seed, ms_per_tick,
                      and optionally sticky_persistence / sticky_ramp
    """

    def __init__(
        self,
        program:        Program,
        resources:      Resources,
        simulation_cfg: Dict[str, Any],
    ):
        self.prog      = program
        self.sim_cfg   = dict(simulation_cfg)
        self.sim_cfg.setdefault('sticky_persistence', DEFAULT_STICKY_PERSISTENCE)
        self.sim_cfg.setdefault('sticky_ramp', DEFAULT_STICKY_RAMP)
        self.resources = resources
        self.ms_per_tick = float(self.sim_cfg.get('ms_per_tick', 60_000))

        self._distributions: Dict[str, DistributionNode] = {
            d.measure_name: d for d in program.distributions
        }
        self._arrivals: Dict[str, ArrivalSpec] = {}
        for es in program.event_streams:
            for arr in es.arrivals:
                self._arrivals[arr.measure_name] = arr
        self._event_types = {et.name: et for et in program.event_types}
        self.warnings: List[str] = []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate(self) -> Dict[str, Any]:
        """Returns a dict that can be JSON-serialised to profiles.json"""
        return {
            'simulation':    self.sim_cfg,
            'stream':        self._generate_stream_shape(),
            'event_types':   event_attributes(self.prog),
            'profiles':      self._generate_profiles(),
            'bindings':      self._generate_bindings(),
            'event_streams': self._generate_streams(),
            'rules':         self._generate_rules(),
        }

    def _ticks(self, ms: float) -> float:
        return ms / self.ms_per_tick

    # ------------------------------------------------------------------
    # Stream shape: what a transmitted reading looks like
    # ------------------------------------------------------------------

    def _streamed_type(self) -> Optional[str]:
        streamed = [et for es in self.prog.event_streams for et in es.event_types]
        return streamed[0] if streamed else None

    def _generate_stream_shape(self) -> Dict[str, Any]:
        types = {}
        for es in self.prog.event_streams:
            for name in es.event_types:
                et = self._event_types.get(name)
                if et is None:
                    continue
                attrs = [a.name for a in et.attributes]
                ingest = None
                if et.ingest_attr and et.max_delay_ms:
                    max_delay = self._ticks(et.max_delay_ms)
                    # Delay ~ Exponential(mean = MAXDELAY / 2); later than MAXDELAY = stale
                    ingest = {'attr': et.ingest_attr, 'max_delay_ticks': max_delay,
                              'mean_delay_ticks': max_delay / 2}
                types[name] = {
                    'attributes':   attrs,
                    'measure_attr': next((a for a in attrs if a in MEASURE_ATTRS), None),
                    'value_fields': [f for f in VALUE_FIELDS if f in attrs],
                    'ingest':       ingest,
                }
        return {'event_type': self._streamed_type(), 'types': types}

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def _generate_profiles(self) -> Dict[str, Any]:
        """
        One profile entry per (device_class, measure, WHERE block).
        Profile name: see profile_name(), e.g. SoilMoistureSensor_Moisture_zone1
        """
        profiles = {}
        for dc in self.prog.device_classes:
            for measure in dc.measures:
                dist_node = self._distributions.get(measure.name)
                if dist_node is None:
                    continue
                for wb in dist_node.where_blocks:
                    profiles[profile_name(dc.name, measure.name, wb)] = self._build_profile(
                        dc_name=dc.name, measure=measure, dist_node=dist_node,
                        where_block=wb)
        return profiles

    def _build_profile(self, dc_name: str, measure: Any, dist_node: DistributionNode,
                       where_block: WhereBlock) -> Dict[str, Any]:
        arrival = self._arrivals.get(measure.name)
        persistence = float(self.sim_cfg['sticky_persistence'])

        tiers = []
        for t in where_block.tiers:
            mean, var = dist_moments(t.distribution.dist_type, t.distribution.params)
            tiers.append({
                'tier':         t.tier,
                'distribution': self._dist_to_dict(t.distribution),
                'probability':  t.probability,
                'sticky':       t.sticky,
                'mean':         mean,
                'variance':     var,
            })

        profile: Dict[str, Any] = {
            'event_type':    self._find_event_type(dc_name),
            'measure_name':  measure.name,
            'unit':          measure.unit,
            'valid_range':   {'min': measure.range_min, 'max': measure.range_max},
            'resolution':    measure.resolution,
            'discriminator': where_block.field,     # e.g. 'zone'; None = applies to all
            'zone_id':       where_block.value,     # the WHERE value
            'tiers':         tiers,
            'chain':         sticky_chain(where_block.tiers, persistence),
        }
        # Back-compatible tier dicts (normal / above / below)
        for t in tiers:
            profile.setdefault(t['tier'].lower(), {
                'distribution': t['distribution'], 'probability': t['probability'],
                'sticky': t['sticky']})

        if dist_node.failure_lifetime_ms:
            profile['lifetime_ticks'] = self._ticks(dist_node.failure_lifetime_ms)

        if arrival:
            profile['sampling'] = self._sampling(arrival)
        return profile

    def _sampling(self, arrival: ArrivalSpec) -> Dict[str, Any]:
        interval_ticks = None
        if arrival.mode == 'PERIODIC':
            exact = self._ticks(arrival.interval_ms)
            interval_ticks = max(1, int(round(exact)))
            if abs(exact - interval_ticks) > 1e-9:
                self.warnings.append(
                    f'ARRIVALS {arrival.measure_name}: interval is {exact:g} ticks; '
                    f'using {interval_ticks}')
        return {
            'mode':           arrival.mode,
            'interval_ms':    arrival.interval_ms,
            'interval_ticks': interval_ticks,
            'threshold':      arrival.threshold,
            'failure':        arrival.failure_prob,
        }

    @staticmethod
    def _dist_to_dict(spec: DistributionSpec) -> Dict[str, Any]:
        return {'type': spec.dist_type.lower(), 'params': spec.params}

    def _find_event_type(self, dc_name: str) -> str:
        """
        Which event type a device's readings are tagged with.

        The EVENTSTREAM declares it: `EVENTTYPE sensor_reading` means every
        transmitted reading is a sensor_reading, wherever that EVENTTYPE sits
        in the file. Only when a stream lists several event types does this
        fall back to matching name fragments (soil / environment / ...)
        among the listed ones.
        """
        streamed = [et for es in self.prog.event_streams for et in es.event_types]
        if len(set(streamed)) == 1:
            return streamed[0]
        candidates = [et for et in self.prog.event_types
                      if not streamed or et.name in streamed]
        # Match the device class's measure name (or its class name without
        # a _Sensor suffix) to the one listed event type whose name contains it:
        # Ammonia_Sensor / MEASURES Ammonia -> Ammonia_Reading.
        dc = next((d for d in self.prog.device_classes if d.name == dc_name), None)
        keys = [m.name.lower() for m in dc.measures] if dc else []
        stem = dc_name.lower()
        for suffix in ('_sensor', 'sensor'):
            if stem.endswith(suffix):
                stem = stem[:-len(suffix)]
                break
        keys.append(stem)
        for key in keys:
            hits = [et.name for et in candidates if key and key in et.name.lower()]
            if len(hits) == 1:
                return hits[0]
        
        # Match device class name fragments to event type names
        dc_lower = dc_name.lower()
        for et in candidates:
            et_lower = et.name.lower()
            if any(fragment in et_lower for fragment in
                   ['soil', 'environment', 'vital', 'location', 'plant']):
                # Map by device class category
                if any(s in dc_lower for s in
                       ['moisture', 'temperature', 'npk', 'ph', 'salinity', 'insect',
                        'soil']):
                    if 'soil' in et_lower:
                        return et.name
                elif any(s in dc_lower for s in
                         ['air', 'humidity', 'co2', 'solar', 'wind', 'rain']):
                    if 'environment' in et_lower:
                        return et.name
                elif any(s in dc_lower for s in ['oximeter', 'ecg', 'nibp', 'thermo',
                                                  'respiratory', 'icp']):
                    if 'vital' in et_lower:
                        return et.name
                elif 'rfid' in dc_lower:
                    if 'location' in et_lower:
                        return et.name
        # Fallback: the stream's first event type, else the first declared
        if streamed:
            return streamed[0]
        return self.prog.event_types[0].name if self.prog.event_types else 'sensor_reading'

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def _generate_bindings(self) -> List[Dict[str, Any]]:
        """
        One binding per device in the enterprise DB. For each of its measures,
        pick the WHERE block whose value equals the device's user-defined
        field value (e.g. its zone), or the DISTRIBUTION's unconditional block.

        Any device that cannot be bound is a compile error (ResourceError),
        never a silent skip.
        """
        bindings: List[Dict[str, Any]] = []
        errors:   List[str] = []
        classes = {dc.name: dc for dc in self.prog.device_classes}

        for dev in self.resources.devices:
            dc          = classes[dev.device_class]
            field_name  = dc.discriminator
            field_value = dev.fields.get(field_name) if field_name else None
            et_name     = self._find_event_type(dc.name)
            et          = self._event_types.get(et_name)
            et_attrs    = [a.name for a in et.attributes] if et else []

            measures = []
            for i, measure in enumerate(dc.measures):
                dist = self._distributions.get(measure.name)
                if dist is None:
                    errors.append(f'device {dev.device_id!r} ({dc.name}): '
                                  f'no DISTRIBUTION FOR {measure.name}')
                    continue
                wb = self._select_block(dist, field_value)
                if wb is None:
                    defined = ', '.join(value_key(b.value) for b in dist.where_blocks)
                    if field_name is None:
                        errors.append(
                            f'device {dev.device_id!r} ({dc.name}): DISTRIBUTION FOR '
                            f'{measure.name} uses WHERE, but {dc.name} declares no '
                            f'user-defined field')
                    else:
                        errors.append(
                            f'device {dev.device_id!r} ({dc.name}) has {field_name} = '
                            f'{value_key(field_value)}, but DISTRIBUTION FOR '
                            f'{measure.name} has no WHERE {field_name} = '
                            f'{value_key(field_value)} block (defined: {defined})')
                    continue
                field = VALUE_FIELDS[i] if i < len(VALUE_FIELDS) else None
                if field is None or field not in et_attrs:
                    errors.append(
                        f'device {dev.device_id!r} ({dc.name}): measure {i + 1} '
                        f'({measure.name}) needs a {field or "value" + str(i + 1)} '
                        f'attribute in EVENTTYPE {et_name}')
                    continue
                measures.append({
                    'measure_name': measure.name,
                    'profile':      profile_name(dc.name, measure.name, wb),
                    'field':        field,
                    'tier_field':   TIER_FIELDS[i],
                })

            first = self._arrivals.get(dc.measures[0].name) if dc.measures else None
            if not measures or first is None:
                continue
            bindings.append({
                'device_id':     dev.device_id,
                'device_class':  dc.name,
                'event_type':    et_name,
                'discriminator': field_name,
                'zone_id':       field_value,     # the device's field value (e.g. its zone)
                'measures':      measures,
                'sampling':      self._sampling(first),
                'attrs':         self._event_attrs(dc.name, dev),
            })

        if errors:
            raise ResourceError(errors)
        return bindings

    @staticmethod
    def _select_block(dist: DistributionNode, value: Any) -> Optional[WhereBlock]:
        if dist.is_unconditional:
            return dist.where_blocks[0]
        if value is None:
            return None
        key = value_key(value)
        return next((b for b in dist.where_blocks if value_key(b.value) == key), None)

    def _event_attrs(self, dc_name: str, dev: Any) -> Dict[str, Any]:
        """
        Event attributes filled from the enterprise DB: every attribute of the
        device's event type whose name matches a column of the device table
        (e.g. depth_cm, device_class) or a user-defined field (e.g. zone).
        """
        et = self._event_types.get(self._find_event_type(dc_name))
        if et is None:
            return {}
        row = {k.lower(): v for k, v in dev.row.items()}
        id_col = self.resources.id_column.lower()
        skip = {'device_id', id_col, *VALUE_FIELDS, *MEASURE_ATTRS}
        attrs: Dict[str, Any] = {}
        for attr in et.attributes:
            name = attr.name.lower()
            if name in skip:
                continue
            if name in row:
                attrs[attr.name] = row[name]
            elif name in dev.fields:
                attrs[attr.name] = dev.fields[name]
        return attrs

    # ------------------------------------------------------------------
    # Streams and rules
    # ------------------------------------------------------------------

    def _generate_streams(self) -> List[Dict[str, Any]]:
        return [{
            'name':        es.name,
            'event_types': es.event_types,
            'arrivals': [{
                'measure':     a.measure_name,
                'mode':        a.mode,
                'interval_ms': a.interval_ms,
                'threshold':   a.threshold,
                'failure':     a.failure_prob,
            } for a in es.arrivals],
        } for es in self.prog.event_streams]

    def _generate_rules(self) -> List[Dict[str, Any]]:
        compiled, errors = [], []
        for i, rule in enumerate(self.prog.rules):
            c, errs, warns = compile_rule(rule, i, self.prog, ms_per_tick=self.ms_per_tick,
                                          streamed_type=self._streamed_type())
            errors.extend(errs)
            self.warnings.extend(warns)
            if c:
                if any(o['label'] == c['label'] for o in compiled):
                    c['label'] = f"{c['label']}#{i + 1}"
                compiled.append(c)
        if errors:
            raise ResourceError(errors)
        return compiled
