-- ═══════════════════════════════════════════════════════════
-- Precision Agriculture and Autonomous Irrigation
-- IoT-Sim schema + rules (one file)
--
-- Compile (from compiler/):
--   python3 compiler.py --schema examples/precision_agriculture.sql \
--       --db examples/agriculture_resources.db \
--       --sim-end 90 --time-unit days --tick-unit minutes \
--       --seed 42 --output out/
--
-- RESOURCES: the device rows live in the enterprise DB (--db), in the
-- tables declared below with CREATE TABLE.
--
-- Time model:
--   EVENTTIME T is an integer tick index. One tick = --tick-unit,
--   which is minutes here (the fastest sensors report every 1 MIN).
--   Durations take a unit (MS, SEC, MIN, HOUR(S), DAY(S)) in ARRIVALS,
--   rule windows and fire times; a bare number in a tumbling window or
--   fire time is ticks, and in a sliding window it is a count of readings.
-- ═══════════════════════════════════════════════════════════


-- ═══════════════════════════════════════════════════════════
-- DEVICE CLASSES
-- `zone ENUM *` declares the class's one user-defined field.
-- Its value for each device comes from the enterprise DB and
-- selects the matching `WHERE zone = <value>` block of each
-- DISTRIBUTION. A device whose zone has no block is a compile
-- error.
-- Multi-measure classes (WindSensor, NPKSensor) report all of
-- their measures in one sensor_reading: value, value2, value3
-- follow the order of the MEASURES clauses.
-- ═══════════════════════════════════════════════════════════

CREATE DEVICE CLASS SoilMoistureSensor (
    MEASURES    Moisture
    UNIT        percent_VWC
    RANGE       (0.0, 100.0)
    RESOLUTION  0.1,
    zone        ENUM *
)

CREATE DEVICE CLASS SoilTemperatureSensor (
    MEASURES    SoilTemp
    UNIT        celsius
    RANGE       (-10.0, 80.0)
    RESOLUTION  0.1,
    zone        ENUM *
)

CREATE DEVICE CLASS AirTemperatureSensor (
    MEASURES    AirTemp
    UNIT        celsius
    RANGE       (-20.0, 60.0)
    RESOLUTION  0.1,
    zone        ENUM *
)

CREATE DEVICE CLASS HumiditySensor (
    MEASURES    Humidity
    UNIT        percent_RH
    RANGE       (0.0, 100.0)
    RESOLUTION  0.5,
    zone        ENUM *
)

CREATE DEVICE CLASS SolarRadiationSensor (
    MEASURES    SolarRadiation
    UNIT        W_per_m2
    RANGE       (0.0, 1400.0)
    RESOLUTION  1.0,
    zone        ENUM *
)

CREATE DEVICE CLASS RainfallSensor (
    MEASURES    Rainfall
    UNIT        mm_per_hour
    RANGE       (0.0, 300.0)
    RESOLUTION  0.2,
    zone        ENUM *
)

CREATE DEVICE CLASS WindSensor (
    MEASURES    WindSpeed           -- value
    UNIT        m_per_s
    RANGE       (0.0, 60.0)
    RESOLUTION  0.1,

    MEASURES    WindDirection       -- value2
    UNIT        degrees
    RANGE       (0.0, 360.0)
    RESOLUTION  1.0,

    zone        ENUM *
)

CREATE DEVICE CLASS CO2Sensor (
    MEASURES    CO2
    UNIT        ppm
    RANGE       (300.0, 2000.0)
    RESOLUTION  1.0,
    zone        ENUM *
)

CREATE DEVICE CLASS NPKSensor (
    MEASURES    Nitrogen            -- value
    UNIT        mg_per_kg
    RANGE       (0.0, 999.0)
    RESOLUTION  1.0,

    MEASURES    Phosphorus          -- value2
    UNIT        mg_per_kg
    RANGE       (0.0, 999.0)
    RESOLUTION  1.0,

    MEASURES    Potassium           -- value3
    UNIT        mg_per_kg
    RANGE       (0.0, 999.0)
    RESOLUTION  1.0,

    zone        ENUM *
)

CREATE DEVICE CLASS PHSensor (
    MEASURES    PH
    UNIT        pH
    RANGE       (0.0, 14.0)
    RESOLUTION  0.01,
    zone        ENUM *
)

CREATE DEVICE CLASS SalinitySensor (
    MEASURES    Salinity
    UNIT        dS_per_m
    RANGE       (0.0, 20.0)
    RESOLUTION  0.01,
    zone        ENUM *
)

CREATE DEVICE CLASS InsectTrapSensor (
    MEASURES    InsectCount
    UNIT        count_per_hour
    RANGE       (0.0, 500.0)
    RESOLUTION  1.0,
    zone        ENUM *
)


-- ═══════════════════════════════════════════════════════════
-- TABLES (RESOURCES: the enterprise DB)
-- The compiler reads exactly these columns from the --db file
-- (extra columns in the DB are ignored):
--   * devices is the device table: its device_class column
--     REFERENCES DEVICE_CLASS, and its PRIMARY KEY is the id.
--   * `zone ENUM *` is resolved by following REFERENCES out of
--     devices to the nearest column named zone or zone_id:
--         devices.plot_id → plots.zone_id
-- ═══════════════════════════════════════════════════════════

CREATE TABLE zones (
    zone_id         INTEGER PRIMARY KEY,
    zone_name       STRING,
    zone_type       ENUM,       -- IRRIGATED | NON_IRRIGATED | CANOPY | PERIMETER | INDOOR
    area_ha         FLOAT
)

CREATE TABLE plots (
    plot_id         INTEGER PRIMARY KEY,
    zone_id         INTEGER REFERENCES zones(zone_id),
    plot_name       STRING,
    area_ha         FLOAT,
    plot_type       ENUM,       -- OPEN_FIELD | GREENHOUSE | ORCHARD | NURSERY
    soil_type       ENUM,       -- CLAY | LOAM | SANDY | SILT | PEAT
    crop_type       STRING
)

CREATE TABLE devices (
    device_id       STRING PRIMARY KEY,
    device_class    STRING REFERENCES DEVICE_CLASS,
    plot_id         INTEGER REFERENCES plots(plot_id),
    depth_cm        INTEGER     -- NULL for non-soil sensors
)


-- ═══════════════════════════════════════════════════════════
-- EVENT TYPES
-- Readings are tagged with the EVENTSTREAM's EVENTTYPE
-- (sensor_reading). Attributes whose names match a column of the
-- devices table (depth_cm) are filled from the device's row.
-- Rule bodies bind fields by position, in the order declared
-- here (EVENTTIME excluded).
-- ═══════════════════════════════════════════════════════════

CREATE EVENTTYPE sensor_reading (
    EVENTTIME T     INTEGER,
    device_id:      devices(device_id) FK,
    measure_type    ENUM,       -- first MEASURES of the device's class
    value           FLOAT,      -- 1st measure
    value2          FLOAT,      -- 2nd measure: WindDirection | Phosphorus; else NULL
    value3          FLOAT,      -- 3rd measure: Potassium; else NULL
    depth_cm        INTEGER     -- devices.depth_cm; NULL for non-soil sensors
)

CREATE EVENTTYPE metric_avg (
    EVENTTIME T     INTEGER,
    device_id:      devices(device_id) FK,
    measure_type    ENUM,       -- Moisture | AirTemp | Humidity | CO2 | SoilTemp |
                                -- WindSpeed | SolarRadiation | Nitrogen | Phosphorus |
                                -- Potassium | PH | Salinity | InsectCount
    avg_value       FLOAT
)

CREATE EVENTTYPE alert (
    EVENTTIME T     INTEGER,
    device_id:      devices(device_id) FK,
    alert_type      ENUM,       -- DROUGHT_STRESS | WATERLOGGING | FROST_RISK | HEATWAVE |
                                -- DISEASE_RISK | CO2_LOW | CO2_HIGH | STORM |
                                -- NITROGEN_DEFICIENCY | PHOSPHORUS_DEFICIENCY |
                                -- POTASSIUM_DEFICIENCY | PH_LOW | PH_HIGH |
                                -- SALINITY_HIGH | PEST_SURGE
    value           FLOAT
)

CREATE EVENTTYPE report (
    EVENTTIME T     INTEGER,
    device_id:      devices(device_id) FK,
    report_type     ENUM        -- IRRIGATION_NEEDED | DISEASE_WINDOW | NPK_CRITICAL
)


-- ═══════════════════════════════════════════════════════════
-- EVENT STREAM
-- Each line: <Measure> PERIODIC (<n> <UNIT>) FAILURE <p>
--   FAILURE = probability that a reading is dropped, per reading.
--   Units: MS, SEC, MIN, HOUR(S), DAY(S); anything else is an error.
-- Every measure needs a line, including the 2nd/3rd measures of
-- multi-measure devices (keep them identical to the 1st).
-- ═══════════════════════════════════════════════════════════

CREATE EVENTSTREAM agriculture_stream (
    EVENTTYPE sensor_reading,

    ARRIVALS (
        -- Soil
        Moisture        PERIODIC (1 HOUR)       FAILURE 0.04,
        SoilTemp        PERIODIC (10 MIN)       FAILURE 0.0008,
        Nitrogen        PERIODIC (1 DAY)        FAILURE 0.12,
        Phosphorus      PERIODIC (1 DAY)        FAILURE 0.12,
        Potassium       PERIODIC (1 DAY)        FAILURE 0.12,
        PH              PERIODIC (12 HOURS)     FAILURE 0.09,
        Salinity        PERIODIC (12 HOURS)     FAILURE 0.09,
        InsectCount     PERIODIC (1 HOUR)       FAILURE 0.08,

        -- Environment
        AirTemp         PERIODIC (5 MIN)        FAILURE 0.05,
        Humidity        PERIODIC (5 MIN)        FAILURE 0.05,
        CO2             PERIODIC (10 MIN)       FAILURE 0.06,
        SolarRadiation  PERIODIC (1 MIN)        FAILURE 0.04,
        WindSpeed       PERIODIC (1 MIN)        FAILURE 0.04,
        WindDirection   PERIODIC (1 MIN)        FAILURE 0.04,
        Rainfall        PERIODIC (1 HOUR)       FAILURE 0.07
    )
)


-- ═══════════════════════════════════════════════════════════
-- DISTRIBUTIONS
-- Each WHERE ZONE block: the unlabelled NORMAL tier first, then
-- ABOVE / BELOW. Tier probabilities in a block must sum to 1.0.
-- ═══════════════════════════════════════════════════════════

DISTRIBUTION FOR Moisture (
    WHERE zone = 1 (
        NORMAL (mean = 65.0, std_dev = 8.0)           PROB 0.90
        ABOVE (
            NORMAL (mean = 88.0, std_dev = 4.0)       PROB 0.03
        )
        BELOW (
            NORMAL (mean = 18.0, std_dev = 5.0)       PROB 0.07
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 32.0, std_dev = 10.0)          PROB 0.80
        ABOVE (
            NORMAL (mean = 62.0, std_dev = 5.0)       PROB 0.05
        )
        BELOW (
            NORMAL (mean = 8.0, std_dev = 3.0)        PROB 0.15
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 70.0, std_dev = 5.0)           PROB 0.95
        ABOVE (
            NORMAL (mean = 90.0, std_dev = 3.0)       PROB 0.02
        )
        BELOW (
            NORMAL (mean = 48.0, std_dev = 4.0)       PROB 0.03
        )
    )
)

DISTRIBUTION FOR SoilTemp (
    WHERE zone = 1 (
        NORMAL (mean = 18.0, std_dev = 3.0)           PROB 0.92
        ABOVE (
            NORMAL (mean = 50.0, std_dev = 5.0)       PROB 0.04
        )
        BELOW (
            NORMAL (mean = -2.0, std_dev = 2.0)       PROB 0.04
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 18.0, std_dev = 4.0)           PROB 0.90
        ABOVE (
            NORMAL (mean = 52.0, std_dev = 6.0)       PROB 0.06
        )
        BELOW (
            NORMAL (mean = -2.0, std_dev = 2.0)       PROB 0.04
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 22.0, std_dev = 2.0)           PROB 0.96
        ABOVE (
            NORMAL (mean = 38.0, std_dev = 3.0)       PROB 0.02
        )
        BELOW (
            NORMAL (mean = 12.0, std_dev = 2.0)       PROB 0.02
        )
    )
)

DISTRIBUTION FOR AirTemp (
    WHERE zone = 1 (
        NORMAL (mean = 22.0, std_dev = 4.0)           PROB 0.85
        ABOVE (
            NORMAL (mean = 40.0, std_dev = 3.0)       PROB 0.08
        )
        BELOW (
            NORMAL (mean = 1.0, std_dev = 1.5)        PROB 0.07
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 22.0, std_dev = 4.0)           PROB 0.87
        ABOVE (
            NORMAL (mean = 40.0, std_dev = 3.0)       PROB 0.07
        )
        BELOW (
            NORMAL (mean = 1.0, std_dev = 1.5)        PROB 0.06
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 26.0, std_dev = 1.5)           PROB 0.95
        ABOVE (
            NORMAL (mean = 34.0, std_dev = 2.0)       PROB 0.03
        )
        BELOW (
            NORMAL (mean = 15.0, std_dev = 1.5)       PROB 0.02
        )
    )
)

DISTRIBUTION FOR Humidity (
    WHERE zone = 1 (
        NORMAL (mean = 55.0, std_dev = 12.0)          PROB 0.82
        ABOVE (
            NORMAL (mean = 88.0, std_dev = 5.0)       PROB 0.12
        )
        BELOW (
            NORMAL (mean = 12.0, std_dev = 4.0)       PROB 0.06
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 55.0, std_dev = 12.0)          PROB 0.84
        ABOVE (
            NORMAL (mean = 88.0, std_dev = 5.0)       PROB 0.10
        )
        BELOW (
            NORMAL (mean = 12.0, std_dev = 4.0)       PROB 0.06
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 72.0, std_dev = 6.0)           PROB 0.93
        ABOVE (
            NORMAL (mean = 92.0, std_dev = 3.0)       PROB 0.04
        )
        BELOW (
            NORMAL (mean = 42.0, std_dev = 5.0)       PROB 0.03
        )
    )
)

DISTRIBUTION FOR CO2 (
    WHERE zone = 1 (
        NORMAL (mean = 415.0, std_dev = 15.0)         PROB 0.92
        ABOVE (
            NORMAL (mean = 470.0, std_dev = 10.0)     PROB 0.05
        )
        BELOW (
            NORMAL (mean = 360.0, std_dev = 10.0)     PROB 0.03
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 800.0, std_dev = 80.0)         PROB 0.88
        ABOVE (
            NORMAL (mean = 1400.0, std_dev = 120.0)   PROB 0.07
        )
        BELOW (
            NORMAL (mean = 420.0, std_dev = 20.0)     PROB 0.05
        )
    )
)

DISTRIBUTION FOR SolarRadiation (
    WHERE zone = 1 (
        NORMAL (mean = 450.0, std_dev = 150.0)        PROB 0.80
        ABOVE (
            NORMAL (mean = 1050.0, std_dev = 100.0)   PROB 0.10
        )
        BELOW (
            NORMAL (mean = 60.0, std_dev = 30.0)      PROB 0.10
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 280.0, std_dev = 100.0)        PROB 0.82
        ABOVE (
            NORMAL (mean = 780.0, std_dev = 80.0)     PROB 0.08
        )
        BELOW (
            NORMAL (mean = 40.0, std_dev = 20.0)      PROB 0.10
        )
    )
)

DISTRIBUTION FOR WindSpeed (
    WHERE zone = 1 (
        NORMAL (mean = 3.5, std_dev = 2.0)            PROB 0.88
        ABOVE (
            NORMAL (mean = 20.0, std_dev = 8.0)       PROB 0.08
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.5)           PROB 0.04
            -- calm conditions, any value near zero equally likely
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 2.0, std_dev = 1.2)            PROB 0.90
        ABOVE (
            NORMAL (mean = 16.0, std_dev = 6.0)       PROB 0.07
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.5)           PROB 0.03
        )
    )
)

-- No WHERE: one distribution for every zone.
DISTRIBUTION FOR WindDirection (
    UNIFORM (low = 0.0, high = 360.0)                 PROB 1.0
    -- no meaningful ABOVE or BELOW for direction
)

-- No-rain tier: POISSON with mean 0 is a point mass at exactly 0.
-- (UNIFORM (low = 0, high = 0) and a NORMAL with std_dev = 0 are
-- both rejected by the validator.)
DISTRIBUTION FOR Rainfall (
    WHERE zone = 1 (
        POISSON (mean = 0.0)                          PROB 0.85
        ABOVE (
            EXPONENTIAL (mean = 8.0)                  PROB 0.15
            -- rainfall intensity when raining follows
            -- exponential: light rain is most common,
            -- heavy rain is rare but possible
        )
    )
    WHERE zone = 2 (
        POISSON (mean = 0.0)                          PROB 0.85
        ABOVE (
            EXPONENTIAL (mean = 5.0)                  PROB 0.15
            -- canopy intercepts some rain so mean
            -- intensity is lower than at perimeter
        )
    )
)

DISTRIBUTION FOR Nitrogen (
    WHERE zone = 1 (
        NORMAL (mean = 180.0, std_dev = 30.0)         PROB 0.85
        ABOVE (
            NORMAL (mean = 340.0, std_dev = 30.0)     PROB 0.05
        )
        BELOW (
            NORMAL (mean = 55.0, std_dev = 15.0)      PROB 0.10
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 120.0, std_dev = 25.0)         PROB 0.80
        ABOVE (
            NORMAL (mean = 275.0, std_dev = 25.0)     PROB 0.04
        )
        BELOW (
            NORMAL (mean = 38.0, std_dev = 12.0)      PROB 0.16
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 220.0, std_dev = 20.0)         PROB 0.90
        ABOVE (
            NORMAL (mean = 370.0, std_dev = 20.0)     PROB 0.04
        )
        BELOW (
            NORMAL (mean = 60.0, std_dev = 12.0)      PROB 0.06
        )
    )
)

DISTRIBUTION FOR Phosphorus (
    WHERE zone = 1 (
        NORMAL (mean = 80.0, std_dev = 18.0)          PROB 0.85
        ABOVE (
            NORMAL (mean = 165.0, std_dev = 15.0)     PROB 0.05
        )
        BELOW (
            NORMAL (mean = 25.0, std_dev = 8.0)       PROB 0.10
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 55.0, std_dev = 14.0)          PROB 0.80
        ABOVE (
            NORMAL (mean = 132.0, std_dev = 12.0)     PROB 0.05
        )
        BELOW (
            NORMAL (mean = 15.0, std_dev = 5.0)       PROB 0.15
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 100.0, std_dev = 14.0)         PROB 0.90
        ABOVE (
            NORMAL (mean = 195.0, std_dev = 12.0)     PROB 0.04
        )
        BELOW (
            NORMAL (mean = 28.0, std_dev = 7.0)       PROB 0.06
        )
    )
)

DISTRIBUTION FOR Potassium (
    WHERE zone = 1 (
        NORMAL (mean = 160.0, std_dev = 28.0)         PROB 0.85
        ABOVE (
            NORMAL (mean = 300.0, std_dev = 25.0)     PROB 0.05
        )
        BELOW (
            NORMAL (mean = 40.0, std_dev = 12.0)      PROB 0.10
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 110.0, std_dev = 22.0)         PROB 0.80
        ABOVE (
            NORMAL (mean = 248.0, std_dev = 20.0)     PROB 0.05
        )
        BELOW (
            NORMAL (mean = 25.0, std_dev = 8.0)       PROB 0.15
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 200.0, std_dev = 18.0)         PROB 0.90
        ABOVE (
            NORMAL (mean = 338.0, std_dev = 18.0)     PROB 0.04
        )
        BELOW (
            NORMAL (mean = 42.0, std_dev = 10.0)      PROB 0.06
        )
    )
)

DISTRIBUTION FOR PH (
    WHERE zone = 1 (
        NORMAL (mean = 6.5, std_dev = 0.4)            PROB 0.88
        ABOVE (
            NORMAL (mean = 8.0, std_dev = 0.3)        PROB 0.06
        )
        BELOW (
            NORMAL (mean = 4.8, std_dev = 0.3)        PROB 0.06
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 6.2, std_dev = 0.5)            PROB 0.85
        ABOVE (
            NORMAL (mean = 8.0, std_dev = 0.3)        PROB 0.08
        )
        BELOW (
            NORMAL (mean = 4.5, std_dev = 0.3)        PROB 0.07
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 6.8, std_dev = 0.2)            PROB 0.92
        ABOVE (
            NORMAL (mean = 7.8, std_dev = 0.2)        PROB 0.04
        )
        BELOW (
            NORMAL (mean = 5.2, std_dev = 0.2)        PROB 0.04
        )
    )
)

DISTRIBUTION FOR Salinity (
    WHERE zone = 1 (
        NORMAL (mean = 0.8, std_dev = 0.3)            PROB 0.88
        ABOVE (
            NORMAL (mean = 4.5, std_dev = 1.0)        PROB 0.08
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.1)           PROB 0.04
        )
    )
    WHERE zone = 2 (
        NORMAL (mean = 0.5, std_dev = 0.2)            PROB 0.90
        ABOVE (
            NORMAL (mean = 3.5, std_dev = 0.8)        PROB 0.07
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.1)           PROB 0.03
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 1.2, std_dev = 0.3)            PROB 0.92
        ABOVE (
            NORMAL (mean = 5.0, std_dev = 1.2)        PROB 0.06
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.1)           PROB 0.02
        )
    )
)

DISTRIBUTION FOR InsectCount (
    WHERE zone = 1 (
        POISSON (mean = 12.0)                         PROB 0.82
        -- normal insect activity, count per hour
        -- Poisson because each insect arrives
        -- independently and randomly
        ABOVE (
            POISSON (mean = 85.0)                     PROB 0.12
            -- pest surge, infestation event
        )
        BELOW (
            POISSON (mean = 1.0)                      PROB 0.06
            -- very low activity, cold spell
            -- or after pesticide application
        )
    )
    WHERE zone = 3 (
        POISSON (mean = 3.0)                          PROB 0.90
        ABOVE (
            POISSON (mean = 25.0)                     PROB 0.07
        )
        BELOW (
            POISSON (mean = 0.5)                      PROB 0.03
        )
    )
)


-- ═══════════════════════════════════════════════════════════
-- RULES
-- new <eventtype>[<sliding|tumbling>(s, <size>)](<head>) @ (<fire>) :-
--     <source>(<fields in EVENTTYPE order>) @ z, <constraint>;
-- sensor_reading fields: device_id, measure_type, value, value2,
-- value3, depth_cm. Constants are capitalised or name a measure
-- (Moisture); constraints filter the readings in the window.
-- ═══════════════════════════════════════════════════════════

-- ── Derived metrics ───────────────────────────────────────────

new metric_avg[sliding(s, 6)](device_id, Moisture, (avg_value = avg(value))) @ (s + 7) :-
    sensor_reading(device_id, Moisture, value, _, _, _) @ z;

new metric_avg[sliding(s, 6)](device_id, AirTemp, (avg_value = avg(value))) @ (s + 7) :-
    sensor_reading(device_id, AirTemp, value, _, _, _) @ z;

new metric_avg[sliding(s, 6)](device_id, Humidity, (avg_value = avg(value))) @ (s + 7) :-
    sensor_reading(device_id, Humidity, value, _, _, _) @ z;

new metric_avg[sliding(s, 6)](device_id, InsectCount, (avg_value = avg(value))) @ (s + 7) :-
    sensor_reading(device_id, InsectCount, value, _, _, _) @ z;

-- ── Soil moisture ─────────────────────────────────────────────

new alert[tumbling(s, 30 MIN)](device_id, DROUGHT_STRESS, (value = avg(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, Moisture, value, _, _, _) @ z,
    value < 25;

new alert[tumbling(s, 30 MIN)](device_id, WATERLOGGING, (value = avg(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, Moisture, value, _, _, _) @ z,
    value > 85;

new report[tumbling(s, 1 HOUR)](device_id, IRRIGATION_NEEDED) @ (s + 1 HOUR) :-
    alert(device_id, DROUGHT_STRESS, value) @ z,
    value < 25;

-- ── Air temperature ───────────────────────────────────────────

new alert[tumbling(s, 30 MIN)](device_id, FROST_RISK, (value = avg(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, AirTemp, value, _, _, _) @ z,
    value < 3;

new alert[tumbling(s, 1 HOUR)](device_id, HEATWAVE, (value = avg(value))) @ (s + 1 HOUR) :-
    sensor_reading(device_id, AirTemp, value, _, _, _) @ z,
    value > 38;

-- ── Humidity ──────────────────────────────────────────────────

new alert[tumbling(s, 1 HOUR)](device_id, DISEASE_RISK, (value = avg(value))) @ (s + 1 HOUR) :-
    sensor_reading(device_id, Humidity, value, _, _, _) @ z,
    value > 80;

-- ── CO2 ───────────────────────────────────────────────────────

new alert[tumbling(s, 30 MIN)](device_id, CO2_LOW, (value = avg(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, CO2, value, _, _, _) @ z,
    value < 450;

new alert[tumbling(s, 30 MIN)](device_id, CO2_HIGH, (value = avg(value))) @ (s + 30 MIN) :-
    sensor_reading(device_id, CO2, value, _, _, _) @ z,
    value > 1500;

-- ── Wind ──────────────────────────────────────────────────────

new alert[tumbling(s, 10 MIN)](device_id, STORM, (value = avg(value))) @ (s + 10 MIN) :-
    sensor_reading(device_id, WindSpeed, value, _, _, _) @ z,
    value > 20;

-- ── NPK ───────────────────────────────────────────────────────
-- An NPK reading has measure_type = Nitrogen (its first MEASURES),
-- with Phosphorus in value2 and Potassium in value3.

new alert[tumbling(s, 1 DAY)](device_id, NITROGEN_DEFICIENCY, (value = avg(value))) @ (s + 1 DAY) :-
    sensor_reading(device_id, Nitrogen, value, _, _, _) @ z,
    value < 80;

new alert[tumbling(s, 1 DAY)](device_id, PHOSPHORUS_DEFICIENCY, (value = avg(value2))) @ (s + 1 DAY) :-
    sensor_reading(device_id, Nitrogen, _, value2, _, _) @ z,
    value2 < 40;

new alert[tumbling(s, 1 DAY)](device_id, POTASSIUM_DEFICIENCY, (value = avg(value3))) @ (s + 1 DAY) :-
    sensor_reading(device_id, Nitrogen, _, _, value3, _) @ z,
    value3 < 60;

new report[tumbling(s, 1 DAY)](device_id, NPK_CRITICAL) @ (s + 1 DAY) :-
    alert(device_id, NITROGEN_DEFICIENCY, _) @ z,
    alert(device_id, PHOSPHORUS_DEFICIENCY, _) @ z;

-- ── PH ────────────────────────────────────────────────────────

new alert[tumbling(s, 12 HOURS)](device_id, PH_LOW, (value = avg(value))) @ (s + 12 HOURS) :-
    sensor_reading(device_id, PH, value, _, _, _) @ z,
    value < 5.5;

new alert[tumbling(s, 12 HOURS)](device_id, PH_HIGH, (value = avg(value))) @ (s + 12 HOURS) :-
    sensor_reading(device_id, PH, value, _, _, _) @ z,
    value > 7.5;

-- ── Salinity ──────────────────────────────────────────────────

new alert[tumbling(s, 12 HOURS)](device_id, SALINITY_HIGH, (value = avg(value))) @ (s + 12 HOURS) :-
    sensor_reading(device_id, Salinity, value, _, _, _) @ z,
    value > 4.0;

-- ── Pest ──────────────────────────────────────────────────────

new alert[tumbling(s, 1 HOUR)](device_id, PEST_SURGE, (value = avg(value))) @ (s + 1 HOUR) :-
    sensor_reading(device_id, InsectCount, value, _, _, _) @ z,
    value > 50;

-- ── Combined reports ──────────────────────────────────────────

new report[tumbling(s, 2 HOURS)](device_id, DISEASE_WINDOW) @ (s + 2 HOURS) :-
    alert(device_id, DISEASE_RISK, _) @ z,
    alert(device_id, PEST_SURGE, _) @ z;
