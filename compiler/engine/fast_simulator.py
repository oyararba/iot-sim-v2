"""
engine/fast_simulator.py

Heap-based simulation engine (README §8). It consumes profiles.json and
writes events in event-time order.

Readings (README §6.1–6.4, §8.2)
  * One device = one reading per transmission. A multi-measure device packs
    its measures in MEASURES order into value / value2 / value3; timing,
    arrival mode and FAILURE come from its first measure's ARRIVALS entry.
  * Each value: pick a tier (NORMAL / ABOVE / BELOW), sample it, clamp to
    RANGE, round to RESOLUTION. The tier is tagged on the event (tier,
    tier2, tier3) for the correctness analyser.
  * STICKY tiers: the tier is a per-device Markov chain whose long-run
    frequencies equal the declared PROB values (see codegen.sticky_chain),
    and when the tier changes into or out of a STICKY tier the mean moves
    there over `sticky_ramp` readings instead of jumping.
  * ARRIVALS FAILURE p drops each reading with probability p.
  * DISTRIBUTION FAILURE <duration>: each device's sensor for that measure
    has an exponential lifetime with that mean; once it fails the measure is
    no longer reported, and a device whose measures have all failed stops.
  * INGESTTIME I MAXDELAY d: each reading gets ingest_time = event_time +
    delay, delay ~ Exponential(mean d/2); a reading later than d has
    stale = true.
  * Readings carry exactly the attributes their EVENTTYPE declares:
    device_id, the measure name (measure_type), value..value3, and any
    attribute named like a device-table column (depth_cm ...).

Rules (README §6.5, §8.3): see codegen/rules.py for the semantics. The
engine processes, at each tick: tumbling-window closes for windows that
ended before the tick, then readings, then triggered events due at the
tick. Triggered events are written to the output and fed to downstream
(chained) rules.
"""

from __future__ import annotations

import heapq
import math
import random
import time
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from codegen.rules import norm_const

# heap priorities within one tick
CLOSE, READING, TRIGGERED = 0, 1, 2


# -----------------------------------------------------------------------
# Sampling
# -----------------------------------------------------------------------

def sample(dist_type: str, params: Dict) -> float:
    dt = dist_type.lower()
    if dt == 'normal':
        sd = params['std_dev']
        return params['mean'] if sd <= 0 else random.gauss(params['mean'], sd)
    if dt == 'uniform':
        lo, hi = params['low'], params['high']
        return lo if hi <= lo else random.uniform(lo, hi)
    if dt == 'exponential':
        m = params['mean']
        return 0.0 if m <= 0 else random.expovariate(1.0 / m)
    if dt == 'poisson':
        lam = params['mean']
        if lam <= 0:
            return 0.0
        if lam < 30:
            L, k, p = math.exp(-lam), 0, 1.0
            while p > L:
                k += 1
                p *= random.random()
            return float(k - 1)
        return float(max(0, int(round(random.gauss(lam, math.sqrt(lam))))))
    if dt == 'binomial':
        return float(sum(1 for _ in range(int(params['n'])) if random.random() < params['p']))
    return 0.0


def _decimals(res: float) -> int:
    text = repr(float(res))
    if 'e' in text:
        return max(0, -int(text.split('e')[1]))
    return len(text.split('.')[1].rstrip('0')) if '.' in text else 0


def _cumulative(probs: List[float]) -> List[float]:
    out, acc = [], 0.0
    for p in probs:
        acc += p
        out.append(acc)
    return out


def _draw(cum: List[float]) -> int:
    r = random.random() * cum[-1]
    for i, c in enumerate(cum):
        if r < c:
            return i
    return len(cum) - 1


class Slot:
    """One measure of one device: its model, tier state and sensor lifetime."""

    __slots__ = ('measure', 'field', 'tier_field', 'tiers', 'cum', 'chain_stay',
                 'redraw_cum', 'lo', 'hi', 'res', 'dec', 'cur', 'ramp_left',
                 'ramp_gap', 'offset', 'ramp_n', 'death')

    def __init__(self, entry: Dict, profile: Dict, ramp_n: int, start: int):
        self.measure    = entry['measure_name']
        self.field      = entry['field']
        self.tier_field = entry['tier_field']
        self.tiers      = profile['tiers']
        self.cum        = _cumulative([t['probability'] for t in self.tiers])
        chain           = profile.get('chain')
        self.chain_stay = chain['stay'] if chain else None
        self.redraw_cum = _cumulative(chain['redraw']) if chain else None
        vr              = profile.get('valid_range', {})
        self.lo         = vr.get('min', float('-inf'))
        self.hi         = vr.get('max', float('inf'))
        self.res        = profile.get('resolution') or 0.0
        self.dec        = _decimals(self.res) if self.res > 0 else 6
        self.cur        = None
        self.ramp_left  = 0
        self.ramp_gap   = 0.0
        self.offset     = 0.0
        self.ramp_n     = ramp_n
        life            = profile.get('lifetime_ticks')
        self.death      = (start + math.ceil(random.expovariate(1.0 / life))
                           if life else None)

    def next_value(self) -> Tuple[float, str]:
        prev = self.cur
        if self.chain_stay is None or prev is None:
            idx = _draw(self.cum)
        elif random.random() < self.chain_stay[prev]:
            idx = prev
        else:
            idx = _draw(self.redraw_cum)
        self.cur = idx
        tier = self.tiers[idx]

        # STICKY: move the mean gradually into / out of a sticky tier
        if prev is not None and idx != prev:
            if self.ramp_n > 0 and (tier['sticky'] or self.tiers[prev]['sticky']):
                self.ramp_gap  = (self.tiers[prev]['mean'] + self.offset) - tier['mean']
                self.ramp_left = self.ramp_n
            else:
                self.ramp_left = 0
        if self.ramp_left > 0:
            self.offset = self.ramp_gap * self.ramp_left / (self.ramp_n + 1)
            self.ramp_left -= 1
        else:
            self.offset = 0.0

        dist = tier['distribution']
        v = sample(dist['type'], dist['params']) + self.offset
        v = min(max(v, self.lo), self.hi)
        if self.res > 0:
            v = round(round(v / self.res) * self.res, self.dec)
            v = min(max(v, self.lo), self.hi)
        else:
            v = round(v, 6)
        return v, tier['tier']


class Device:
    __slots__ = ('device_id', 'event_type', 'slots', 'attrs', 'mode', 'interval',
                 'threshold', 'failure', 'last_value')

    def __init__(self, binding: Dict, profiles: Dict, ramp_n: int, start: int):
        self.device_id  = binding['device_id']
        self.event_type = binding['event_type']
        self.slots      = [Slot(m, profiles[m['profile']], ramp_n, start)
                           for m in binding['measures']]
        self.attrs      = binding.get('attrs', {})
        smp             = binding['sampling']
        self.mode       = smp['mode']
        self.interval   = smp.get('interval_ticks') or 1     # ON_* modes sample every tick
        self.threshold  = smp.get('threshold') or 0.0
        self.failure    = smp.get('failure') or 0.0
        self.last_value = None


# -----------------------------------------------------------------------
# Rules
# -----------------------------------------------------------------------

def _compare(a: Any, op: str, b: Any) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        x, y = float(a), float(b)
    elif op in ('==', '!='):
        x, y = norm_const(a), norm_const(b)
    else:
        x, y = str(a), str(b)
    if op == '<':  return x < y
    if op == '<=': return x <= y
    if op == '>':  return x > y
    if op == '>=': return x >= y
    if op == '==': return x == y
    if op == '!=': return x != y
    return False


def _check(constraints: List[Dict], b: Dict) -> bool:
    for c in constraints:
        rhs = c['rhs']
        rv = b.get(rhs['value']) if rhs['kind'] == 'var' else rhs['value']
        if not _compare(b.get(c['lhs']), c['op'], rv):
            return False
    return True


def _aggregate(func: str, values: List[Any]) -> Any:
    vals = [v for v in values if v is not None]
    if func == 'count':
        return len(vals)
    if func == 'sum':
        return float(sum(vals))
    if not vals:
        return None
    if func == 'avg':
        return sum(vals) / len(vals)
    if func == 'min':
        return min(vals)
    if func == 'max':
        return max(vals)
    if func == 'last':
        return vals[-1]
    return None


class CompiledRule:
    def __init__(self, spec: Dict, start: int):
        self.spec       = spec
        self.label      = spec['label']
        self.head_event = spec['head_event']
        self.window     = spec['window']
        self.sources    = spec['sources']
        self.joins      = spec['join_constraints']
        self.group_vars = spec['group_vars']
        self.head       = spec['head']
        self.aggs       = spec['aggs']
        self.fill_same  = spec['fill_same_name']
        self.fire_off   = spec['fire']['offset_ticks']
        self.start      = start
        w = self.window
        self.kind = 'none' if w is None else (
            'tumbling' if w['type'] == 'tumbling' else
            ('sliding_count' if w.get('count') else 'sliding_time'))
        self.size = None if w is None else (w.get('count') or w.get('ticks'))
        # state
        self.buffers: Dict[int, List[List[Dict]]] = {}        # tumbling: window → per source
        self.groups:  Dict[tuple, deque] = {}                  # sliding: group → bindings

    def match(self, si: int, ev: Dict) -> Optional[Dict]:
        src = self.sources[si]
        b: Dict[str, Any] = {}
        for attr, kind, val in src['pattern']:
            x = ev.get(attr)
            if kind == 'const':
                if x is None or norm_const(x) != val:
                    return None
            elif val in b:
                if b[val] != x:
                    return None
            else:
                b[val] = x
        tv = src['time_var']
        t = ev['event_time']
        if tv in b and b[tv] != t:
            return None
        b[tv] = t
        b['__time__'] = t
        if src['local_constraints'] and not _check(src['local_constraints'], b):
            return None
        return b

    def group_key(self, b: Dict) -> tuple:
        return tuple(b.get(v) for v in self.group_vars)

    def build_event(self, b: Dict, agg: Dict, fire: float) -> Dict:
        ev: Dict[str, Any] = {'event_type': self.head_event, 'event_time': fire}
        assigned = set()
        for h in self.head:
            if h['kind'] == 'var':
                v = b.get(h['value'])
            elif h['kind'] == 'const':
                v = h['value']
            else:
                v = agg.get(h['value'])
                if isinstance(v, float):
                    v = round(v, 6)
            assigned.add(h['attr'])
            if v is not None:
                ev[h['attr']] = v
        if self.fill_same:
            for k, v in b.items():
                if k not in assigned and k != '__time__' and k in self.head_attrs \
                        and v is not None:
                    ev[k] = v
        ev['triggered_by'] = self.label
        return ev

    def aggregate(self, tuples: List[Dict]) -> Dict:
        return {a['out']: _aggregate(a['func'], [t.get(a['in']) for t in tuples])
                for a in self.aggs}


# -----------------------------------------------------------------------
# Simulator
# -----------------------------------------------------------------------

class FastSimulator:
    def __init__(self, compiled: Dict, ms_per_tick: int = 60000, verbose: int = 0):
        self.compiled = compiled
        self.verbose  = verbose

        sim = compiled.get('simulation', {})
        self._start = int(sim.get('start_time', 0))
        self._end   = int(sim.get('end_time', 1000))
        self.ms_per_tick = int(sim.get('ms_per_tick', ms_per_tick))
        ramp_n = int(sim.get('sticky_ramp', 5))

        shape = compiled.get('stream', {})
        self._types = shape.get('types', {})

        profiles = compiled.get('profiles', {})
        self._devices: Dict[str, Device] = {}
        self._heap: list = []
        self._seq = 0
        for binding in compiled.get('bindings', []):
            dev = Device(binding, profiles, ramp_n, self._start)
            self._devices[dev.device_id] = dev
            self._push(self._start, READING, dev.device_id)

        event_types = compiled.get('event_types', {})
        self._rules: List[CompiledRule] = []
        self._listeners: Dict[str, List[Tuple[CompiledRule, int]]] = defaultdict(list)
        for spec in compiled.get('rules', []):
            rule = CompiledRule(spec, self._start)
            rule.head_attrs = set(event_types.get(rule.head_event, []))
            self._rules.append(rule)
            for si, src in enumerate(rule.sources):
                self._listeners[src['event_type']].append((rule, si))
            if rule.kind == 'tumbling':
                self._push(self._start + rule.size, CLOSE, (rule, 0))

        self.stats = {
            'persistent': 0, 'triggered': 0,
            'dropped_failure': 0, 'dropped_no_change': 0,
            'sensors_failed': 0, 'devices_failed': 0, 'stale': 0,
            'triggered_after_end': 0,
        }
        self._emit: Optional[Callable] = None
        self._collected: List[Dict] = []

    def _push(self, tick: float, prio: int, payload: Any):
        self._seq += 1
        heapq.heappush(self._heap, (tick, prio, self._seq, payload))

    # ------------------------------------------------------------------

    def run(self, output_fn: Callable = None) -> List:
        self._emit = output_fn
        heap = self._heap
        t0 = time.perf_counter()
        last_log = self._start
        print(f'[FastSim] Ticks {self._start}→{self._end} | '
              f'Devices: {len(self._devices)} | Rules: {len(self._rules)} | '
              f'ms/tick: {self.ms_per_tick}')

        while heap:
            tick, prio, _, payload = heapq.heappop(heap)
            if prio == READING:
                if tick > self._end:
                    continue
                self._reading(tick, self._devices[payload])
            elif prio == CLOSE:
                rule, k = payload
                if tick > self._end + 1:
                    continue
                self._close_tumbling(rule, k, tick)
                nxt = self._start + (k + 2) * rule.size
                if nxt <= self._end + 1:
                    self._push(nxt, CLOSE, (rule, k + 1))
            else:
                self._deliver(payload, tick)

            if self.verbose > 0 and tick - last_log >= self.verbose:
                wall = time.perf_counter() - t0
                total = self.stats['persistent'] + self.stats['triggered']
                pct = 100 * (tick - self._start) / max(self._end - self._start, 1)
                print(f'  [t={tick:>7}] {pct:5.1f}%  events={total:,}  '
                      f'{total / max(wall, 0.001):,.0f} ev/s')
                last_log = tick

        wall = time.perf_counter() - t0
        st = self.stats
        total = st['persistent'] + st['triggered']
        print(f'[FastSim] Done: {total:,} events in {wall:.2f}s '
              f'({total / max(wall, 0.001):,.0f} ev/s)')
        print(f'  Persistent : {st["persistent"]:,}  Triggered  : {st["triggered"]:,}')
        print(f'  Dropped (failure): {st["dropped_failure"]:,}  '
              f'Dropped (no change): {st["dropped_no_change"]:,}')
        if st['stale']:
            print(f'  Stale (ingested after MAXDELAY): {st["stale"]:,}')
        if st['sensors_failed']:
            print(f'  Sensors failed (FAILURE lifetime): {st["sensors_failed"]:,}  '
                  f'Devices stopped: {st["devices_failed"]:,}')
        if st['triggered_after_end']:
            print(f'  Triggered events due after the end (not written): '
                  f'{st["triggered_after_end"]:,}')
        return self._collected

    # ------------------------------------------------------------------
    # Readings
    # ------------------------------------------------------------------

    def _reading(self, tick: int, dev: Device):
        values: Dict[str, Any] = {}
        tiers:  Dict[str, str] = {}
        primary = None
        measure_name = dev.slots[0].measure
        live = 0
        for slot in dev.slots:
            if slot.death is not None and tick >= slot.death:
                if slot.death >= 0:
                    self.stats['sensors_failed'] += 1
                    slot.death = -1                       # count once
                continue
            if slot.death == -1:
                continue
            live += 1
            v, tier = slot.next_value()
            values[slot.field] = v
            tiers[slot.tier_field] = tier
            if primary is None:
                primary = v
        if live == 0:
            self.stats['devices_failed'] += 1
            return                                        # stops: not rescheduled

        if dev.mode == 'PERIODIC':
            emit = True
        elif dev.mode == 'ON_CHANGE':
            emit = dev.last_value is None or abs(primary - dev.last_value) >= dev.threshold
        else:   # ON_THRESHOLD: emit when the value crosses the boundary
            prev = dev.last_value
            emit = prev is None and primary >= dev.threshold or (
                prev is not None and (prev >= dev.threshold) != (primary >= dev.threshold))

        nxt = tick + dev.interval
        if nxt <= self._end:
            self._push(nxt, READING, dev.device_id)

        if not emit:
            self.stats['dropped_no_change'] += 1
            return
        if dev.failure and random.random() < dev.failure:
            self.stats['dropped_failure'] += 1
            return
        dev.last_value = primary

        shape = self._types.get(dev.event_type, {})
        ev: Dict[str, Any] = {'event_type': dev.event_type, 'event_time': tick}
        ingest = shape.get('ingest')
        if ingest:
            delay = random.expovariate(1.0 / ingest['mean_delay_ticks'])
            ev['ingest_time'] = round(tick + delay, 3)
            stale = delay > ingest['max_delay_ticks']
            ev['stale'] = stale
            if stale:
                self.stats['stale'] += 1
        measure_attr = shape.get('measure_attr')
        for attr in shape.get('attributes', []):
            if attr == 'device_id':
                ev[attr] = dev.device_id
            elif attr == measure_attr:
                ev[attr] = measure_name
            elif attr in values:
                ev[attr] = values[attr]
            elif attr in dev.attrs and dev.attrs[attr] is not None:
                ev[attr] = dev.attrs[attr]
        ev.update(tiers)
        self._deliver(ev, tick)

    # ------------------------------------------------------------------
    # Event delivery: output + rules
    # ------------------------------------------------------------------

    def _deliver(self, ev: Dict, now: float):
        if 'triggered_by' in ev:
            self.stats['triggered'] += 1
        else:
            self.stats['persistent'] += 1
        if self._emit:
            self._emit(ev)
        else:
            self._collected.append(ev)

        for rule, si in self._listeners.get(ev['event_type'], ()):
            b = rule.match(si, ev)
            if b is None:
                continue
            if rule.kind == 'none':
                fire = b[rule.spec['fire']['var']] + rule.fire_off
                self._schedule(rule.build_event(b, {}, 0), fire, now)
            elif rule.kind == 'tumbling':
                k = int((ev['event_time'] - self._start) // rule.size)
                bufs = rule.buffers.setdefault(k, [[] for _ in rule.sources])
                bufs[si].append(b)
            elif rule.kind == 'sliding_count':
                key = rule.group_key(b)
                dq = rule.groups.get(key)
                if dq is None:
                    dq = rule.groups[key] = deque(maxlen=rule.size)
                dq.append(b)
                if len(dq) == rule.size:
                    s = dq[0]['__time__']
                    self._schedule(rule.build_event(b, rule.aggregate(list(dq)), 0),
                                   s + rule.fire_off, now)
            else:   # sliding_time
                key = rule.group_key(b)
                dq = rule.groups.setdefault(key, deque())
                dq.append(b)
                t = ev['event_time']
                s = t - rule.size + 1
                while dq and dq[0]['__time__'] < s:
                    dq.popleft()
                self._schedule(rule.build_event(b, rule.aggregate(list(dq)), 0),
                               s + rule.fire_off, now)

    def _schedule(self, ev: Dict, fire: float, now: float):
        fire = max(fire, now)                       # never into the past
        if float(fire).is_integer():
            fire = int(fire)
        if fire > self._end:
            self.stats['triggered_after_end'] += 1
            return
        ev['event_time'] = fire
        self._push(fire, TRIGGERED, ev)

    # ------------------------------------------------------------------
    # Tumbling windows
    # ------------------------------------------------------------------

    def _close_tumbling(self, rule: CompiledRule, k: int, now: int):
        bufs = rule.buffers.pop(k, None)
        if not bufs or any(not lst for lst in bufs):
            return
        tuples = bufs[0]
        for other in bufs[1:]:
            tuples = _join(tuples, other)
            if not tuples:
                return
        if rule.joins:
            tuples = [t for t in tuples if _check(rule.joins, t)]
        groups: Dict[tuple, List[Dict]] = {}
        for t in tuples:
            groups.setdefault(rule.group_key(t), []).append(t)
        s = self._start + k * rule.size
        for members in groups.values():
            ev = rule.build_event(members[-1], rule.aggregate(members), 0)
            self._schedule(ev, s + rule.fire_off, now)


def _join(left: List[Dict], right: List[Dict]) -> List[Dict]:
    """Natural join on shared variable names (hash join)."""
    if not left or not right:
        return []
    shared = [k for k in left[0] if k in right[0] and k != '__time__']
    index: Dict[tuple, List[Dict]] = defaultdict(list)
    for r in right:
        index[tuple(r.get(k) for k in shared)].append(r)
    out = []
    for l in left:
        for r in index.get(tuple(l.get(k) for k in shared), ()):
            merged = dict(r)
            merged.update(l)
            merged['__time__'] = max(l['__time__'], r['__time__'])
            out.append(merged)
    return out
