"""
eval/correctness.py

Correctness analysis — observed vs declared for a completed simulation run
(README §9).

  * Values are pooled over every device of a (device class, measure, zone).
  * Multi-measure readings are read from value / value2 / value3 in the
    device class's MEASURES order — taken from the compiled bindings, so no
    per-class configuration is needed.
  * Mean and std are compared with the MIXTURE statistics of all tiers,
    using each tier's own distribution (NORMAL, UNIFORM, EXPONENTIAL,
    POISSON, BINOMIAL):
        mean_mix = Σ pᵢ μᵢ
        var_mix  = Σ pᵢ (σᵢ² + (μᵢ − mean_mix)²)
  * Tier rates are compared with the declared PROB values.
  * The expected tier-switch rate is 1 − Σ pᵢ² for independent tiers, and
    Σ pᵢ (1 − stayᵢ)(1 − rᵢ) when STICKY tiers make the tier a Markov chain.
    Switches are counted within each device's own sequence.
"""

import json, math, statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# ── Mixture statistics ────────────────────────────────────────────────────────

def _moments(tier: Dict):
    """(mean, variance) of a tier, from its distribution type and parameters."""
    if 'mean' in tier and 'variance' in tier:
        return tier['mean'], tier['variance']
    dist = tier.get('distribution', {})
    t, p = dist.get('type', 'normal').lower(), dist.get('params', {})
    if t == 'normal':      return p.get('mean', 0.0), p.get('std_dev', 0.0) ** 2
    if t == 'uniform':     return (p['low'] + p['high']) / 2, (p['high'] - p['low']) ** 2 / 12
    if t == 'exponential': return p['mean'], p['mean'] ** 2
    if t == 'poisson':     return p['mean'], p['mean']
    if t == 'binomial':    return p['n'] * p['p'], p['n'] * p['p'] * (1 - p['p'])
    return 0.0, 0.0


def tier_dicts(profile: Dict) -> List[Dict]:
    tiers = profile.get('tiers')
    if tiers is None:   # older profiles.json
        tiers = [dict(profile[n], tier=n.upper()) for n in ('normal', 'above', 'below')
                 if profile.get(n)]
    out = []
    for t in tiers:
        mean, var = _moments(t)
        out.append({'name': t['tier'], 'mean': mean, 'std': math.sqrt(max(var, 0.0)),
                    'prob': t.get('probability', 0.0)})
    return out

def mix_mean(tiers): return sum(t['prob'] * t['mean'] for t in tiers)

def mix_std(tiers):
    mu = mix_mean(tiers)
    var = sum(t['prob'] * (t['std']**2 + (t['mean'] - mu)**2) for t in tiers)
    return math.sqrt(var)

def expected_switch_rate(profile: Dict, tds: List[Dict]) -> float:
    chain = profile.get('chain')
    if not chain:
        return 1.0 - sum(td['prob'] ** 2 for td in tds)
    return sum(td['prob'] * (1 - s) * (1 - r)
               for td, s, r in zip(tds, chain['stay'], chain['redraw']))

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TierResult:
    tier: str
    declared_prob: float
    observed_prob: float
    observed_count: int
    total_count: int
    within_tolerance: bool
    tolerance: float = 0.05

    @property
    def deviation_pp(self): return abs(self.observed_prob - self.declared_prob) * 100

@dataclass
class MeasureResult:
    device_id: str
    device_class: str
    measure_name: str
    zone_id: object
    n_events: int
    declared_failure: float
    observed_mean: float
    declared_mean: float
    mean_ok: bool
    observed_std: float
    declared_std: float
    std_ok: bool
    tier_results: List[TierResult]
    n_transitions: int
    transition_rate: float
    expected_transition_rate: float
    n_out_of_range: int
    range_ok: bool
    n_devices: int = 1
    sticky: bool = False
    arrival_mode: str = 'PERIODIC'
    decaying: bool = False

    @property
    def applicable(self):
        """ON_CHANGE / ON_THRESHOLD transmit a value-dependent subset of readings,
        and a DECAY battery widens the spread on purpose, so in both cases the
        observed statistics are not expected to match the declared model."""
        return self.arrival_mode == 'PERIODIC' and not self.decaying

    @property
    def skip_reason(self):
        if self.decaying:
            return 'DECAY widens the spread as the battery drains'
        return f'{self.arrival_mode} transmits a value-dependent subset'

    @property
    def low_power(self):
        """True when ±5 pp is within two standard errors of some tier rate,
        i.e. the sample is too small for the tier check to mean much."""
        return any(2 * math.sqrt(t.declared_prob * (1 - t.declared_prob) / max(self.n_events, 1))
                   > t.tolerance for t in self.tier_results)

    @property
    def overall_pass(self):
        return (self.mean_ok and self.std_ok and self.range_ok
                and all(t.within_tolerance for t in self.tier_results))

    @property
    def transition_ok(self):
        return abs(self.transition_rate - self.expected_transition_rate) < 5.0

# ── Core analysis ─────────────────────────────────────────────────────────────

def analyse(events_jsonl, profiles_json,
            tolerance_mean=0.15, tolerance_std=0.20, tolerance_tier=0.05):

    data     = json.loads(Path(profiles_json).read_text())
    profiles = data['profiles']
    bindings = data['bindings']
    stream = data.get('stream', {})
    streamed = set(stream.get('types') or [stream.get('event_type', 'sensor_reading')])

    # device → [(field, tier_field, group key)]
    slots = {}
    groups = {}
    for b in bindings:
        entries = []
        for m in b.get('measures', []):
            key = (b['device_class'], m['measure_name'], b.get('zone_id'))
            groups.setdefault(key, {'profile': m['profile'], 'devices': [],
                                    'failure': b.get('sampling', {}).get('failure', 0.0),
                                    'mode': b.get('sampling', {}).get('mode', 'PERIODIC'),
                                    'decay': bool(b.get('decay'))})
            groups[key]['devices'].append(b['device_id'])
            entries.append((m['field'], m.get('tier_field', 'tier'), key))
        slots[b['device_id']] = entries

    values = defaultdict(list)          # key → values
    tiers  = defaultdict(list)          # key → tiers
    seqs   = defaultdict(lambda: defaultdict(list))   # key → device → tier sequence
    with open(events_jsonl) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            ev = json.loads(line)
            if ev.get('event_type') not in streamed or ev.get('triggered_by'):
                continue
            for field, tier_field, key in slots.get(ev.get('device_id'), ()):
                v = ev.get(field)
                if v is None:
                    continue
                t = ev.get(tier_field, 'NORMAL')
                values[key].append(v)
                tiers[key].append(t)
                seqs[key][ev['device_id']].append(t)

    results = []
    for key, g in groups.items():
        device_class, measure_name, zone_id = key
        prof = profiles.get(g['profile'], {})
        vals = values.get(key, [])
        n_events = len(vals)
        if n_events < 5:
            continue
        tds = tier_dicts(prof)
        if not tds:
            continue

        decl_mix_mean = mix_mean(tds)
        decl_mix_std  = mix_std(tds)
        obs_mean      = statistics.mean(vals)
        obs_std       = statistics.stdev(vals) if n_events > 1 else 0.0

        tol_mean = abs(decl_mix_mean) * tolerance_mean if decl_mix_mean != 0 else 1.0
        tol_std  = decl_mix_std * tolerance_std if decl_mix_std > 0 else 1e-9
        mean_ok = abs(obs_mean - decl_mix_mean) <= tol_mean
        std_ok  = abs(obs_std  - decl_mix_std)  <= tol_std

        tier_counts = defaultdict(int)
        for t in tiers[key]: tier_counts[t] += 1
        tier_results = []
        for td in tds:
            op = tier_counts.get(td['name'], 0) / n_events
            tier_results.append(TierResult(
                tier=td['name'], declared_prob=td['prob'], observed_prob=op,
                observed_count=tier_counts.get(td['name'], 0), total_count=n_events,
                within_tolerance=abs(op - td['prob']) <= tolerance_tier,
            ))

        n_trans = n_steps = 0
        for seq in seqs[key].values():
            n_trans += sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
            n_steps += max(len(seq) - 1, 0)
        trans_rate = 100.0 * n_trans / max(n_steps, 1)
        exp_trans  = 100.0 * expected_switch_rate(prof, tds)

        vr = prof.get('valid_range', {})
        rmin, rmax = vr.get('min', float('-inf')), vr.get('max', float('inf'))
        n_out = sum(1 for v in vals if not (rmin <= v <= rmax))

        results.append(MeasureResult(
            device_id=g['devices'][0], device_class=device_class,
            measure_name=measure_name, zone_id=zone_id,
            n_events=n_events, declared_failure=g['failure'],
            observed_mean=obs_mean, declared_mean=decl_mix_mean, mean_ok=mean_ok,
            observed_std=obs_std,   declared_std=decl_mix_std,   std_ok=std_ok,
            tier_results=tier_results,
            n_transitions=n_trans, transition_rate=trans_rate,
            expected_transition_rate=exp_trans,
            n_out_of_range=n_out, range_ok=(n_out == 0),
            n_devices=len(set(g['devices'])),
            sticky=bool(prof.get('chain')),
            arrival_mode=g['mode'],
            decaying=g.get('decay', False),
        ))

    return results

# ── Report renderer ───────────────────────────────────────────────────────────

def print_report(results: List[MeasureResult]):
    G='\033[92m'; R='\033[91m'; Y='\033[93m'; B='\033[1m'; D='\033[2m'; X='\033[0m'
    ok = lambda f: f'{G}PASS{X}' if f else f'{R}FAIL{X}'

    print(f'\n{B}{"═"*80}{X}')
    print(f'{B}  CORRECTNESS REPORT  —  Observed vs Declared{X}')
    print(f'{B}{"═"*80}{X}')
    print(f'  {D}Pooled over every device of a class/measure/zone. Std compared against mixture std (all tiers).{X}')

    by_measure = defaultdict(list)
    for r in results: by_measure[r.measure_name].append(r)

    total_pass = total_fail = n_skipped = 0

    for measure, mrs in sorted(by_measure.items()):
        mrs = sorted(mrs, key=lambda x: str(x.zone_id))
        print(f'\n{B}  ── {measure} ──{X}')

        # Distribution table
        print(f'  {"Zone":>4}  {"n":>7}  {"Obs μ":>7} {"Mix μ":>7}  '
              f'{"Obs σ":>6} {"Mix σ":>6}  {"μ":>4} {"σ":>4} {"Rng":>4} {"✓":>4}')
        print(f'  {"─"*4}  {"─"*7}  {"─"*7} {"─"*7}  {"─"*6} {"─"*6}  '
              f'{"─"*4} {"─"*4} {"─"*4} {"─"*4}')
        for r in mrs:
            n_txt = f'{r.n_events:,}' + ('*' if r.low_power else '')
            if not r.applicable:
                n_skipped += 1
                print(f'  {str(r.zone_id):>4}  {n_txt:>7}  '
                      f'{r.observed_mean:>7.2f} {r.declared_mean:>7.2f}  '
                      f'{r.observed_std:>6.2f} {r.declared_std:>6.2f}  '
                      f'{D}n/a — {r.skip_reason}{X}')
                continue
            total_pass += r.overall_pass
            total_fail += (not r.overall_pass)
            print(f'  {str(r.zone_id):>4}  {n_txt:>7}  '
                  f'{r.observed_mean:>7.2f} {r.declared_mean:>7.2f}  '
                  f'{r.observed_std:>6.2f} {r.declared_std:>6.2f}  '
                  f'{ok(r.mean_ok):>4} {ok(r.std_ok):>4} '
                  f'{ok(r.range_ok):>4} {ok(r.overall_pass):>4}')

        # Tier rates table
        print()
        print(f'  {"Zone":>4}  {"NORMAL  obs/decl":>20}  '
              f'{"ABOVE  obs/decl":>18}  {"BELOW  obs/decl":>18}  '
              f'{"Transitions  obs/exp":>24}')
        print(f'  {"─"*4}  {"─"*20}  {"─"*18}  {"─"*18}  {"─"*24}')

        for r in mrs:
            td = {t.tier: t for t in r.tier_results}
            def tfmt(tier, width=18):
                if tier not in td: return f'{"—":{width}}'
                t = td[tier]
                s = f'{t.observed_prob*100:4.1f}% / {t.declared_prob*100:4.1f}%'
                c = G if t.within_tolerance else R
                return f'{c}{s:{width}}{X}'
            tc = G if r.transition_ok else Y
            ts = f'{tc}{r.n_transitions:,} ({r.transition_rate:.1f}%/{r.expected_transition_rate:.1f}%){X}'
            print(f'  {str(r.zone_id):>4}  {tfmt("NORMAL",20):>20}  '
                  f'{tfmt("ABOVE"):>18}  {tfmt("BELOW"):>18}  {ts}')

    n_total = total_pass + total_fail
    print(f'\n{B}{"═"*80}{X}')
    print(f'{B}  SUMMARY  ({n_total} device/zone combinations){X}')
    print(f'  PASS : {G}{total_pass}{X}  ({100*total_pass//max(n_total,1)}%)')
    print(f'  FAIL : {(R if total_fail else "")}{total_fail}{(X if total_fail else "")}')
    if n_skipped:
        print(f'  Not checked: {n_skipped} (ON_CHANGE / ON_THRESHOLD arrivals, DECAY batteries)')
    if any(r.low_power for r in results):
        print(f'  * small sample: ±5 pp is within 2 standard errors of a tier rate, '
              f'so a tier FAIL there can be chance')
    print(f'\n  Tolerances: mean ±15% of mix μ  |  std ±20% of mix σ  |  tier ±5 pp')
    print(f'{"═"*80}\n')


def save_json(results: List[MeasureResult], path: str):
    out = [{
        'device_id':    r.device_id,
        'device_class': r.device_class,
        'measure_name': r.measure_name,
        'zone_id':      r.zone_id,
        'n_events':     r.n_events,
        'n_devices':    r.n_devices,
        'sticky':       r.sticky,
        'mean':   {'observed': round(r.observed_mean,4), 'declared': round(r.declared_mean,4), 'pass': r.mean_ok},
        'std':    {'observed': round(r.observed_std,4),  'declared': round(r.declared_std,4),  'pass': r.std_ok},
        'tiers':  [{'tier': t.tier, 'observed_prob': round(t.observed_prob,4),
                    'declared_prob': round(t.declared_prob,4), 'pass': t.within_tolerance,
                    'deviation_pp': round(t.deviation_pp,2)} for t in r.tier_results],
        'transitions': {'count': r.n_transitions, 'observed_rate': round(r.transition_rate,2),
                        'expected_rate': round(r.expected_transition_rate,2), 'pass': r.transition_ok},
        'range':  {'n_out_of_range': r.n_out_of_range, 'pass': r.range_ok},
        'overall_pass': r.overall_pass,
        'applicable':   r.applicable,
        'arrival_mode': r.arrival_mode,
        'decaying': r.decaying,
        'small_sample': r.low_power,
    } for r in results]
    with open(path, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'[Correctness] Saved → {path}')
