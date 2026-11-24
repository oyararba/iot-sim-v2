CREATE TABLE Fridge_Zones (
    zone_id INTEGER PRIMARY KEY,
    zone_name ENUM,
    zone_location ENUM
);

CREATE TABLE Sensor_Deployment (
    device_id STRING PRIMARY KEY,
    device_class STRING REFERENCES DEVICE_CLASS,
    zone_id INTEGER REFERENCES Fridge_Zones(zone_id)
);

CREATE TABLE Fridge_Inventory (
    ItemID STRING PRIMARY KEY,
    ItemType ENUM,
    ItemName STRING,
    zone_id INTEGER REFERENCES Fridge_Zones(zone_id),
    DateAdded INTEGER
);
CREATE EVENTTYPE Ammonia_Reading (
    EVENTTIME T INTEGER,
    device_id STRING,
    zone_id INTEGER,
    value FLOAT
);

CREATE EVENTTYPE Ethylene_Reading (
    EVENTTIME T INTEGER,
    device_id STRING,
    zone_id INTEGER,
    value FLOAT
);


CREATE EVENTTYPE High_Ammonia_Alert (
    EVENTTIME T INTEGER,
    zone_id INTEGER,
    avg_ppm FLOAT
);

CREATE EVENTTYPE Ammonia_Avg(
    EVENTTIME T INTEGER,
    zone_id INTEGER,
    avg_ppm FLOAT
);

CREATE EVENTTYPE Ethylene_Avg(
    EVENTTIME T INTEGER,
    zone_id INTEGER,
    avg_ppm FLOAT
);

CREATE EVENTTYPE ChickenSpoiledAlert (
    EVENTTIME T INTEGER,
    ItemID STRING,
    zone_id INTEGER,
    avg_ammonia_ppm FLOAT,
    age_seconds INTEGER
);

CREATE EVENTTYPE ProduceOverripeAlert (
    EVENTTIME T INTEGER,
    ItemID STRING,
    zone_id INTEGER,
    avg_ethylene_ppm FLOAT,
    age_seconds INTEGER
);

CREATE DEVICE CLASS Ethylene_Sensor (
    MEASURES Ethylene
    UNIT "ppm"
    RANGE (0.0, 100.0)
    RESOLUTION 0.05
    zone ENUM *
);

CREATE DEVICE CLASS Ammonia_Sensor (
    MEASURES Ammonia
    UNIT "ppm"
    RANGE (0.0, 50.0)
    RESOLUTION 0.1
    zone ENUM *
);

CREATE EVENTSTREAM fridge_sensor_stream (
    EVENTTYPE Ammonia_Reading, Ethylene_Reading
    ARRIVALS (
        Ammonia PERIODIC (1 MIN) FAILURE 0.02,
        Ethylene PERIODIC (1 MIN) FAILURE 0.02
    )
);

DISTRIBUTION FOR Ammonia (
    WHERE zone = 1 (
        NORMAL (mean = 0.8, std_dev = 0.2) PROB 0.85
        ABOVE (
            NORMAL (mean = 18.5, std_dev = 3.0) PROB 0.12
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.2) PROB 0.03
        )
    )
);

DISTRIBUTION FOR Ethylene (
    WHERE zone = 2 (
        NORMAL (mean = 0.8, std_dev = 0.3) PROB 0.85
        ABOVE (
            NORMAL (mean = 20.0, std_dev = 3.5) PROB 0.10
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.2) PROB 0.05
        )
    )
    WHERE zone = 3 (
        NORMAL (mean = 2.0, std_dev = 0.5) PROB 0.80
        ABOVE (
            NORMAL (mean = 25.0, std_dev = 4.5) PROB 0.15
        )
        BELOW (
            UNIFORM (low = 0.0, high = 0.3) PROB 0.05
        )
    )
);
