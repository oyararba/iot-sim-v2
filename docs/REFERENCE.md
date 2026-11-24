# IoTSim Reference

Language reference, compiler and engine internals, and rule semantics. For how to run IoTSim and read its output, start with the [README](../README.md).

Section numbers are kept stable: `SPEC_CONFORMANCE.md` and comments in the code that cite "README §N" refer to section N of this file.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Why IoTSim](#2-why-iotsim)
3. [Architecture](#3-architecture)
4. [Installation](#4-installation)
5. [Quick Start](#5-quick-start)
6. [Language Reference](#6-language-reference)
   - 6.1 [CREATE DEVICE CLASS](#61-create-device-class)
   - 6.2 [CREATE EVENTTYPE](#62-create-eventtype)
   - 6.3 [CREATE EVENTSTREAM](#63-create-eventstream)
   - 6.4 [DISTRIBUTION FOR](#64-distribution-for)
   - 6.5 [IoTDL Rules](#65-iotdl-rules)
   - 6.6 [CREATE TABLE and RESOURCES](#66-create-table-and-resources-the-enterprise-db)
7. [Compiler](#7-compiler)
   - 7.1 [Pipeline Stages](#71-pipeline-stages)
   - 7.2 [CLI Reference](#72-cli-reference)
   - 7.3 [Output Files](#73-output-files)
8. [Simulation Engine](#8-simulation-engine)
   - 8.1 [Heap Scheduler](#81-heap-scheduler)
   - 8.2 [Value Generation](#82-value-generation)
   - 8.3 [Rule Engine](#83-rule-engine)
   - 8.4 [Time Model](#84-time-model)
9. [Correctness Validation](#9-correctness-validation)
   - 9.1 [Running the Analyser](#91-running-the-analyser)
   - 9.2 [Mixture Statistics](#92-mixture-statistics)
   - 9.3 [Validation Metrics](#93-validation-metrics)
10. [Performance Benchmarks](#10-performance-benchmarks)
    - 10.1 [Running Benchmarks](#101-running-benchmarks)
    - 10.2 [Results](#102-results)
11. [Debugging Rules with IoTSim](#11-debugging-rules-with-iotsim)
12. [Examples](#12-examples)
    - 12.1 [Agriculture](#121-agriculture)
    - 12.2 [Hospital](#122-hospital)
13. [Project Structure](#13-project-structure)
14. [Future Work](#14-future-work)
15. [References](#15-references)

---

## 1. Overview

Testing and developing IoT stream-processing systems requires realistic sensor data that captures:

- Statistical signal behaviour per deployment zone
- Device heterogeneity across sensor types
- Network unreliability and transmission failure
- Causal event chains — alerts that fire from readings, reports that follow alerts

IoTSim addresses this by providing a declarative language with five constructs, each covering one independent concern. The same compiler and engine work for any IoT domain — agriculture, hospital, smart building, logistics — without modification. Only the schema changes.

```
schema.sql  +  rules.iotdl
        │
        ▼
   IoTSim Compiler
   ├── Lexer + Parser
   ├── Semantic Validator
   └── Code Generator  ──→  profiles.json
                                  │
                                  ▼
                         Simulation Engine  ──→  events.jsonl
                                                 correctness report
```

---

## 2. Why IoTSim

| Problem | IoTSim's answer |
|---|---|
| Custom simulation scripts are hardcoded to one deployment | Declarative schema separates specification from simulation code |
| No reuse across domains | Same compiler and engine; only schema changes |
| No statistical structure in synthetic data | Per-zone parameter models with NORMAL / ABOVE / BELOW tiers |
| No causal event chains | IoTDL rule language for window-based triggered events |
| No verifiability | Automated correctness analyser using mixture statistics |
| Rare conditions hard to test | Tune tier probabilities in schema to force any condition |

---

## 3. Architecture

IoTSim separates five orthogonal concerns into distinct constructs:

| Construct | Concern |
|---|---|
| `CREATE DEVICE CLASS` | Physical hardware — what a sensor measures |
| `CREATE EVENTTYPE` | Schema of a streaming event |
| `CREATE EVENTSTREAM` | Transmission behaviour and network failure |
| `DISTRIBUTION FOR` | Statistical parameter model per zone |
| `IoTDL Rules` | Causal event chain logic |

The compiler reads the schema and rules, joins device instances against the enterprise database to resolve zone-to-distribution bindings, compiles rules into executable objects, and writes `profiles.json`. The simulation engine consumes `profiles.json` and generates events using a heap-based scheduler.

---

## 4. Installation

Python 3.9+ and nothing else; see [Requirements](../README.md#requirements).

---

## 5. Quick Start

See the [README](../README.md#quick-start) for running with a SQLite database, with JSON, and for reading the output.

---

## 6. Language Reference

IoTSim schemas are written in a SQL-like declarative syntax. A schema file (`.sql`) contains the five construct types. A rules file (`.iotdl`) contains IoTDL rules.

### 6.1 CREATE DEVICE CLASS

Describes physical hardware: which physical quantity it measures, in which unit, over what range and at what resolution. Says nothing about where the device is deployed or how it samples.

```
CREATE DEVICE CLASS <class name> (
    MEASURES <quantity> [IN <unit>] [FOLLOWING <distribution>]
        RANGE (<min>, <max>)
        RESOLUTION <step>,
    ...                                  -- up to three MEASURES
    <field> ENUM *
)
```

**MEASURES is not free text.** `<quantity>` must be a quantity from the measurement ontology (`ontology/measurements.sql`) and `<unit>` one of that quantity's units. The ontology is a pair of SQL tables, `quantity` (name, SI dimension, SI unit, physical bounds) and `unit` (name, symbol, conversion to SI), loaded into SQLite at compile time. `python3 compiler.py --list-quantities` prints it. Names match ignoring case and underscores; a unit may also be written as its quoted symbol (`IN "°C"`).

| Clause | Meaning | Default |
|---|---|---|
| `MEASURES <quantity>` | The physical quantity (Temperature, Pressure, AmountFraction, …) | required |
| `IN <unit>` | The unit readings, RANGE, RESOLUTION and the distribution's parameters are in | the quantity's default unit (kelvin for Temperature) |
| `FOLLOWING <distribution>` | The `DISTRIBUTION FOR` this measure samples from. `ARRIVALS` and rules refer to the measure by this name | the quantity's name |

The compiler rejects an unknown quantity (and suggests the closest), a unit that belongs to a different quantity, a `RANGE` outside the quantity's physical bounds (below absolute zero, a fraction above 100 %, a negative concentration), and two measures that follow the same distribution in different quantities or units. The old form `MEASURES AirTemp UNIT celsius` is a parse error that shows the new form.

`<field> ENUM *` declares the class's **user-defined field** (any name, any case; one per class). It is the distribution discriminator: each device's value for it is read from the enterprise DB (see [6.6](#66-create-table-and-resources-the-enterprise-db)), and selects the matching `WHERE <field> = <value>` block of every distribution the class's measures follow. A device whose value has no matching block is a compile error.

```sql
CREATE DEVICE CLASS SoilMoistureSensor (
    MEASURES    VolumeFraction
    IN          percent
    FOLLOWING   Moisture
    RANGE       (0.0, 100.0)
    RESOLUTION  0.1,
    zone        ENUM *
)

-- Two classes, one quantity, two behaviours
CREATE DEVICE CLASS AirTemperatureSensor (
    MEASURES Temperature IN celsius FOLLOWING AirTemp  RANGE (-20.0, 60.0) RESOLUTION 0.1,
    zone ENUM *
)
CREATE DEVICE CLASS SoilTemperatureSensor (
    MEASURES Temperature IN celsius FOLLOWING SoilTemp RANGE (-10.0, 80.0) RESOLUTION 0.1,
    zone ENUM *
)

-- Multi-measure sensor
CREATE DEVICE CLASS NPKSensor (
    MEASURES MassFraction IN mg_per_kg FOLLOWING Nitrogen   RANGE (0.0, 999.0) RESOLUTION 1.0,
    MEASURES MassFraction IN mg_per_kg FOLLOWING Phosphorus RANGE (0.0, 999.0) RESOLUTION 1.0,
    MEASURES MassFraction IN mg_per_kg FOLLOWING Potassium  RANGE (0.0, 999.0) RESOLUTION 1.0,
    zone ENUM *
)
```

`CREATE DEVICE CLASS` and `CREATE DEVICE_CLASS` are both accepted. Each profile in `profiles.json` carries the measure's `quantity`, `unit`, `unit_symbol`, `si_unit` and `to_si` (`si = value × scale + offset`), so downstream tools can convert readings to SI.

**DECAY (battery drain).** A device class may add one clause:

```
DECAY OVER <NORMAL | UNIFORM | EXPONENTIAL> (<params>) <time unit> NOISE <factor> [CURVE LINEAR | QUADRATIC | EXPONENTIAL]
```

```sql
CREATE DEVICE CLASS Ammonia_Sensor (
    MEASURES AmountFraction IN ppm FOLLOWING Ammonia RANGE (0.0, 50.0) RESOLUTION 0.1,
    DECAY OVER NORMAL (mean = 180, std_dev = 20) DAYS NOISE 4.0 CURVE EXPONENTIAL,
    zone ENUM *
)
```

Each device draws its own battery life from the `OVER` distribution once, at the start of the run (fresh batteries at time 0). As the battery drains, every draw is widened around its tier's mean by a factor *k* that grows from 1 to `NOISE` (*u* = fraction of the battery used):

| CURVE | k(u) | Shape |
|---|---|---|
| `LINEAR` (default) | 1 + (NOISE − 1)·u | steady degradation |
| `QUADRATIC` | 1 + (NOISE − 1)·u² | holds up, then worsens near the end |
| `EXPONENTIAL` | NOISE^u | multiplicative growth |

The tier mixture and the means are unchanged; only the spread grows (values are still clamped to `RANGE`). When the battery is empty the device stops transmitting. Readings of a decaying device carry `battery` (fraction left, 1.0 → 0.0) as ground truth, like `tier`; the correctness analyser reports these groups as not checked. `DECAY` is per device and covers all its measures; `FAILURE <duration>` in a `DISTRIBUTION` is a separate, sudden, per-measure failure, and whichever comes first wins.

**Multi-measure sensors** store readings in order: `value` = first measure, `value2` = second, `value3` = third. A multi-measure device sends one reading per transmission with all of its measures; the reading's timing, arrival mode and `FAILURE` come from the first measure's `ARRIVALS` entry.

### 6.2 CREATE EVENTTYPE

Defines the schema of a streaming event. The `device_id` field uses `fieldname: TableName(column) FK` syntax to reference the enterprise device table — the compiler reads this to know where to resolve device-to-zone mappings.

Only `sensor_reading` events enter the stream. Computed events (`metric_avg`, `alert`, `report`) are produced by the rule engine only.

```sql
CREATE EVENTTYPE sensor_reading (
    EVENTTIME T    INTEGER,
    INGESTTIME I   MAXDELAY 5 MIN,
    device_id:     Device(device_id) FK,
    measure_type   ENUM,
    value          FLOAT,
    value2         FLOAT,   -- optional: second measure
    value3         FLOAT    -- optional: third measure
)

CREATE EVENTTYPE alert (
    EVENTTIME T    INTEGER,
    device_id:     Device(device_id) FK,
    alert_type     ENUM,
    value          FLOAT
)

CREATE EVENTTYPE report (
    EVENTTIME T    INTEGER,
    device_id:     Device(device_id) FK,
    report_type    ENUM
)
```

The foreign key may also be written without the colon or as `FOREIGNKEY` (`device_id Devices(device_id) FOREIGNKEY`); it must point at the device table (the one with `REFERENCES DEVICE_CLASS`).

**INGESTTIME / MAXDELAY** — models the delay between sensor emission time (`EVENTTIME`) and engine ingestion time. `MAXDELAY 5 MIN` means events arriving more than 5 minutes late are flagged as stale. Each reading gets `ingest_time = event_time + delay` with delay ~ Exponential(mean = MAXDELAY / 2), so about 13.5% of readings arrive stale (`"stale": true`).

**What a reading looks like.** A reading carries exactly the attributes its `EVENTTYPE` declares, in declared order: `device_id`, the measure name in `measure_type`, the values in `value` / `value2` / `value3`, and any attribute whose name matches a column of the device table (`depth_cm`, `device_class`). Values are omitted when absent (no `value2` on a single-measure device). The engine adds `event_type`, `event_time`, `ingest_time` / `stale` when declared, the tier of each value (`tier`, `tier2`, `tier3`) for the correctness analyser, and `battery` (fraction left) for a device class with `DECAY`:

```json
{"event_type": "sensor_reading", "event_time": 1440, "device_id": "NPK_N1",
 "measure_type": "Nitrogen", "value": 192.0, "value2": 89.0, "value3": 118.0,
 "depth_cm": 20, "tier": "NORMAL", "tier2": "NORMAL", "tier3": "NORMAL"}
```

### 6.3 CREATE EVENTSTREAM

Models the physical transmission channel. Declares which event type flows through the stream, and for each measure: arrival mode, and failure probability.

```sql
CREATE EVENTSTREAM agriculture_stream (
    EVENTTYPE  sensor_reading,

    ARRIVALS (
        Moisture      ON_CHANGE   (2.0)      FAILURE 0.10,
        AirTemp       PERIODIC    (5 MIN)    FAILURE 0.05,
        Nitrogen      PERIODIC    (1 DAY)    FAILURE 0.12,
        Rainfall      ON_THRESHOLD(0.2)      FAILURE 0.07,
        WindSpeed     PERIODIC    (1 MIN)    FAILURE 0.04
    )
)
```

**Arrival modes:**

| Mode | Behaviour |
|---|---|
| `PERIODIC (N UNIT)` | Fixed clock interval. Units: `MS` `SEC` `MIN` `HOUR(S)` `DAY(S)`; any other unit is an error |
| `ON_CHANGE (threshold)` | Emit only when value shifts by ≥ threshold |
| `ON_THRESHOLD (value)` | Emit only when value crosses the declared boundary |

**FAILURE** — probability of dropping any given reading (network loss, sensor fault). Modelled independently per reading. Optional: `Moisture PERIODIC (5 MIN)` without `FAILURE` drops nothing.

### 6.4 DISTRIBUTION FOR

Maps a device class measure to a statistical parameter model, per zone. The `WHERE ZONE = N` blocks are matched against the zone resolved from the enterprise database for each physical device.

Each WHERE block has three tiers:
- `NORMAL` — regular operating conditions
- `ABOVE` — values exceeding normal range (heatwave, tachycardia, flooding)
- `BELOW` — values beneath normal range (frost, hypoxia, drought)

Probabilities within each WHERE block must sum to 1.0 (validated at compile time).

Tiers can be written in any order (the NORMAL tier is the one without a label), with or without commas between them.

**STICKY** at tier level means transitions into or out of that tier happen gradually rather than abruptly. Tiers without STICKY can switch instantly — useful for phenomena like rainfall that starts and stops suddenly. Concretely:
- The tier becomes a per-device Markov chain. A STICKY tier holds for the next reading with probability 0.9 (`--sticky-persistence`); otherwise the next tier is redrawn with weights chosen so the long-run tier frequencies still equal the declared `PROB` values.
- When the tier changes into or out of a STICKY tier, the mean moves from the old tier to the new one over 5 readings (`--sticky-ramp`) instead of jumping.

**FAILURE `<duration>`** inside a `DISTRIBUTION` (`FAILURE 360 DAYS`, as on the slides) is the sensor's mean lifetime: each device's sensor for that measure fails at an exponentially distributed time with that mean and is not reported afterwards; a device whose sensors have all failed stops transmitting. (The per-reading drop probability is `FAILURE p` in `ARRIVALS`; a gradual decline is `DECAY` on the device class, §6.1.)

A zero spread is a point mass: `NORMAL (mean = 0.0, std_dev = 0.0)` and `UNIFORM (low = 0, high = 0)` always give 0.

```sql
DISTRIBUTION FOR AirTemp (
    WHERE ZONE = 1 (
        NORMAL (mean = 22.0, std_dev = 4.0)   PROB 0.85  STICKY,
        ABOVE (
            NORMAL (mean = 40.0, std_dev = 3.0) PROB 0.08  STICKY
            -- heatwave builds and fades gradually
        )
        BELOW (
            NORMAL (mean = 1.0,  std_dev = 1.5) PROB 0.07  STICKY
            -- frost also gradual
        )
    )
    WHERE ZONE = 2 (
        NORMAL (mean = 24.0, std_dev = 3.0)   PROB 0.92  STICKY,
        ABOVE (
            NORMAL (mean = 38.0, std_dev = 2.0) PROB 0.05  STICKY
        )
        BELOW (
            NORMAL (mean = 2.0,  std_dev = 1.0) PROB 0.03  STICKY
        )
    )
)

-- Rainfall: no STICKY — can start and stop abruptly
DISTRIBUTION FOR Rainfall (
    WHERE ZONE = 1 (
        NORMAL (mean = 0.0, std_dev = 0.0)   PROB 0.85,
        ABOVE (
            EXPONENTIAL (mean = 8.0)          PROB 0.15
        )
    )
)
```

**Supported distributions:** `NORMAL`, `UNIFORM`, `EXPONENTIAL`, `POISSON`, `BINOMIAL`

**WHERE blocks.** `WHERE <field> = <value>` uses the user-defined field of the device classes that measure it (`zone` above). Values can be integers, numbers, `"strings"` or bare identifiers (`WHERE soil_type = LOAM`), and parameters may be negative (`mean = -2.0`). A `DISTRIBUTION FOR` written without any `WHERE` applies to every device, whatever its zone:

```sql
DISTRIBUTION FOR WindDirection (
    UNIFORM (low = 0.0, high = 360.0)   PROB 1.0
)
```

### 6.5 IoTDL Rules

IoTDL rules define triggered events — window-based computations over sensor readings that produce derived event streams. Rules are declared alongside the schema so the causal structure of the deployment is specified and verified in one place.

**Syntax:**

```
new event_type[window?](grouping_vars, (aggregations?)) @ (fire_time) :-
    source_clause @ time_var,
    constraint?;
```

**Window types:**
- `sliding(s, N)` — N most recent readings
- `tumbling(s, N)` — non-overlapping windows of size N
- _(no window)_ — fires immediately on each matching event

**Aggregation functions:** `avg`, `min`, `max`, `sum`, `count`, `last`

```sql
-- Sliding window average
new metric_avg[sliding(s, 6)](device_id, MOISTURE,
    (avg_value = avg(value))) @ (s + 7) :-
    sensor_reading(device_id, MOISTURE, value, _, _) @ z;

-- Tumbling window with threshold constraint
new alert[tumbling(s, 30 MIN)](device_id, DROUGHT_STRESS,
    (value = avg(value))) @ (s + 31 MIN) :-
    sensor_reading(device_id, MOISTURE, value, _, _) @ z,
    value < 25;

-- No window — fires immediately on each matching event
new alert(device_id, FROST_RISK) @ (T + 1) :-
    sensor_reading(device_id, AIR_TEMP, value, _) @ T,
    value < 3;

-- Chained rule — fires on a triggered alert event
new report[tumbling(s, 60 MIN)](device_id, IRRIGATION_NEEDED) @ (s + 61 MIN) :-
    alert(device_id, DROUGHT_STRESS, value) @ z,
    value < 25;
```

The `measure_type` field must be specified in rule bodies (e.g. `MOISTURE`, `AIR_TEMP`) to prevent rules from matching readings from the wrong sensor type.

**Semantics** (implemented in `codegen/rules.py` and `engine/fast_simulator.py`):

| | |
|---|---|
| `tumbling(s, N)` | Non-overlapping windows of N ticks, or of a duration (`tumbling(s, 30 MIN)`), from the start of the simulation. Evaluated when the window closes. `s` is the window's first tick. |
| `sliding(s, N)` | The N most recent matching readings per group. Evaluated on each matching event once N readings are there. `s` is the time of the oldest reading in the window. |
| `sliding(s, 6 HOURS)` | The matching readings of the last 6 hours per group. Evaluated on each matching event. `s` is the window's first tick. |
| No window | Fires once per matching event. The time variable after `@` (`@ T`) is that event's time. |
| Body clauses | Bind an event's attributes by position, in the order its `EVENTTYPE` declares them (`EVENTTIME` / `INGESTTIME` excluded). Trailing arguments may be left out. |
| Constants | Identifiers in capitals of two or more characters (`DROUGHT_STRESS`, `MOISTURE`) or naming a declared measure (`Moisture`), numbers and `"strings"`. Everything else (`device_id`, `value`, and the window / time variables) is a variable. String constants match ignoring case and underscores: `MOISTURE` = `Moisture`, `AIR_TEMP` = `AirTemp`. |
| Constraints | Filter the matching events (`value < 25` keeps readings below 25; the aggregate is computed over those). |
| Several clauses | Tumbling windows only. Joined on shared variable names, the time variable included: `@ z` in both clauses means the two events share a timestamp. |
| Head | Aggregation outputs (`value = avg(value)`, several allowed) fill the head event's attribute of that name; the other head arguments fill the remaining attributes in order. Without a window, other attributes are filled from body variables of the same name. Groups are the head's variables. |
| Fire time | The window variable (or the body's time variable) plus offsets, e.g. `s + 31 MIN`; bare numbers are ticks. A fire time earlier than the moment the rule is evaluated is moved up to that moment. |
| Chaining | Triggered events are written to the output and fed to rules that read their event type. Each rule keeps its own windows. |

**Aggregations:** `avg`, `min`, `max`, `sum`, `count`, `last` (nulls are skipped).

### 6.6 CREATE TABLE and RESOURCES (the enterprise DB)

`CREATE TABLE` declares the enterprise tables that say where devices are deployed. The rows themselves live in the enterprise DB and are read at compile time from a SQLite file (`--db`) or a JSON file (`--devices`). Only the declared columns are read; extra columns in the DB are ignored, and a declared column missing from the DB is an error.

```sql
CREATE TABLE zones   ( zone_id INTEGER PRIMARY KEY, zone_name STRING, zone_type ENUM )
CREATE TABLE plots   ( plot_id INTEGER PRIMARY KEY,
                       zone_id INTEGER REFERENCES zones(zone_id), crop_type STRING )
CREATE TABLE devices ( device_id    STRING  PRIMARY KEY,
                       device_class STRING  REFERENCES DEVICE_CLASS,
                       plot_id      INTEGER REFERENCES plots(plot_id),
                       depth_cm     INTEGER )
```

How the compiler reads them:

| What | Where it comes from |
|---|---|
| The device table | The one table with a column declared `REFERENCES DEVICE_CLASS`. Required: a schema with none, with two, or whose device table has no single `PRIMARY KEY` is rejected by the validator, before any data is read |
| A device's id | That table's `PRIMARY KEY` |
| A device's class | The `REFERENCES DEVICE_CLASS` column; must name a declared `DEVICE_CLASS` |
| A user-defined field (`zone ENUM *`) | The nearest column named `zone` or `zone_id`, found by following `REFERENCES` out of the device table. Above: `devices.plot_id → plots.zone_id`. An exact name beats the `_id` form; two equally close candidates are an error. |
| Event attributes | Any attribute of the stream's event type whose name matches a device-table column (e.g. `depth_cm`) is filled from the device's row |

Every problem (unknown device class, dangling `REFERENCES`, NULL zone, a zone with no `WHERE` block) is reported together and stops compilation; devices are never defaulted or skipped.

**Building a DB.** `tools/build_resources_db.py` creates a SQLite file whose tables come from the schema's own `CREATE TABLE` blocks, filled from JSON rows keyed by table name:

```bash
python3 tools/build_resources_db.py \
    --schema examples/precision_agriculture.sql \
    --json   examples/agriculture_resources.json \
    --db     examples/agriculture_resources.db
```

`--devices` takes the same JSON directly.

---

## 7. Compiler

### 7.1 Pipeline Stages

| Stage | File | Description |
|---|---|---|
| Lexer | `lexer/tokenizer.py` | Tokenizes schema source. Handles keywords, identifiers, quoted strings, numeric literals, `--` comments. |
| Parser | `parser/parser.py` | Recursive-descent parser. Builds a typed AST from all five construct types plus IoTDL rules. |
| Semantic Validator | `validator/semantic.py` | Checks probability sums, distribution parameter completeness, referential integrity, measure name existence, `WHERE` fields against each class's user-defined field, and that every measure has a distribution and an `ARRIVALS` entry. |
| Resource Loader | `resources/loader.py` | Reads the declared tables from `--db` (SQLite) or `--devices` (JSON), finds the device table, and resolves each device's user-defined field through `REFERENCES`. |
| Code Generator | `codegen/generator.py` | Binds each device to the `WHERE` block matching its field value. Assigns distribution profiles per device. Compiles rules to structured objects. Writes `profiles.json`. |
| Simulation Engine | `engine/fast_simulator.py` | Consumes `profiles.json`. Runs the simulation. Writes `events.jsonl`. |

### 7.2 CLI Reference

```bash
python3 compiler.py [OPTIONS]

Options:
  --schema      PATH   Path to .sql schema file                    [required]
  --list-quantities    Print the measurement ontology (quantities, units) and exit
  --rules       PATH   Path to .iotdl rules file                   [optional]
  --db          PATH   Enterprise DB (SQLite) with the declared tables
  --devices     PATH   Or: the same tables as JSON {"table": [rows]}
                       (one of --db / --devices is required unless --validate-only)
  --validate-only      Check the schema, and the resources if given; no simulation
  --sim-start   INT    Simulation start time                       [default: 0]
  --sim-end     INT    Simulation end time        [default: 90; none with --num-events]
  --num-events  INT    Stop after exactly N events (readings + rule events);
                       alone: no time limit; with --sim-end: whichever comes first
  --time-unit   STR    Unit for sim-start/end: seconds|minutes|hours|days [default: days]
  --tick-unit   STR    Simulation resolution: seconds|minutes|hours  [default: minutes]
  --seed        INT    Random seed for reproducibility             [default: none; runs differ]
  --sticky-persistence F  STICKY: chance a tier holds for the next reading [default: 0.9]
  --sticky-ramp INT    STICKY: readings over which the mean moves     [default: 5]
  --verbose     INT    Print progress every N ticks                [default: 0]
  --output      PATH   Output directory for profiles.json + events.jsonl [default: out]
```

**Tick unit guidance:** set to the fastest sensor interval in your deployment. For agriculture with 1-minute wind sensors, use `--tick-unit minutes`. A 90-day simulation at 1-minute resolution = 129,600 ticks.

### 7.3 Output Files

| File | Description |
|---|---|
| `out/profiles.json` | Compiled profiles: device bindings (with any `DECAY` battery), parameter models (with each measure's quantity, unit and SI conversion), rules, stream config |
| `out/events.jsonl` | Simulation output — one JSON object per event, newline-delimited |
| `out/correctness.json` | Correctness analysis results (if analyser is run) |
| `out/benchmark.json` | Benchmark sweep results (if benchmark runner is used) |

---

## 8. Simulation Engine

### 8.1 Heap Scheduler

The engine maintains a min-heap of scheduled device fire times, tumbling-window closes and triggered events. At each step it pops the earliest entry; within one tick it closes the windows that ended before the tick, then handles readings, then triggered events due at the tick. A device entry generates a reading, applies the failure probability, emits the event (which also feeds the rules), and pushes the device back with its next fire tick. Cost is O(log N) per event where N is the number of devices — never O(T × D). Output is in event-time order.

### 8.2 Value Generation

For each emission, and for each of the device's measures:
1. Select the NORMAL, ABOVE or BELOW tier: an independent roll against the declared probabilities, or a step of the STICKY Markov chain (6.4).
2. Sample from that tier's statistical distribution (shifted while a STICKY transition is ramping; widened around the tier mean by the battery's spread factor when the class has `DECAY`).
3. Clamp to the declared RANGE from the device class.
4. Round to the declared RESOLUTION.
5. Tag the tier name on the event (`tier`, `tier2`, `tier3`; used by the correctness analyser).

A device whose `DECAY` battery is empty stops transmitting. A measure whose sensor has passed its `FAILURE <duration>` lifetime is left out. The reading is then dropped with the `ARRIVALS` `FAILURE` probability, and given an ingest time when the event type declares `INGESTTIME`.

### 8.3 Rule Engine

- **Tumbling window rules** — evaluated when a window closes, per group, over the matching events (joined across clauses).
- **Sliding window rules** — evaluated on each matching event.
- **No-window rules** — fire immediately on each matching event.

Each rule keeps its own windows and timers. Triggered events are scheduled at their fire time, written to the output, and fed to downstream chained rules. See 6.5 for the exact semantics.

### 8.4 Time Model

Simulation time is expressed in user-friendly units at the CLI and converted to ticks internally.

| Duration | Ticks (1-min tick unit) |
|---|---|
| 1 minute | 1 |
| 5 minutes | 5 |
| 1 hour | 60 |
| 1 day | 1,440 |
| 90 days | 129,600 |

**Stopping by time or by count.** A run ends at `--sim-end`, after `--num-events` events, or at whichever comes first when both are given (`simulation.end_time` and `simulation.max_events` in `profiles.json`; `end_time` is `null` for a count-only run). Because the heap pops entries in event-time order, a count-only run writes the first N events of the stream: with the same seed, the first N lines of a timed run long enough to hold them. Readings and rule events both count. Without a time limit the engine also stops, with a warning, once every device has stopped and no rule window still holds data (otherwise tumbling-window timers would re-arm forever), or after `STALL_LIMIT` (1,000,000) heap steps in a row without an event.

---

## 9. Correctness Validation

The correctness analyser in `eval/correctness.py` compares observed simulation output against declared parameter models.

### 9.1 Running the Analyser

```python
from eval.correctness import analyse, print_report, save_json

results = analyse(
    events_jsonl  = 'out/events.jsonl',
    profiles_json = 'out/profiles.json',
    tolerance_mean = 0.15,   # ±15% of mixture mean
    tolerance_std  = 0.20,   # ±20% of mixture std
    tolerance_tier = 0.05,   # ±5 percentage points on tier rates
)

print_report(results)
save_json(results, 'out/correctness.json')
```

Values are pooled over every device of a (device class, measure, zone). **Multi-measure sensors:** the analyser reads `value`, `value2`, `value3` in the device class's MEASURES order, taken from the compiled bindings. Mixture statistics use each tier's own distribution (NORMAL, UNIFORM, EXPONENTIAL, POISSON, BINOMIAL). Measures transmitted `ON_CHANGE` / `ON_THRESHOLD` are reported but not checked, because those modes transmit a value-dependent subset of readings; so are device classes with a `DECAY` battery, whose spread widens on purpose. Groups marked `*` have too few readings for the ±5 pp tier check to be meaningful.

### 9.2 Mixture Statistics

Observed mean and std are compared against **mixture statistics** — the combined values across all tiers — not against the NORMAL tier alone.

```
mixture_mean = Σ pᵢ × μᵢ

mixture_var  = Σ pᵢ × (σᵢ² + (μᵢ − mixture_mean)²)
mixture_std  = √(mixture_var)
```

For AirTemp Zone 1 (NORMAL mean=22 prob=0.85, ABOVE mean=40 prob=0.08, BELOW mean=1 prob=0.07):
- `mixture_mean = 21.97`
- `mixture_std  = 8.44` (not 4.0 — between-tier variance dominates)

Comparing observed std against the NORMAL-only std of 4.0 would always fail even when the simulation is correct.

### 9.3 Validation Metrics

| Check | Description | Tolerance |
|---|---|---|
| Tier rates | Fraction of events in NORMAL / ABOVE / BELOW vs declared probabilities | ±5 percentage points |
| Mean | Observed mean vs mixture mean | ±15% of mixture mean |
| Std dev | Observed std vs mixture std | ±20% of mixture std |
| Range | Any reading outside declared device RANGE | Zero violations |
| Transition rate | Observed tier switch rate vs theoretical `1 − Σpᵢ²` (or the STICKY chain's rate) | ±5 percentage points |

**Agriculture results (90-day, 48 devices, 1.07M events):** 36 to 38 of 39 class/measure/zone groups pass, depending on the seed, with zero range violations. The groups that fail are small samples (daily NPK sensors, 80 to 160 readings) or dominated by a rare tier (rainfall), and differ from seed to seed.

---

## 10. Performance Benchmarks

### 10.1 Running Benchmarks

```bash
python3 tools/benchmark.py \
    --schema    examples/agriculture.sql \
    --rules     examples/agriculture.iotdl \
    --zones     2 \
    --sim-end   1 \
    --time-unit days \
    --tick-unit minutes \
    --counts    10 50 100 250 500 1000 \
    --seed      42 \
    --output    out/benchmark.json \
    --plot                          # requires matplotlib
```

Generates resource data at each device count using `tools/generate_devices.py` with realistic sensor mix ratios (50% SoilMoisture, 20% AirTemp, 15% Humidity, 15% NPK).

### 10.2 Results

1-day simulation, 1-minute ticks, `agriculture.sql` + `agriculture.iotdl`, Apple M3 Max, Python 3.10:

| Devices | Wall time (s) | Events | Events/s |
|---|---|---|---|
| 10 | 0.14 | 13,136 | 90,588 |
| 50 | 0.70 | 65,363 | 92,803 |
| 100 | 1.46 | 135,807 | 93,117 |
| 250 | 3.67 | 338,082 | 92,210 |
| 500 | 7.67 | 677,809 | 88,403 |
| 1,000 | 15.70 | 1,352,862 | 86,184 |

**Scaling exponent: 1.02** (near-linear). Throughput stays near 90K events/second from 10 to 1,000 devices; the small drop is the O(log N) heap cost as device count grows.

**Full 90-day run** (`precision_agriculture.sql`, 48 devices): 1.07 million events (815K readings, 254K triggered) in 12.6 seconds at ~85K events/second. Output: 165 MB of JSON Lines, about 155 bytes per event.

---

## 11. Debugging Rules with IoTSim

The schema is a controllable knob. By temporarily tuning tier probabilities you can force specific sensor conditions to isolate rule bugs without waiting for rare events to appear naturally.

**Scenario 1 — Force a tier (unit test a rule)**

HEATWAVE alert never fires during testing. Is the rule wrong or does heatwave data never appear?

```sql
DISTRIBUTION FOR AirTemp (
    WHERE ZONE = 1 (
        NORMAL (...) PROB 0.00,
        ABOVE  ( NORMAL (mean = 40.0, std_dev = 3.0) PROB 1.00 )
        -- force all readings into heatwave tier
    )
)
```

If the alert still does not fire, the bug is in the rule logic, not the data. Restore original probabilities once confirmed.

**Scenario 2 — Push to the boundary (test threshold logic)**

Drought alert fires inconsistently near the declared 25% moisture threshold.

```sql
DISTRIBUTION FOR Moisture (
    WHERE ZONE = 1 (
        BELOW ( NORMAL (mean = 25.0, std_dev = 0.5) PROB 0.99 )
        NORMAL (...) PROB 0.01
        -- cluster readings right at the decision boundary
    )
)
```

Exposes off-by-one errors and window size issues that would take days to appear in real data.

**Scenario 3 — Reproduce a rare co-occurrence (test composite rules)**

NPK_CRITICAL report requires both nitrogen and phosphorus deficiency simultaneously — statistically rare in a realistic simulation.

```sql
DISTRIBUTION FOR Nitrogen (
    WHERE ZONE = 1 (
        BELOW ( NORMAL (mean = 40.0, std_dev = 10.0) PROB 0.90 )
        NORMAL (...) PROB 0.10
    )
)
DISTRIBUTION FOR Phosphorus (
    WHERE ZONE = 1 (
        BELOW ( NORMAL (mean = 15.0, std_dev = 5.0) PROB 0.90 )
        NORMAL (...) PROB 0.10
    )
)
```

Co-occurring deficiencies appear in every window. Verify the composite rule then restore realistic probabilities.

---

## 12. Examples

### 12.1 Agriculture

`examples/precision_agriculture.sql` (schema + rules) with its enterprise DB `examples/agriculture_resources.db` (48 devices). The smaller `examples/agriculture.sql` + `examples/agriculture.iotdl` is the original example. Models a precision agriculture farm with three zone types.

**Device classes (12):** SoilMoistureSensor, SoilTemperatureSensor, AirTemperatureSensor, HumiditySensor, SolarRadiationSensor, RainfallSensor, WindSensor, CO2Sensor, NPKSensor, PHSensor, SalinitySensor, InsectTrapSensor

**Zone types:** IRRIGATED, NON_IRRIGATED, INDOOR

**IoTDL rules (22):** four sliding averages (moisture, air temperature, humidity, insect count); alerts for drought stress, waterlogging, frost risk, heatwave, disease risk, CO2 low/high, storm, N/P/K deficiency (separately), pH low/high, salinity and pest surge; reports for irrigation needed, NPK critical and disease window

**Run:**
```bash
python3 compiler.py \
    --schema examples/precision_agriculture.sql \
    --db     examples/agriculture_resources.db \
    --sim-end 90 --time-unit days --tick-unit minutes \
    --output out/
```

### 12.2 Hospital

Not included in this repository. Models a multi-ward hospital. Demonstrates IoTSim generalisability — the same compiler and engine, zero modification.

**Device classes (7):** PulseOximeter, ECGMonitor, NIBPCuff, ThermometerProbe, RespiratoryBelt, ICPMonitor, RFIDReader

**Zone types:** ICU, CICU, NEURO_ICU, GENERAL, ED, TRAUMA

**Notable distributions:**
- `NEURO_ICU` SpO2 uses permissive hypertension baseline (mean systolic 138 mmHg) — clinical protocol for TBI patients
- `TRAUMA` models haemodynamic instability as the NORMAL state, recovery as ABOVE

**IoTDL rules:** Hypoxia alert, tachycardia alert, hypertension alert, bradycardia alert, deterioration report (requires hypoxia + tachycardia simultaneously), ICP critical

---

## 13. Project Structure

```
compiler/
├── compiler.py                  # CLI entrypoint
├── lexer/tokenizer.py           # Tokenizer — keywords, literals, comments
├── parser/parser.py             # Recursive-descent parser → AST
├── ast_nodes/nodes.py           # Typed AST node dataclasses
├── validator/semantic.py        # Semantic validation
│                                # - probability sums per WHERE block
│                                # - distribution parameter completeness
│                                # - referential integrity
│                                # - measure name existence
├── ontology/
│   ├── measurements.sql         # Measurement ontology: quantity + unit tables
│   └── __init__.py              # Loads it into SQLite; MEASURES / IN lookups
├── resources/loader.py          # Enterprise DB (--db / --devices) reader
│                                # - device → plot → zone resolution
├── codegen/
│   ├── generator.py             # Device bindings, packed measures, STICKY chains,
│   │                            # lifetimes, event shape, profiles.json
│   └── rules.py                 # Rule compilation and rule checks
├── engine/
│   ├── fast_simulator.py        # Primary: heap-based scheduler + rule engine
│   └── simulator.py             # Original simulator (for reference; not used,
│                                # does not read the current profiles.json)
├── eval/correctness.py          # Correctness analyser
├── tools/
│   ├── generate_devices.py      # Resource JSON generator at arbitrary scale
│   ├── build_resources_db.py    # SQLite enterprise DB from schema + JSON rows
│   └── benchmark.py             # Performance benchmark sweep
├── tests/                       # python3 -m unittest discover -s tests
├── examples/
│   ├── precision_agriculture.sql    # Full schema + 22 rules
│   ├── agriculture_resources.db     # Its enterprise DB (48 devices)
│   ├── agriculture_resources.json   # The same rows as JSON
│   ├── agriculture.sql              # Smaller schema (4 classes, 2 zones)
│   └── agriculture.iotdl            # Its rules
└── out/                         # Default output directory (generated files)
```

---

## 14. Future Work

| Item | Description |
|---|---|
| Multiple event streams | Currently only one stream per simulation. Multiple parallel streams with independent failure rates and arrival configurations. |
| Custom distribution functions | User-defined Python callables as distribution implementations alongside the five built-in families. |
| Anomaly injection with temporal correlation | Per-device state machine that holds a tier for a declared duration, introducing genuine temporal correlation between consecutive readings. |
| Circadian and seasonal modulation | Sinusoidal layer over base parameters — daily cycles for temperature and heart rate, seasonal cycles for soil temperature. |
| Compact output format | Switch from JSON Lines (~150 bytes/event) to Parquet or binary MessagePack (5-10× smaller, faster analytics). |
| Real-time mode | Throttle the engine to wall-clock speed so IoTSim can feed a live stream processor during development. |
| Multi-source rules | Rules that join two event streams — e.g. hypoxia AND tachycardia on the same patient within a window. |
| Visual schema editor | Graphical tool for configuring device classes, zones, and parameter models without writing schema syntax. |

---

## 15. References

- Tyler et al. — CEPAL event type specification (FK syntax in EVENTTYPE adapted from this work)
- NEXMark Benchmark — Tucker et al. (motivating example of hardcoded data generators)
- DEBS Grand Challenge datasets (motivating example of domain-locked simulators)

---

## License

MIT License.

---
<!-- 
## Citation

If you use IoTSim in your research, please cite:

```
@mastersthesis{iotsim2026,
  title   = {IoTSim: A Language Framework for Pseudo-Realistic IoT Sensor Stream Generation with Causal Event Chains},
  author  = {Your Name},
  school  = {Your University},
  year    = {2026}
}
``` -->