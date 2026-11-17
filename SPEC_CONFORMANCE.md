# Spec conformance

The spec sheet is the language reference in `docs/REFERENCE.md` (§6–§9; cited below
as "README §N", since it was the README when this was written) and the MS defense
slides ("IoT-Sim: A General Framework for Generating Pseudo-Real Sensor
Streams with Causal Event Chains"). This file lists every place the code
deviated from it, what it does now, and the test that pins it down
(`compiler/tests/`). Run the tests from `compiler/`:

```bash
python -m unittest discover -s tests -v
```

## Language

| Spec sheet | Before | Now | Test |
|---|---|---|---|
| `CREATE DEVICE CLASS` (two words, README §6.1 and slides) | Parse error; only `DEVICE_CLASS` | Both forms | `SpecSheetSyntaxTests` |
| `MEASURES Moisture UNIT percent_VWC` (README §6.1, slides) | Parse error unquoted; any name, any unit string | Superseded by the measurement ontology: `MEASURES <quantity> IN <unit> FOLLOWING Moisture`; the old form is a parse error that shows the new one (see Extensions) | `test_device_class_forms_and_units`, `test_ontology.py` |
| `zone ENUM *` as the user-defined distribution discriminator; one per class | Parsed into a flag nothing read; lowercase `zone` hung the parser | Any name and case; value read per device from the enterprise DB; more than one is an error | `test_resources.py` |
| `device_id: Device(device_id) FK` / `Devices(device_id) FOREIGNKEY` (README §6.2, slides) | Parse error | Parsed and checked against the device table | `test_eventtype_fk_and_ingesttime` |
| `INGESTTIME I MAXDELAY 5 MIN` (README §6.2) | Parse error | Parsed and simulated (see Interpretations) | `IngestTimeTests` |
| `Moisture PERIODIC(5 MIN)` without `FAILURE` (slides) | Parse error | `FAILURE` optional, default 0 | `test_arrivals_failure_optional` |
| `1 DAY`, `12 HOUR` (README §6.3, slides) | Silently read as **1 second** | Singular and plural units; unknown units are errors | `test_unknown_time_unit_is_an_error` |
| `FAILURE 360 DAYS` inside `DISTRIBUTION` (slides) | Parse error | Sensor lifetime (see Interpretations) | `FailureLifetimeTests` |
| `PROB 0.85 STICKY` (README §6.4, slides) | Parse error | Parsed and simulated (see Interpretations) | `StickyTests` |
| Tiers in any order, commas between tiers (README §6.4, §11) | NORMAL had to come first; commas were errors | Any order, commas optional | `test_distribution_failure_sticky_and_tier_order` |
| `NORMAL (mean = 0.0, std_dev = 0.0)` for "no rain" (README §6.4) | Rejected (std_dev must be > 0) | Zero spread is a point mass | `test_resolution_and_range` |
| `mean = -2.0` | Parse error | Signed parameters | `test_where_values_and_unconditional` |
| `DISTRIBUTION FOR X (` tiers `)` with no `WHERE` | Parse error | Applies to every zone | `test_resources.py` |
| Rules without a window (README §6.5, slides) | Parse error | Fire once per matching event | `test_rule_without_window_fires_per_event` |
| `tumbling(s, 30 MIN)`, `@ (s + 31 MIN)` (README §6.5, slides) | Parse error; bare ticks only | Durations in windows and fire times | `test_tumbling_window_start_fire_time_and_aggregates` |
| `sensor_reading(device_id, AIR_TEMP, value, _)` with fewer arguments than attributes (README §6.5) | Body arguments were ignored by the engine | Arguments bind attributes by position; trailing ones may be left out; too many is an error | `test_rule_errors` |
| `<=`, `>=`, `!=`, `==` in constraints | Mis-tokenised (`<=` became `<` followed by `=`) | Supported | `test_comparison_operators` |
| Several aggregations `(aggregations?)` (README §6.5) | Parse error | `(a = avg(x), b = max(y))` | `test_several_aggregations` |

## Resources (the enterprise DB)

| Spec sheet | Before | Now | Test |
|---|---|---|---|
| RESOURCES are enterprise SQL tables (slides) | Only a JSON file with hard-coded field names; extra columns crashed | `--db` (SQLite) or `--devices` (JSON), read through the schema's `CREATE TABLE`s | `test_resources.py` |
| The zone comes from the enterprise DB and selects the `WHERE` block (README §3, §6.1, §6.4) | Hard-coded `plots.zone_id`; devices in a zone with no block were dropped silently; without a device file, devices and zones were invented | Resolved through `REFERENCES`; unmatched devices are compile errors; no invented devices | `test_device_in_zone_without_block_is_an_error` |

## Readings

| Spec sheet | Before | Now | Test |
|---|---|---|---|
| `EVENTTYPE` is "the shape of a streaming event" (slides); only `sensor_reading` enters the stream | Events had a fixed shape with `measure_name`; devices were tagged with the first `EVENTTYPE` in the file or by name fragments | Readings carry exactly the declared attributes and the stream's `EVENTTYPE` | `test_event_shape_follows_eventtype` |
| Multi-measure sensors: `value`, `value2`, `value3` in MEASURES order (README §6.1) | Only the **last** measure was emitted, at N× the rate (NPK sensors produced only Potassium) | One packed reading per transmission | `test_multi_measure_device_sends_one_packed_reading` |
| `RESOLUTION` is a physical property of the sensor (slides) | Ignored (values had 4 decimals) | Values rounded to the resolution | `test_resolution_and_range` |
| Correctness analyser compares every device/zone with the mixture model (README §9) | Only the first device of each group; non-NORMAL tiers used wrong moments (e.g. UNIFORM std 1.0); NPK order hard-coded | Pooled over devices; correct moments per distribution; order from bindings; STICKY-aware transition rate | run `eval/correctness.py` |

## Rules

| Spec sheet | Before | Now | Test |
|---|---|---|---|
| The measure in the body selects the readings (README §6.5) | Ignored: every rule saw every reading of the event type | Constants filter events | `test_rules_only_see_their_measure` |
| Each rule is its own computation | All rules writing `alert` shared one timer, so only the first `alert` rule in the file ever fired | Each rule keeps its own windows | `test_rules_with_the_same_head_fire_independently` |
| Sliding windows "evaluated on each event emit" (README §8.3) | Never evaluated | Count and time windows | `test_sliding_count_window`, `test_sliding_time_window` |
| Fire time `@ (s + 31 MIN)` | Ignored | Honoured | `test_tumbling_window_start_fire_time_and_aggregates` |
| Composite rules need both conditions (README §11, "NPK_CRITICAL requires both") | Each clause fired on its own (OR) | Join on shared variables (AND) | `test_join_and_chaining` |
| The head constant names the alert (`DROUGHT_STRESS`) | Not recorded; every alert looked the same | Written to the head's attribute (`alert_type`) | `test_tumbling_window_start_fire_time_and_aggregates` |
| Rules read `sensor_reading` (the repo's own `agriculture.iotdl` used undeclared `sensor_reading_soil` / `_environment`) | Those rules could never fire | Example rules rewritten; undeclared sources are errors | `test_rule_errors` |

## Interpretations

Where the spec sheet is silent or contradicts itself, the code takes these
readings (the first three were confirmed with the project owner):

1. **`sliding(s, N)` without a unit holds the N most recent readings**
   (README §6.5). With a unit (`sliding(s, 6 HOURS)`) it is a time window.
   The original engine used N ticks.
2. **`FAILURE <duration>` in a `DISTRIBUTION` is a mean sensor lifetime.**
   Each device's sensor for that measure fails at an exponentially
   distributed time with that mean and is not reported afterwards; a device
   with no working sensors stops. `FAILURE p` in `ARRIVALS` stays the
   per-reading drop probability (README §6.3).
3. **Ingest delay is Exponential(mean = MAXDELAY / 2)**, so about 13.5% of
   readings are later than MAXDELAY and get `"stale": true`.
4. **STICKY**: the tier is a Markov chain whose long-run frequencies equal
   the declared `PROB` values; a STICKY tier holds with probability 0.9
   (`--sticky-persistence`), and transitions into or out of a STICKY tier
   move the mean over 5 readings (`--sticky-ramp`). The README gives the
   behaviour but not these numbers.
5. **Packed readings take their timing from the first measure's `ARRIVALS`**
   and carry the first measure's name in `measure_type`. Differing
   `ARRIVALS` for the other measures produce a warning.
6. **Constants vs variables in rules**: a name in capitals of two or more
   characters (`DROUGHT_STRESS`) or a declared measure name = constant;
   everything else = variable, including single letters like `T` (every
   spec-sheet example follows this). Constants match ignoring case and
   underscores.
7. **Constraints filter events before aggregation** (Datalog body
   semantics), not the aggregate.
8. **`s` is the window's start**, so `tumbling(s, 30 MIN) @ (s + 31 MIN)`
   fires one minute after the window closes, as the slide's example reads.
   A fire time earlier than the evaluation moment is moved up to it.
9. **A shared time variable in a join means equal timestamps** (`@ z` in
   both clauses).

## Not covered

- Multiple event streams are future work on the slides; a stream listing
  several event types still routes devices by name fragments, with a
  warning.
- The hospital example the README describes is not in the repository.
- `engine/simulator.py` is the original engine, kept for reference only.
- Measures transmitted `ON_CHANGE` / `ON_THRESHOLD` are not checked by the
  correctness analyser, because those modes transmit a value-dependent
  subset of readings.

## Extensions beyond the spec sheet

| Extension | What it does | Test |
|---|---|---|
| Measurement ontology (`compiler/ontology/measurements.sql`) | `MEASURES` must name a quantity from a fixed SQL table (SI base, derived and dimensionless quantities), `IN` one of its units, and `FOLLOWING` the distribution it samples. Unknown quantities, wrong units and physically impossible `RANGE`s are compile errors. | `test_ontology.py` |
| Device table required by the validator | A schema with no table `REFERENCES DEVICE_CLASS`, with two, or with a device table lacking exactly one `PRIMARY KEY` is rejected before any data is read. | `ValidationTests` in `test_resources.py` |
| Battery `DECAY` on a device class | Per-device battery life drawn from a distribution; the spread of readings grows to `NOISE`x along a curve; the device goes silent when the battery is empty. | `test_decay.py` |
| Several event types in one stream | Each device class is tagged with the listed event type whose name contains its measure or class name; the analyser checks all of them. | `test_each_class_gets_its_own_event_type` |
