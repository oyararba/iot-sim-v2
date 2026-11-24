"""
compiler.py

Main entrypoint for the IoT schema compiler.

Usage:
    python compiler.py --schema agriculture.sql --rules agriculture.iotdl
                       --db resources.db --output out/
    python compiler.py --schema agriculture.sql --devices resources.json ...

Pipeline:
    1. Lex + Parse schema file   → AST
    2. Lex + Parse rules file    → rule AST nodes appended
    3. Semantic validation        → error/warn report
    4. Load RESOURCES             → device rows from the enterprise DB
                                    (--db SQLite or --devices JSON), read
                                    through the schema's CREATE TABLEs;
                                    user-defined fields (zone ENUM *) resolved
    5. Code generation            → profiles.json + bindings
    6. Run simulation             → events.jsonl
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lexer.tokenizer    import Lexer
from parser.parser      import Parser
from validator.semantic import SemanticValidator
from codegen.generator  import CodeGenerator
from resources.loader   import ResourceError, load_resources
from engine.fast_simulator import FastSimulator


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def parse_schema(text: str):
    tokens  = Lexer(text).tokenize()
    program = Parser(tokens).parse()
    return program


def report_resource_errors(exc: ResourceError, stage: str):
    print(f'\n[Compiler] {len(exc.errors)} resource error(s) {stage} — compilation aborted.')
    for msg in exc.errors:
        print(f'  ERROR: {msg}')
    sys.exit(1)


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(description='IoT Schema Compiler & Simulator')
    p.add_argument('--schema',    required=False, default=None, help='Schema file (.sql)')
    p.add_argument('--rules',     required=False, default=None,
                   help='IoTDL rules file (.iotdl)')
    p.add_argument('--db',        required=False, default=None,
                   help='Enterprise DB (SQLite file) holding the tables the '
                        'schema declares with CREATE TABLE')
    p.add_argument('--devices',   required=False, default=None,
                   help='Resource data as JSON: {"<table>": [ {row}, ... ], ...} '
                        'for the tables the schema declares')
    p.add_argument('--sticky-persistence', type=float, default=0.9,
                   help='STICKY tiers: chance the tier holds for the next reading '
                        '(default 0.9)')
    p.add_argument('--sticky-ramp', type=int, default=5,
                   help='STICKY tiers: readings over which the mean moves to a new '
                        'tier (default 5; 0 = jump)')
    p.add_argument('--output',    default='out',
                   help='Output directory (default: out/)')
    p.add_argument('--sim-start', type=int,   default=0,
                   help='Simulation start in --time-unit units (default: 0)')
    p.add_argument('--sim-end',   type=int,   default=None,
                   help='Simulation end in --time-unit units (default: 90; '
                        'no time limit when --num-events is given)')
    p.add_argument('--num-events', type=int, default=None, metavar='N',
                   help='Stop after exactly N events (sensor readings + rule events, '
                        'i.e. N lines of events.jsonl). Alone: no time limit. With '
                        '--sim-end: whichever comes first.')
    p.add_argument('--time-unit', default='days',
                   choices=['seconds', 'minutes', 'hours', 'days'],
                   help='Unit for sim-start and sim-end (default: days)')
    p.add_argument('--tick-unit', default='minutes',
                   choices=['seconds', 'minutes', 'hours'],
                   help='Resolution of one simulation tick (default: minutes)')
    p.add_argument('--seed',      type=int,   default=None)
    p.add_argument('--verbose',   type=int,   default=0,
                   help='Print progress every N ticks')
    p.add_argument('--validate-only', action='store_true',
                   help='Run validation only, do not simulate')
    p.add_argument('--list-quantities', action='store_true',
                   help='Print the measurement ontology (quantities and their units) '
                        'and exit')
    return p


def main():
    ap      = build_arg_parser()
    args    = ap.parse_args()
    if args.list_quantities:
        from ontology import default_ontology
        print(default_ontology().describe())
        return
    if not args.schema:
        ap.error('--schema is required')
    if args.num_events is not None and args.num_events < 1:
        ap.error('--num-events must be at least 1')
    if args.sim_end is None and args.num_events is None:
        args.sim_end = 90                       # the default run: 90 days
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Parse ────────────────────────────────────────────────────────
    print('[Compiler] Parsing schema …')
    schema_text = Path(args.schema).read_text()
    program     = parse_schema(schema_text)

    # Append rules if provided
    if args.rules:
        print('[Compiler] Parsing rules …')
        rules_text   = Path(args.rules).read_text()
        rules_prog   = parse_schema(rules_text)
        program.rules.extend(rules_prog.rules)

    print(f'  Device classes : {len(program.device_classes)}')
    print(f'  Tables         : {len(program.tables)}')
    print(f'  Event types    : {len(program.event_types)}')
    print(f'  Event streams  : {len(program.event_streams)}')
    print(f'  Distributions  : {len(program.distributions)}')
    print(f'  Rules          : {len(program.rules)}')

    # ── 2. Validate ─────────────────────────────────────────────────────
    print('\n[Compiler] Validating …')
    validator = SemanticValidator(program)
    errors    = validator.validate()

    for err in errors:
        prefix = '  ERROR' if err.severity == 'ERROR' else '  WARN '
        print(f'{prefix} (L{err.line}): {err.message}')

    if validator.has_errors:
        print(f'\n[Compiler] {sum(1 for e in errors if e.severity=="ERROR")} error(s) — '
              f'compilation aborted.')
        sys.exit(1)
    else:
        print(f'  Validation passed '
              f'({sum(1 for e in errors if e.severity=="WARN")} warnings)')

    # ── 3. Load RESOURCES (enterprise DB) ───────────────────────────────
    if args.db and args.devices:
        print('\n[Compiler] Give either --db or --devices, not both.')
        sys.exit(1)
    if not (args.db or args.devices):
        if args.validate_only:
            print('\n[Compiler] No --db / --devices given: resource data not checked.')
            return
        print('\n[Compiler] No resource data. Pass --db <enterprise>.db (SQLite) or '
              '--devices <resources>.json; devices are no longer invented.')
        sys.exit(1)

    print('\n[Compiler] Loading resources …')
    try:
        resources = load_resources(program, db=args.db, json_path=args.devices)
    except ResourceError as exc:
        report_resource_errors(exc, 'while loading resources')

    print(f'  Source        : {resources.source}')
    for tname, n in resources.row_counts.items():
        print(f'  {tname:<14}: {n} rows')
    print(f'  Device table  : {resources.device_table} '
          f'(id {resources.id_column}, class {resources.class_column})')
    for fname, path in resources.paths.items():
        print(f'  {fname + " ENUM *":<14}← {path.describe(resources.device_table)}')
    for w in resources.warnings:
        print(f'  WARN: {w}')

    # ── 4. Code generation ───────────────────────────────────────────────
    # Convert sim duration to ticks
    time_unit_to_min  = {'seconds': 1/60, 'minutes': 1, 'hours': 60, 'days': 1440}
    tick_unit_to_min  = {'seconds': 1/60, 'minutes': 1, 'hours': 60}
    sim_start_min     = args.sim_start * time_unit_to_min[args.time_unit]
    min_per_tick      = tick_unit_to_min[args.tick_unit]
    start_tick        = int(sim_start_min / min_per_tick)
    end_tick          = None                # None: no time limit (count-only run)
    if args.sim_end is not None:
        sim_duration_min = args.sim_end * time_unit_to_min[args.time_unit]
        end_tick         = int(sim_duration_min / min_per_tick)
    ms_per_tick       = int(min_per_tick * 60 * 1000)

    sim_cfg = {
        'start_time':  start_tick,
        'end_time':    end_tick,
        'max_events':  args.num_events,
        'time_unit':   'ticks',
        'tick_unit':   args.tick_unit,
        'ms_per_tick': ms_per_tick,
        'seed':        args.seed,
        'sticky_persistence': args.sticky_persistence,
        'sticky_ramp': args.sticky_ramp,
    }

    import random
    if args.seed is not None:
        random.seed(args.seed)

    print('\n[Compiler] Binding devices and generating profiles …')
    try:
        codegen  = CodeGenerator(program=program, resources=resources,
                                 simulation_cfg=sim_cfg)
        compiled = codegen.generate()
    except ResourceError as exc:
        report_resource_errors(exc, 'while binding devices')
    for w in codegen.warnings:
        print(f'  WARN: {w}')

    n_devices = len({b['device_id'] for b in compiled['bindings']})
    print(f'  Devices bound      : {n_devices} of {len(resources.devices)}')
    print(f'  Profiles generated : {len(compiled["profiles"])}')
    print(f'  Rules compiled     : {len(compiled["rules"])}')

    if args.validate_only:
        print('\n[Compiler] Resources OK (validate-only: not simulating).')
        return

    n = args.num_events
    if end_tick is None:
        print(f'\n[Compiler] Simulation: from {args.sim_start} {args.time_unit} until '
              f'{n:,} events (no time limit)')
    else:
        print(f'\n[Compiler] Simulation: {args.sim_start}-{args.sim_end} {args.time_unit}'
              + (f', or {n:,} events if that comes first' if n else ''))
    print(f'           Tick unit : 1 {args.tick_unit} = {ms_per_tick} ms/tick')
    if end_tick is not None:
        print(f'           Total ticks: {end_tick - start_tick:,}')

    # Write profiles.json
    profiles_path = out_dir / 'profiles.json'
    profiles_path.write_text(json.dumps(compiled, indent=2))
    print(f'  profiles.json → {profiles_path}')

    # ── 5. Simulate ──────────────────────────────────────────────────────
    print('\n[Compiler] Running simulation …')
    events_path = out_dir / 'events.jsonl'

    with open(events_path, 'w') as f:
        def write_event(ev):
            d = ev if isinstance(ev, dict) else ev.to_dict()
            f.write(json.dumps(d) + '\n')

        sim = FastSimulator(compiled=compiled, ms_per_tick=ms_per_tick,
                            verbose=args.verbose)
        sim.run(output_fn=write_event)

    print(f'\n  events.jsonl → {events_path}')
    print('\n[Compiler] Done.')


if __name__ == '__main__':
    main()
