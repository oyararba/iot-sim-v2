# IoTSim

**A general framework for generating pseudo-real IoT sensor streams with causal event chains.**

You describe a deployment in a small SQL-like language: what the sensors measure, how their values are distributed, how often they transmit, and which alerts follow from which readings. IoTSim compiles that description, combines it with a list of the devices that exist, and writes a reproducible stream of events.

This page covers how to run IoTSim and how to read what it produces. The language itself is specified in [docs/REFERENCE.md](docs/REFERENCE.md).

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [The three inputs](#the-three-inputs)
- [Run with a database (SQLite)](#run-with-a-database-sqlite)
- [Run without a database (JSON)](#run-without-a-database-json)
- [Check a schema without simulating](#check-a-schema-without-simulating)
- [Options](#options)
- [Reading the output](#reading-the-output)
- [Checking that the output is statistically right](#checking-that-the-output-is-statistically-right)
- [Tests and benchmarks](#tests-and-benchmarks)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)

## Requirements

- Python 3.9 or newer (verified on 3.10). The compiler and engine use only the standard library, so there is nothing to install.
- Optional: `matplotlib`, only for `tools/benchmark.py --plot`.
- No database server. "Database" here always means a SQLite file, which Python reads natively.

## Quick start

```bash
git clone https://github.com/oyararba/iot-sim-v2.git
cd iot-sim-v2/compiler

python3 compiler.py \
    --schema  examples/precision_agriculture.sql \
    --db      examples/agriculture_resources.db \
    --sim-end 7 --time-unit days --tick-unit minutes \
    --seed    42 \
    --output  out/
```

This simulates 48 farm sensors for 7 days in about a second and writes two files:

| File | Contents |
|---|---|
| `out/events.jsonl` | The event stream, one JSON object per line (about 83,000 events) |
| `out/profiles.json` | The compiled model the engine ran: every device, its distributions, the rules |

Run all commands on this page from the `compiler/` directory.

## The three inputs

| Input | Flag | What it is |
|---|---|---|
| Schema | `--schema file.sql` | Device classes, event types, the stream, the distributions, and the `CREATE TABLE` declarations that describe the resource tables. Required. |
| Rules | `--rules file.iotdl` | Rules that derive events (averages, alerts, reports) from readings. Optional; rules may also sit in the schema file. |
| Resource data | `--db file.db` or `--devices file.json` | The actual rows: which devices exist, which class each one is, and where it is deployed. Exactly one of the two is required to simulate. |

The schema says what a `SoilMoistureSensor` is and how moisture behaves in zone 1. The resource data says that device `SM_N1_15` is a `SoilMoistureSensor` in plot 11, and that plot 11 is in zone 1. The compiler joins the two, so each device gets the distribution for its own zone.

The schema and the resource data must agree. Two matching sets ship in `examples/`:

| Schema | Rules | Resource data |
|---|---|---|
| `precision_agriculture.sql` (12 device classes, 3 zones) | 22 rules, in the same file | `agriculture_resources.db` or `agriculture_resources.json` (48 devices) |
| `agriculture.sql` (4 device classes, 2 zones) | `agriculture.iotdl` (6 rules) | Generated, see [Generating devices](#generating-devices) |

## Run with a database (SQLite)

### Using the example database

The quick start above does exactly this. The part of the console output to look at is the resource section:

```
[Compiler] Loading resources …
  Source        : agriculture_resources.db
  zones         : 3 rows
  plots         : 5 rows
  devices       : 48 rows
  Device table  : devices (id device_id, class device_class)
  zone ENUM *   ← devices.plot_id → plots.zone_id
```

The last line shows how each device's zone was found: follow `devices.plot_id` to the `plots` table and read `zone_id` there.

### Using your own database

Point `--db` at any SQLite file that contains the tables your schema declares with `CREATE TABLE`.

- Table and column names are matched case-insensitively. Views work as well as tables.
- Only declared columns are read. Extra columns and extra tables in the database are ignored.
- A declared column that is missing from the database is an error.
- The file is opened read-only.

The schema must mark the device table and its key so the compiler can find them:

```sql
CREATE TABLE plots   ( plot_id INTEGER PRIMARY KEY,
                       zone_id INTEGER REFERENCES zones(zone_id) )
CREATE TABLE devices ( device_id    STRING  PRIMARY KEY,               -- the device id
                       device_class STRING  REFERENCES DEVICE_CLASS,   -- marks this as the device table
                       plot_id      INTEGER REFERENCES plots(plot_id) )
```

If your data is in PostgreSQL, MySQL or another server, export the relevant tables to SQLite or to JSON first; IoTSim does not connect to database servers.

### Building a database from JSON

`tools/build_resources_db.py` creates a SQLite file whose tables come from the schema's own `CREATE TABLE` blocks and fills it from a JSON file:

```bash
python3 tools/build_resources_db.py \
    --schema examples/precision_agriculture.sql \
    --json   examples/agriculture_resources.json \
    --db     my_resources.db
```

The target file is overwritten if it exists.

## Run without a database (JSON)

Replace `--db` with `--devices` and pass the rows as JSON. Nothing else changes:

```bash
python3 compiler.py \
    --schema  examples/precision_agriculture.sql \
    --devices examples/agriculture_resources.json \
    --sim-end 7 --time-unit days --tick-unit minutes \
    --seed    42 \
    --output  out/
```

With the same seed, the JSON run and the SQLite run produce byte-identical `events.jsonl`.

The JSON file is one object keyed by table name, each holding a list of rows:

```json
{
  "zones":   [ {"zone_id": 1, "zone_name": "North Field", "zone_type": "IRRIGATED"} ],
  "plots":   [ {"plot_id": 11, "zone_id": 1, "crop_type": "wheat"} ],
  "devices": [ {"device_id": "SM_N1_15", "device_class": "SoilMoistureSensor",
                "plot_id": 11, "depth_cm": 15} ]
}
```

Keys that the schema does not declare are ignored, and a declared column that a row leaves out is read as NULL.

### Generating devices

To get a device list of any size without writing one, use the generator. It produces the four device classes of `agriculture.sql`, which defines zones 1 and 2, so pass `--zones 2`:

```bash
python3 tools/generate_devices.py --devices 200 --zones 2 --output out/devices_200.json

python3 compiler.py \
    --schema  examples/agriculture.sql \
    --rules   examples/agriculture.iotdl \
    --devices out/devices_200.json \
    --sim-end 1 --time-unit days --tick-unit minutes \
    --seed    42 \
    --output  out/
```

## Check a schema without simulating

```bash
# Syntax and semantics only
python3 compiler.py --schema examples/precision_agriculture.sql --validate-only

# Also check that every device in the resource data can be bound
python3 compiler.py --schema examples/precision_agriculture.sql \
    --db examples/agriculture_resources.db --validate-only
```

No output files are written. The exit code is 0 on success and 1 on any error.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--schema PATH` | required | Schema file |
| `--rules PATH` | none | Rules file, added to any rules in the schema |
| `--db PATH` | none | SQLite resource data |
| `--devices PATH` | none | JSON resource data (give `--db` or `--devices`, not both) |
| `--output DIR` | `out` | Where `profiles.json` and `events.jsonl` go |
| `--sim-start N` | `0` | Start of the simulation, in `--time-unit` |
| `--sim-end N` | `90` | End of the simulation, in `--time-unit` |
| `--time-unit` | `days` | Unit of the two values above: `seconds`, `minutes`, `hours`, `days` |
| `--tick-unit` | `minutes` | Length of one simulation tick: `seconds`, `minutes`, `hours` |
| `--seed N` | none | Random seed. Without it, every run is different. |
| `--sticky-persistence F` | `0.9` | Chance that a `STICKY` tier holds for the next reading |
| `--sticky-ramp N` | `5` | Readings over which the mean moves when entering or leaving a `STICKY` tier |
| `--verbose N` | `0` | Print progress every N ticks |
| `--validate-only` | off | Check and stop |
| `--list-quantities` | off | Print the measurement ontology (every quantity `MEASURES` accepts, with its units) and stop |

Two of these deserve care:

- **`--seed`**: pass one whenever you want to reproduce a run. The same seed and inputs give the same file.
- **`--tick-unit`**: the tick is the smallest step of simulated time, and every timestamp in the output is counted in ticks. Set it to the shortest interval in the schema. Both examples have 1-minute sensors, so they use `minutes`. A tick longer than a rule window is a compile error; a tick longer than a sensor interval rounds that interval up to one tick, with a warning.

## Reading the output

### The console summary

```
[FastSim] Ticks 0→10080 | Devices: 48 | Rules: 22 | ms/tick: 60000
[FastSim] Done: 83,108 events in 0.95s (87,028 ev/s)
  Persistent : 63,488  Triggered  : 19,620
  Dropped (failure): 2,591  Dropped (no change): 0
```

| Line | Meaning |
|---|---|
| `Ticks 0→10080` | 7 days at one tick per minute |
| `Persistent` | Sensor readings written to the stream |
| `Triggered` | Events produced by rules (averages, alerts, reports) |
| `Dropped (failure)` | Readings lost to the `FAILURE` probability in `ARRIVALS`, modelling network loss. They are not in the file. |
| `Dropped (no change)` | Samples that `ON_CHANGE` or `ON_THRESHOLD` sensors took but had no reason to transmit |
| `Triggered events due after the end (not written)` | Rule results whose fire time fell past `--sim-end`. Expected for windows that close at the very end. |
| `Stale`, `Sensors failed` | Shown only when the schema uses `INGESTTIME` or a `FAILURE <duration>` sensor lifetime |
| `Batteries run down (DECAY)` | Shown only when a device class has `DECAY`: devices whose battery emptied and that stopped transmitting |

### events.jsonl

One event per line, ordered by `event_time`. A sensor reading:

```json
{"event_type": "sensor_reading", "event_time": 0, "device_id": "NPK_N1",
 "measure_type": "Nitrogen", "value": 196.0, "value2": 92.0, "value3": 163.0,
 "depth_cm": 20, "tier": "NORMAL", "tier2": "NORMAL", "tier3": "NORMAL"}
```

| Field | Meaning |
|---|---|
| `event_type` | `sensor_reading` for readings; otherwise the event type a rule produced |
| `event_time` | Time in ticks since the start of the simulation. With minute ticks, `1440` is the start of day 2. |
| `device_id` | The device, as named in the resource data |
| `measure_type` | What was measured. For a multi-measure device, the name of its first measure. |
| `value`, `value2`, `value3` | The readings, in the order the device class lists its `MEASURES`. Here: nitrogen, phosphorus, potassium. Single-measure devices have only `value`. |
| `depth_cm` and similar | Copied from the device's row, for every event attribute named like a device-table column |
| `tier`, `tier2`, `tier3` | Which part of the distribution each value was drawn from: `NORMAL`, `ABOVE` (anomalously high) or `BELOW` (anomalously low) |
| `ingest_time`, `stale` | Only when the event type declares `INGESTTIME`: arrival time in ticks, and whether it exceeded `MAXDELAY` |
| `battery` | Only for a device class with `DECAY`: the fraction of battery left, from 1.0 down to 0.0. The lower it is, the noisier the reading. |

The `tier` and `battery` fields are ground truth added by the simulator; a real sensor would not send them. They let you check whether a system under test caught an anomaly. Remove them before replaying the stream into such a system.

A rule-produced event:

```json
{"event_type": "alert", "event_time": 30, "device_id": "SM_N2_15",
 "alert_type": "DROUGHT_STRESS", "value": 15.0, "triggered_by": "alert(DROUGHT_STRESS)"}
```

| Field | Meaning |
|---|---|
| `triggered_by` | The rule that produced the event. Its presence is what separates derived events from readings. |
| `alert_type`, `report_type` | The constant from the rule head |
| `value`, `avg_value` | The rule's aggregate, here the average moisture over the 30-minute window that ended at tick 30 |

Events can chain. In the example, `DROUGHT_STRESS` alerts feed a rule that emits an `IRRIGATION_NEEDED` report.

Some useful one-liners:

```bash
# How many events of each kind
python3 -c "import json,collections; print(collections.Counter(json.loads(l)['event_type'] for l in open('out/events.jsonl')))"

# Only the alerts
grep '"event_type": "alert"' out/events.jsonl | head
```

### profiles.json

The compiled model. You do not need it to use the events, but it answers "why did this device produce that value?".

| Section | Contents |
|---|---|
| `simulation` | Start and end tick, milliseconds per tick, seed, `STICKY` settings |
| `stream`, `event_types` | The attributes each event type carries |
| `profiles` | One statistical model per device class, measure and zone, named like `SoilMoistureSensor_Moisture_zone1`: the tiers with their distributions and probabilities, valid range, resolution, and the measure's quantity, unit and conversion to SI (`to_si`) |
| `bindings` | One entry per device: its zone, the profile each measure uses, which `value` field it fills, how often it transmits, its failure probability, and its `DECAY` battery if the class has one |
| `rules` | The compiled rules |

## Checking that the output is statistically right

The analyser compares the events with the declared distributions:

```bash
python3 -c "
from eval.correctness import analyse, print_report, save_json
r = analyse('out/events.jsonl', 'out/profiles.json')
print_report(r)
save_json(r, 'out/correctness.json')"
```

For each measure and zone it prints two tables:

```
  ── AirTemp ──
  Zone        n    Obs μ   Mix μ   Obs σ  Mix σ     μ    σ  Rng    ✓
     1    1,917    21.81   21.97    8.47   8.44  PASS PASS PASS PASS

  Zone      NORMAL  obs/decl     ABOVE  obs/decl     BELOW  obs/decl      Transitions  obs/exp
     1  85.3% / 85.0%          7.5% /  8.0%        7.2% /  7.0%       508 (26.5%/26.6%)
```

| Column | Meaning | Passes when |
|---|---|---|
| `n` | Readings pooled over all devices of that class in that zone. A `*` marks a sample too small for the tier check to be reliable. | |
| `Obs μ` / `Mix μ` | Observed mean against the expected mean of the whole mixture (all three tiers weighted by probability) | within 15% |
| `Obs σ` / `Mix σ` | Observed against expected standard deviation of the mixture | within 20% |
| `Rng` | Every value lies inside the declared `RANGE` | no violations |
| `NORMAL` / `ABOVE` / `BELOW` | Observed share of each tier against its declared `PROB` | within 5 percentage points |
| `Transitions` | How often consecutive readings of a device change tier, against the expected rate. Informational. | |
| `✓` | All of the checks above | |

The expected standard deviation is that of the mixture, not of the `NORMAL` tier. For air temperature in zone 1 the `NORMAL` tier has a standard deviation of 4.0, but the mixture's is 8.44 because the rare hot and cold tiers sit far from the centre.

How much to expect: with a short run, sensors that report once a day have only a handful of readings, so their rows fail by chance. In the 7-day quick start 24 of 39 groups pass, and nearly every failure is starred. In a 90-day run 36 to 38 of 39 pass depending on the seed. The groups that fail are low-count ones (daily NPK sensors) or ones dominated by a rare tier (rainfall), and they change from seed to seed. A `Rng` failure, or the same group failing across seeds, would indicate a real problem.

Measures sent `ON_CHANGE` or `ON_THRESHOLD` are listed but not checked, because those modes transmit only a value-dependent subset of readings. Device classes with `DECAY` are listed but not checked either: their spread widens on purpose as the battery drains.

## Tests and benchmarks

```bash
python3 -m unittest discover -s tests        # 87 tests, about 3 seconds
```

```bash
python3 tools/benchmark.py \
    --schema  examples/agriculture.sql \
    --rules   examples/agriculture.iotdl \
    --sim-end 1 --time-unit days --tick-unit minutes \
    --counts  10 50 100 250 500 1000 --zones 2 \
    --output  out/benchmark.json          # add --plot for charts (needs matplotlib)
```

Measured on an Apple M3 Max with Python 3.10:

| Run | Events | Time | Throughput |
|---|---|---|---|
| Precision agriculture, 48 devices, 90 days | 1.07 million (165 MB) | 12.6 s | 85,000 events/s |
| Benchmark, 10 devices, 1 day | 13,000 | 0.14 s | 91,000 events/s |
| Benchmark, 1,000 devices, 1 day | 1.35 million | 15.7 s | 86,000 events/s |

Run time grows linearly with the number of devices (scaling exponent 1.02). Output is about 155 bytes per event.

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `No resource data. Pass --db … or --devices …` | A simulation needs a device list. Add one of the two flags, or `--validate-only`. |
| `device_class = 'X' is not a declared DEVICE_CLASS` | The resource data and the schema do not belong together. See the pairing table in [The three inputs](#the-three-inputs). |
| `has zone = 3, but DISTRIBUTION FOR X has no WHERE zone = 3 block` | A device sits in a zone the schema has no distribution for. Add the `WHERE` block or move the device. |
| `column(s) … are declared in CREATE TABLE but missing from …` | The database lacks a column the schema declares. Remove it from the schema or add it to the database. |
| `window of 1800 s is shorter than one tick` | `--tick-unit` is too coarse for a rule window. Use a smaller unit. |
| `WARN: ARRIVALS X: interval is … ticks; using 1` | `--tick-unit` is coarser than a sensor's interval, so the sensor is slowed to one reading per tick. Use a smaller unit. |
| `MEASURES X UNIT ... is the old form` | `MEASURES` now names a quantity from the measurement ontology. Write `MEASURES <quantity> IN <unit> FOLLOWING X`, e.g. `MEASURES Temperature IN celsius FOLLOWING AirTemp`. `--list-quantities` shows the choices. |
| `'X' is not a quantity in the measurement ontology` | Use a quantity from `--list-quantities` (soil moisture is `VolumeFraction`, gas concentration is `AmountFraction`), or add a row to `compiler/ontology/measurements.sql`. |
| `'u' is not a unit of Q` | The unit belongs to another quantity. The message lists the units `Q` accepts. |
| `No CREATE TABLE has a column declared REFERENCES DEVICE_CLASS` | The schema has no device table. Exactly one table needs a `device_class STRING REFERENCES DEVICE_CLASS` column and one `PRIMARY KEY`. |
| Two runs give different events | No `--seed` was given. |
| `ModuleNotFoundError: No module named 'eval'` | Run the analyser from `compiler/`. |

All errors are reported together and stop the run; devices are never skipped or given default values.

## Repository layout

```
README.md                  this file
SPEC_CONFORMANCE.md        where the implementation was brought in line with the spec, and the test for each
docs/REFERENCE.md          language reference, engine internals, rule semantics
compiler/
├── compiler.py            command-line entry point
├── lexer/ parser/ ast_nodes/   schema and rules → syntax tree
├── validator/             semantic checks
├── ontology/              measurement ontology: the quantities and units MEASURES may use
├── resources/             reads --db / --devices and resolves each device's zone
├── codegen/               binds devices to distributions, compiles rules, writes profiles.json
├── engine/
│   ├── fast_simulator.py  the simulation engine
│   └── simulator.py       the original engine, kept for reference and not used
├── eval/correctness.py    the correctness analyser
├── tools/                 build_resources_db.py, generate_devices.py, benchmark.py
├── tests/                 unit tests
├── examples/              schemas, rules and resource data
└── out/                   default output directory (contents ignored by git)
```

## Further reading

- [docs/REFERENCE.md](docs/REFERENCE.md): the full language (`CREATE DEVICE CLASS` with the measurement ontology and battery `DECAY`, `CREATE EVENTTYPE`, `CREATE EVENTSTREAM`, `DISTRIBUTION FOR`, rules), how values are generated, exact rule semantics, and how to tune tier probabilities to debug rules.
- [SPEC_CONFORMANCE.md](SPEC_CONFORMANCE.md): the interpretation taken wherever the spec was silent.

## License

MIT License.
