"""
codegen/rules.py

Compiles IoTDL rules into the structures the engine executes, and reports
every problem it finds (the validator calls the same function, so the
checks and the compiled meaning can never drift apart).

Semantics (README §6.5, §8.3, slides):

  new <event>[<window>](<head>) @ (<fire time>) :- <body>;

  Window
    tumbling(s, N)      non-overlapping windows of N ticks (or a duration:
                        tumbling(s, 30 MIN)); evaluated when a window closes;
                        s = the window's first tick
    sliding(s, N)       the N most recent matching readings per group;
                        evaluated on each matching event; s = the event time
                        of the oldest reading in the window
    sliding(s, 6 HOURS) the matching readings of the last 6 hours per group;
                        s = the window's first tick
    (none)              fires on each matching event; the body's time
                        variable (`@ T`) is the event time

  Body
    Source clauses bind an event's attributes by position, in the order
    its EVENTTYPE declares them (EVENTTIME / INGESTTIME excluded). A clause
    may list fewer arguments than the event has; the rest are `_`.
    Constraints (`value < 25`) filter the matching events. Several clauses
    (tumbling windows only) join on shared variables, the time variable
    included.

  Constants vs variables (the convention every spec-sheet example follows)
    An identifier written in capitals, two or more characters long
    (DROUGHT_STRESS, MOISTURE, AIR_TEMP), or naming a declared measure
    (Moisture) is a constant; other identifiers
    (device_id, value, z) are variables, and so are the window variable and
    the time variables after `@`, whatever their case (`@ T`). Numbers and
    "strings" are constants. String constants match case- and
    underscore-insensitively, so MOISTURE, Moisture and moisture are the
    same measure, and AIR_TEMP is AirTemp.

  Head
    Aggregation outputs `(value = avg(value))` fill the head event's
    attribute of that name; the other head arguments fill the remaining
    attributes in declared order. A rule without a window fills any other
    attribute from a body variable of the same name.

  Fire time
    @ (s + 31 MIN) — the window variable (or, without a window, the body's
    time variable) plus offsets; bare numbers are ticks. A fire time earlier
    than the event or window close that produced it is moved up to that
    moment: events are never emitted into the past.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ast_nodes.nodes import Program, RuleNode, Term

AGG_FUNCS = {'avg', 'min', 'max', 'sum', 'count', 'last'}
MEASURE_ATTRS = ('measure_type', 'measure_name', 'measure')
_CAPS = re.compile(r'^[A-Z][A-Z0-9_]+$')


def norm_const(value: Any) -> Any:
    """Comparison form: numbers as float, strings case/underscore-insensitive."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return str(value).replace('_', '').casefold()


def event_attributes(program: Program) -> Dict[str, List[str]]:
    """EVENTTYPE name → positional attribute names (EVENTTIME/INGESTTIME excluded)."""
    return {et.name: [a.name for a in et.attributes] for et in program.event_types}


def measure_names(program: Program) -> Dict[Any, str]:
    """normalised measure name → declared measure name"""
    return {norm_const(m.name): m.name
            for dc in program.device_classes for m in dc.measures}


def rule_label(rule: RuleNode, index: int, measures: Optional[Dict[Any, str]] = None) -> str:
    """alert(DROUGHT_STRESS), metric_avg(Moisture), or report#3 when the head has no constant."""
    measures = measures or {}
    consts = []
    for t in rule.head_args:
        if t.kind == 'const':
            consts.append(str(t.value))
        elif t.kind == 'ident' and norm_const(t.value) in measures:
            consts.append(measures[norm_const(t.value)])
        elif t.kind == 'ident' and _CAPS.match(str(t.value)):
            consts.append(str(t.value))
    return f'{rule.head_event}({consts[0]})' if consts else f'{rule.head_event}#{index + 1}'


def _classify(rule: RuleNode, measures: Dict[Any, str]) -> Dict[str, str]:
    """identifier → 'var' | 'const' for every identifier in the rule."""
    always_vars = {src.time_var for src in rule.sources}
    if rule.window is not None:
        always_vars.add(rule.window.slide_var)

    names = set(always_vars)
    for src in rule.sources:
        names.update(a.value for a in src.args if a.kind == 'ident')
    names.update(t.value for t in rule.head_args if t.kind == 'ident')
    names.update(c.rhs.value for c in rule.constraints if c.rhs.kind == 'ident')
    names.update(c.lhs for c in rule.constraints)

    kinds: Dict[str, str] = {}
    for name in names:
        if name in always_vars:
            kinds[name] = 'var'
        elif _CAPS.match(name) or norm_const(name) in measures:
            kinds[name] = 'const'
        else:
            kinds[name] = 'var'
    return kinds


def compile_rule(rule: RuleNode, index: int, program: Program,
                 ms_per_tick: Optional[float] = None,
                 streamed_type: Optional[str] = None
                 ) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """
    Returns (compiled rule, errors, warnings). With ms_per_tick=None the
    durations are checked but not converted (validation only).
    """
    errors: List[str] = []
    warnings: List[str] = []
    attrs = event_attributes(program)
    measures = measure_names(program)
    label = rule_label(rule, index, measures)
    where = f"Rule {label} (line {rule.line})" if rule.line else f"Rule {label}"
    kinds = _classify(rule, measures)

    def const_value(term: Term) -> Any:
        """The constant as written, or the declared measure name it refers to."""
        v = term.value
        if isinstance(v, str) and norm_const(v) in measures:
            return measures[norm_const(v)]
        return v

    # ── window ─────────────────────────────────────────────────────────
    window = None
    w = rule.window
    if w is not None:
        if w.size <= 0:
            errors.append(f'{where}: window size must be > 0')
        if w.size_ms is None and float(w.size) != int(w.size):
            errors.append(f'{where}: a window size without a unit must be a whole number')
        window = {'type': w.window_type, 'var': w.slide_var,
                  'count': None, 'ticks': None, 'size_ms': w.size_ms}
        if w.window_type == 'sliding' and w.size_ms is None:
            window['count'] = int(w.size)           # N most recent readings
        elif w.size_ms is not None:
            if ms_per_tick:
                ticks = w.size_ms / ms_per_tick
                if ticks < 1:
                    errors.append(f'{where}: window of {w.size_ms/1000:g} s is shorter '
                                  f'than one tick')
                elif abs(ticks - round(ticks)) > 1e-9:
                    warnings.append(f'{where}: window is {ticks:g} ticks; rounded to '
                                    f'{round(ticks)}')
                window['ticks'] = max(1, int(round(ticks)))
        else:
            window['ticks'] = int(w.size)            # tumbling: bare N = ticks

    # ── sources ────────────────────────────────────────────────────────
    if not rule.sources:
        errors.append(f'{where}: the body has no source clause')
    sources = []
    bound: Dict[str, int] = {}          # variable → first source index binding it
    for si, src in enumerate(rule.sources):
        if src.event_name not in attrs:
            errors.append(f"{where}: source event type '{src.event_name}' is not declared")
            continue
        names = attrs[src.event_name]
        if len(src.args) > len(names):
            errors.append(f"{where}: {src.event_name}(...) has {len(src.args)} arguments but "
                          f"{src.event_name} declares {len(names)} attributes "
                          f"({', '.join(names)})")
            continue
        pattern = []
        for attr, term in zip(names, src.args):
            if term.kind == 'wild':
                continue
            if term.kind == 'ident' and kinds.get(term.value) == 'var':
                pattern.append([attr, 'var', term.value])
                bound.setdefault(term.value, si)
                continue
            value = const_value(term)
            if attr in MEASURE_ATTRS and src.event_name == streamed_type \
                    and norm_const(value) not in measures:
                errors.append(f"{where}: '{term.value}' in {src.event_name}.{attr} is not "
                              f"a declared measure")
            pattern.append([attr, 'const', norm_const(value)])
        bound.setdefault(src.time_var, si)
        sources.append({'event_type': src.event_name, 'time_var': src.time_var,
                        'pattern': pattern, 'local_constraints': []})

    # ── constraints ───────────────────────────────────────────────────
    join_constraints = []
    for c in rule.constraints:
        if c.lhs not in bound:
            errors.append(f"{where}: constraint on '{c.lhs}', which no source clause binds")
            continue
        if c.rhs.kind == 'ident' and kinds.get(c.rhs.value) == 'var':
            if c.rhs.value not in bound:
                errors.append(f"{where}: constraint compares with '{c.rhs.value}', which no "
                              f"source clause binds")
                continue
            rhs = {'kind': 'var', 'value': c.rhs.value}
        elif c.rhs.kind == 'wild':
            errors.append(f"{where}: '_' cannot be compared")
            continue
        else:
            rv = const_value(c.rhs)
            rhs = {'kind': 'const', 'value': rv if isinstance(rv, (int, float)) else str(rv)}
        con = {'lhs': c.lhs, 'op': c.op, 'rhs': rhs}
        srcs = {bound[c.lhs]} | ({bound[rhs['value']]} if rhs['kind'] == 'var' else set())
        if len(srcs) == 1 and len(sources) > next(iter(srcs)):
            sources[next(iter(srcs))]['local_constraints'].append(con)
        else:
            join_constraints.append(con)

    # ── head ───────────────────────────────────────────────────────────
    head_attrs = attrs.get(rule.head_event)
    head = []
    group_vars = []
    if head_attrs is None:
        errors.append(f"{where}: head event type '{rule.head_event}' is not declared")
    else:
        agg_out = [a.output_var for a in rule.agg_functions]
        for out in agg_out:
            if out not in head_attrs:
                errors.append(f"{where}: aggregation output '{out}' is not an attribute "
                              f"of {rule.head_event} ({', '.join(head_attrs)})")
        remaining = [a for a in head_attrs if a not in agg_out]
        if len(rule.head_args) > len(remaining):
            errors.append(f"{where}: {len(rule.head_args)} head arguments but "
                          f"{rule.head_event} has only {len(remaining)} attributes left "
                          f"for them ({', '.join(remaining)})")
        for attr, term in zip(remaining, rule.head_args):
            if term.kind == 'wild':
                continue
            if term.kind == 'ident' and kinds.get(term.value) == 'var':
                if term.value not in bound:
                    errors.append(f"{where}: head variable '{term.value}' is not bound by "
                                  f"the body")
                    continue
                head.append({'attr': attr, 'kind': 'var', 'value': term.value})
                group_vars.append(term.value)
            else:
                head.append({'attr': attr, 'kind': 'const', 'value': const_value(term)})
        for a in rule.agg_functions:
            head.append({'attr': a.output_var, 'kind': 'agg', 'value': a.output_var})

    aggs = []
    for a in rule.agg_functions:
        if a.func not in AGG_FUNCS:
            errors.append(f"{where}: unknown aggregation '{a.func}' "
                          f"(use {', '.join(sorted(AGG_FUNCS))})")
        if a.input_var not in bound:
            errors.append(f"{where}: aggregation input '{a.input_var}' is not bound "
                          f"by the body")
        aggs.append({'out': a.output_var, 'func': a.func, 'in': a.input_var})

    # ── structural checks ─────────────────────────────────────────────
    if w is None and rule.agg_functions:
        errors.append(f'{where}: aggregations need a window')
    if len(rule.sources) > 1 and (w is None or w.window_type != 'tumbling'):
        errors.append(f'{where}: joining several source clauses needs a tumbling window')
    if streamed_type and rule.head_event == streamed_type:
        warnings.append(f'{where}: writes {streamed_type}, the event type sensors transmit')

    # ── fire time ─────────────────────────────────────────────────────
    ft = rule.fire_time
    offset_ticks = 0.0
    if ft is None:
        errors.append(f'{where}: missing @ (<fire time>)')
        fire_var = None
    else:
        fire_var = ft.var
        if w is not None and ft.var != w.slide_var:
            errors.append(f"{where}: fire time uses '{ft.var}'; a windowed rule's fire "
                          f"time is relative to its window variable '{w.slide_var}'")
        if w is None and ft.var not in {s.time_var for s in rule.sources}:
            errors.append(f"{where}: fire time uses '{ft.var}', which is not the body's "
                          f"time variable")
        for sign, num, unit_ms in ft.offsets:
            if unit_ms is None:
                offset_ticks += sign * num
            elif ms_per_tick:
                offset_ticks += sign * num * unit_ms / ms_per_tick

    if errors:
        return None, errors, warnings

    return {
        'label':            label,
        'head_event':       rule.head_event,
        'window':           window,
        'sources':          sources,
        'join_constraints': join_constraints,
        'group_vars':       group_vars,
        'head':             head,
        'aggs':             aggs,
        'fill_same_name':   w is None,
        'fire':             {'var': fire_var, 'offset_ticks': offset_ticks,
                             'text': ft.text if ft else ''},
    }, errors, warnings
