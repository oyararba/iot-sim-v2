-- Constructs copied from the README language reference (sections 6.1-6.5,
-- 11) and the MS defense slides, as written there. tests/test_spec_conformance.py
-- checks that all of it parses, validates and runs.

-- README 6.1
CREATE DEVICE CLASS SoilMoistureSensor (
    MEASURES VolumeFraction
        IN          "%"
        FOLLOWING   Moisture
        RANGE       (0.0, 100.0)
        RESOLUTION  0.1,
    zone ENUM *
)
CREATE DEVICE CLASS NPKSensor (
    MEASURES MassFraction IN mg_per_kg FOLLOWING Nitrogen RANGE (0.0, 999.0) RESOLUTION 1.0,
    MEASURES MassFraction IN mg_per_kg FOLLOWING Phosphorus RANGE (0.0, 999.0) RESOLUTION 1.0,
    MEASURES MassFraction IN mg_per_kg FOLLOWING Potassium RANGE (0.0, 999.0) RESOLUTION 1.0,
    zone ENUM *
)
-- Slides
CREATE DEVICE CLASS WindSensor (
 MEASURES Speed
 IN   m_per_s
 FOLLOWING WindSpeed
 RANGE (0.0, 60.0)
 RESOLUTION 0.1,
 MEASURES Angle
 IN   degree
 FOLLOWING WindDirection
 RANGE (0.0, 360.0)
 RESOLUTION 1.0,
 zone ENUM *
)
CREATE DEVICE CLASS AirTempSensor (
 MEASURES Temperature IN celsius FOLLOWING AirTemp RANGE (-20.0, 60.0) RESOLUTION 0.1,
 zone ENUM *
)
CREATE DEVICE CLASS RainSensor (
 MEASURES PrecipitationRate IN mm_per_hour FOLLOWING Rainfall RANGE (0.0, 300.0) RESOLUTION 0.2,
 zone ENUM *
)
CREATE TABLE zones ( zone_id INTEGER PRIMARY KEY, zone_name STRING )
CREATE TABLE plots ( plot_id INTEGER PRIMARY KEY, zone_id INTEGER REFERENCES zones(zone_id) )
CREATE TABLE Device ( device_id STRING PRIMARY KEY, device_class STRING REFERENCES DEVICE_CLASS,
                      plot_id INTEGER REFERENCES plots(plot_id) )
-- README 6.2
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
CREATE EVENTTYPE metric_avg (
    EVENTTIME T INTEGER,
    device_id Device(device_id) FK,
    measure_type ENUM,
    avg_value FLOAT
)
-- README 6.3 + slides
CREATE EVENTSTREAM agriculture_stream (
    EVENTTYPE  sensor_reading,
    ARRIVALS (
        Moisture      ON_CHANGE   (2.0)      FAILURE 0.10,
        AirTemp       PERIODIC    (5 MIN)    FAILURE 0.05,
        Nitrogen      PERIODIC    (1 DAY)    FAILURE 0.12,
        Phosphorus    PERIODIC    (1 DAY)    FAILURE 0.12,
        Potassium     PERIODIC    (1 DAY)    FAILURE 0.12,
        Rainfall      ON_THRESHOLD(0.2)      FAILURE 0.07,
        WindSpeed     PERIODIC    (1 MIN)    FAILURE 0.04,
        WindDirection PERIODIC(1 MIN)
    )
)
-- README 6.4 + slides
DISTRIBUTION FOR AirTemp (
    FAILURE 360 DAYS
    WHERE zone = 1 ( // 1: Irrigated
        NORMAL (mean = 22.0, std_dev = 4.0)   PROB 0.85  STICKY,
        ABOVE (
            NORMAL (mean = 40.0, std_dev = 3.0) PROB 0.08  STICKY
            -- heatwave builds and fades gradually
        )
        BELOW (
            NORMAL (mean = 1.0,  std_dev = 1.5) PROB 0.07  STICKY
        )
    )
    WHERE ZONE = 2 (
        NORMAL (mean = 24.0, std_dev = 3.0)   PROB 0.92  STICKY,
        ABOVE ( NORMAL (mean = 38.0, std_dev = 2.0) PROB 0.05  STICKY )
        BELOW ( NORMAL (mean = 2.0,  std_dev = 1.0) PROB 0.03  STICKY )
    )
)
DISTRIBUTION FOR Rainfall (
    WHERE ZONE = 1 (
        NORMAL (mean = 0.0, std_dev = 0.0)   PROB 0.85,
        ABOVE (
            EXPONENTIAL (mean = 8.0)          PROB 0.15
        )
    )
    WHERE ZONE = 2 (
        NORMAL (mean = 0.0, std_dev = 0.0)   PROB 0.85,
        ABOVE ( EXPONENTIAL (mean = 5.0) PROB 0.15 )
    )
)
-- README 11 (BELOW before NORMAL)
DISTRIBUTION FOR Moisture (
    WHERE ZONE = 1 (
        BELOW ( NORMAL (mean = 25.0, std_dev = 0.5) PROB 0.99 )
        NORMAL (mean = 60.0, std_dev = 5.0) PROB 0.01
    )
    WHERE ZONE = 2 (
        NORMAL (mean = 40.0, std_dev = 5.0) PROB 1.0
    )
)
DISTRIBUTION FOR Nitrogen (
    WHERE ZONE = 1 (
        BELOW ( NORMAL (mean = 40.0, std_dev = 10.0) PROB 0.90 )
        NORMAL (mean = 180.0, std_dev = 30.0) PROB 0.10
    )
    WHERE ZONE = 2 ( NORMAL (mean = 150.0, std_dev = 20.0) PROB 1.0 )
)
DISTRIBUTION FOR Phosphorus (
    WHERE ZONE = 1 (
        BELOW ( NORMAL (mean = 15.0, std_dev = 5.0) PROB 0.90 )
        NORMAL (mean = 80.0, std_dev = 18.0) PROB 0.10
    )
    WHERE ZONE = 2 ( NORMAL (mean = 60.0, std_dev = 10.0) PROB 1.0 )
)
DISTRIBUTION FOR Potassium (
    NORMAL (mean = 160.0, std_dev = 28.0) PROB 1.0
)
DISTRIBUTION FOR WindSpeed (
    NORMAL (mean = 3.5, std_dev = 2.0) PROB 0.9
    ABOVE ( NORMAL (mean = 22.0, std_dev = 3.0) PROB 0.1 )
)
DISTRIBUTION FOR WindDirection (
    UNIFORM (low = 0.0, high = 360.0)   PROB 1.0
)
-- README 6.5 + slides
new metric_avg[sliding(s, 6)](device_id, MOISTURE,
    (avg_value = avg(value))) @ (s + 7) :-
    sensor_reading(device_id, MOISTURE, value, _, _) @ z;

new alert[tumbling(s, 30 MIN)](device_id, DROUGHT_STRESS,
    (value = avg(value))) @ (s + 31 MIN) :-
    sensor_reading(device_id, MOISTURE, value, _, _) @ z,
    value < 25;

new alert(device_id, FROST_RISK) @ (T + 1) :-
    sensor_reading(device_id, AIR_TEMP, value, _) @ T,
    value < 3;

new report[tumbling(s, 60 MIN)](device_id, IRRIGATION_NEEDED) @ (s + 61 MIN) :-
    alert(device_id, DROUGHT_STRESS, value) @ z,
    value < 25;

new alert[tumbling(s, 1 DAY)](device_id, NITROGEN_DEFICIENCY, (value = avg(value))) @ (s + 1 DAY) :-
    sensor_reading(device_id, NITROGEN, value, _, _) @ z, value < 80;
new alert[tumbling(s, 1 DAY)](device_id, PHOSPHORUS_DEFICIENCY, (value = avg(value2))) @ (s + 1 DAY) :-
    sensor_reading(device_id, NITROGEN, _, value2, _) @ z, value2 < 40;
new report[tumbling(s, 1 DAY)](device_id, NPK_CRITICAL) @ (s + 1 DAY) :-
    alert(device_id, NITROGEN_DEFICIENCY, _) @ z,
    alert(device_id, PHOSPHORUS_DEFICIENCY, _) @ z;

new alert[sliding(s, 10 MIN)](device_id, STORM, (value = max(value))) @ (s + 10 MIN) :-
    sensor_reading(device_id, WIND_SPEED, value, _, _) @ z, value > 20;
