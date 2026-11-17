-- IoTSim measurement ontology
--
-- Every MEASURES clause names a quantity from this table, and its IN clause a
-- unit of that quantity. The compiler loads this file into an in-memory
-- SQLite database and rejects any quantity or unit that is not here.
--
-- quantity  : one kind of physical quantity (SI base quantities first, then
--             derived and dimensionless ones), named after QUDT quantity kinds
-- dimension : SI dimension (L length, M mass, T time, I current, Θ temperature,
--             N amount of substance, J luminous intensity; 1 = dimensionless)
-- si_unit   : the coherent SI unit
-- default_unit : the unit used when MEASURES has no IN clause
-- min_si / max_si : physical bounds in the SI unit (NULL = unbounded).
--             A device class RANGE outside them is a compile error.
--
-- unit: si_value = value * scale + offset
--
-- To add a quantity or a unit, add rows here; nothing else changes.

CREATE TABLE quantity (
    name          TEXT PRIMARY KEY,
    dimension     TEXT NOT NULL,
    si_unit       TEXT NOT NULL,
    default_unit  TEXT NOT NULL,
    min_si        REAL,
    max_si        REAL,
    note          TEXT
);

CREATE TABLE unit (
    quantity   TEXT NOT NULL REFERENCES quantity(name),
    name       TEXT NOT NULL,          -- written after IN (case-insensitive)
    symbol     TEXT NOT NULL,          -- may also be written after IN, quoted: IN "°C"
    scale      REAL NOT NULL,
    offset     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (quantity, name)
);

INSERT INTO quantity VALUES
-- SI base quantities
('Length',                 'L',             'm',       'm',          NULL, NULL, 'distance, depth, level'),
('Mass',                   'M',             'kg',      'kg',         0,    NULL, 'load cells, scales'),
('Duration',               'T',             's',       's',          0,    NULL, 'elapsed time'),
('ElectricCurrent',        'I',             'A',       'A',          NULL, NULL, NULL),
('Temperature',            'Θ',             'K',       'kelvin',     0,    NULL, 'thermodynamic temperature'),
('AmountOfSubstance',      'N',             'mol',     'mol',        0,    NULL, NULL),
('LuminousIntensity',      'J',             'cd',      'cd',         0,    NULL, NULL),
-- derived quantities
('Area',                   'L2',            'm²',      'm2',         0,    NULL, NULL),
('Volume',                 'L3',            'm³',      'm3',         0,    NULL, NULL),
('Speed',                  'L T-1',         'm/s',     'm_per_s',    0,    NULL, 'wind, flow, vehicle speed'),
('Acceleration',           'L T-2',         'm/s²',    'm_per_s2',   NULL, NULL, 'vibration, motion'),
('Angle',                  '1',             'rad',     'radian',     NULL, NULL, 'direction, tilt'),
('Frequency',              'T-1',           'Hz',      'hertz',      0,    NULL, 'heart rate, respiratory rate, rotation'),
('Force',                  'M L T-2',       'N',       'newton',     NULL, NULL, NULL),
('Pressure',               'M L-1 T-2',     'Pa',      'pascal',     NULL, NULL, 'air, blood, line pressure'),
('Energy',                 'M L2 T-2',      'J',       'joule',      NULL, NULL, NULL),
('Power',                  'M L2 T-3',      'W',       'watt',       NULL, NULL, NULL),
('Voltage',                'M L2 T-3 I-1',  'V',       'volt',       NULL, NULL, 'ECG, battery'),
('ElectricalConductivity', 'M-1 L-3 T3 I2', 'S/m',     'S_per_m',    0,    NULL, 'soil salinity, water quality'),
('Illuminance',            'J L-2',         'lx',      'lux',        0,    NULL, NULL),
('LuminousFlux',           'J',             'lm',      'lumen',      0,    NULL, NULL),
('Irradiance',             'M T-3',         'W/m²',    'W_per_m2',   0,    NULL, 'solar radiation'),
('MassConcentration',      'M L-3',         'kg/m³',   'kg_per_m3',  0,    NULL, 'particulates, dissolved solids'),
('VolumetricFlowRate',     'L3 T-1',        'm³/s',    'm3_per_s',   0,    NULL, NULL),
('PrecipitationRate',      'L T-1',         'm/s',     'm_per_s',    0,    NULL, 'rainfall'),
-- dimensionless quantities
('AmountFraction',         '1',             'mol/mol', 'mol_per_mol', 0,   1,    'gas concentration: CO2, ammonia, ethylene'),
('MassFraction',           '1',             'kg/kg',   'kg_per_kg',  0,    1,    'soil nutrients'),
('VolumeFraction',         '1',             'm³/m³',   'm3_per_m3',  0,    1,    'soil moisture (VWC)'),
('RelativeHumidity',       '1',             '1',       'ratio',      0,    1,    NULL),
('OxygenSaturation',       '1',             '1',       'ratio',      0,    1,    'SpO2'),
('Acidity',                '1',             'pH',      'pH',         0,    14,   'logarithmic; no SI conversion'),
('SoundPressureLevel',     '1',             'dB',      'dB',         NULL, NULL, 'logarithmic; no SI conversion'),
('Count',                  '1',             '1',       'count',      0,    NULL, 'discrete count'),
('CountRate',              'T-1',           '1/s',     'per_second', 0,    NULL, 'insects per hour, events per minute');

INSERT INTO unit (quantity, name, symbol, scale, offset) VALUES
('Length', 'm', 'm', 1, 0), ('Length', 'cm', 'cm', 0.01, 0), ('Length', 'mm', 'mm', 0.001, 0),
('Length', 'km', 'km', 1000, 0), ('Length', 'inch', 'in', 0.0254, 0), ('Length', 'ft', 'ft', 0.3048, 0),
('Mass', 'kg', 'kg', 1, 0), ('Mass', 'g', 'g', 0.001, 0), ('Mass', 'mg', 'mg', 1e-6, 0),
('Mass', 'lb', 'lb', 0.45359237, 0),
('Duration', 's', 's', 1, 0), ('Duration', 'ms', 'ms', 0.001, 0), ('Duration', 'min', 'min', 60, 0),
('Duration', 'hour', 'h', 3600, 0), ('Duration', 'day', 'd', 86400, 0),
('ElectricCurrent', 'A', 'A', 1, 0), ('ElectricCurrent', 'mA', 'mA', 0.001, 0),
('Temperature', 'kelvin', 'K', 1, 0), ('Temperature', 'celsius', '°C', 1, 273.15),
('Temperature', 'fahrenheit', '°F', 0.5555555555555556, 255.37222222222223),
('AmountOfSubstance', 'mol', 'mol', 1, 0),
('LuminousIntensity', 'cd', 'cd', 1, 0),
('Area', 'm2', 'm²', 1, 0), ('Area', 'ha', 'ha', 10000, 0),
('Volume', 'm3', 'm³', 1, 0), ('Volume', 'L', 'L', 0.001, 0), ('Volume', 'mL', 'mL', 1e-6, 0),
('Speed', 'm_per_s', 'm/s', 1, 0), ('Speed', 'km_per_h', 'km/h', 0.2777777777777778, 0),
('Speed', 'mph', 'mph', 0.44704, 0), ('Speed', 'knot', 'kn', 0.5144444444444445, 0),
('Acceleration', 'm_per_s2', 'm/s²', 1, 0), ('Acceleration', 'g_n', 'g', 9.80665, 0),
('Angle', 'radian', 'rad', 1, 0), ('Angle', 'degree', '°', 0.017453292519943295, 0),
('Frequency', 'hertz', 'Hz', 1, 0), ('Frequency', 'per_minute', '/min', 0.016666666666666666, 0),
('Frequency', 'bpm', 'bpm', 0.016666666666666666, 0), ('Frequency', 'rpm', 'rpm', 0.016666666666666666, 0),
('Force', 'newton', 'N', 1, 0),
('Pressure', 'pascal', 'Pa', 1, 0), ('Pressure', 'hPa', 'hPa', 100, 0), ('Pressure', 'kPa', 'kPa', 1000, 0),
('Pressure', 'bar', 'bar', 100000, 0), ('Pressure', 'atm', 'atm', 101325, 0),
('Pressure', 'psi', 'psi', 6894.757293168, 0), ('Pressure', 'mmHg', 'mmHg', 133.322387415, 0),
('Energy', 'joule', 'J', 1, 0), ('Energy', 'Wh', 'Wh', 3600, 0), ('Energy', 'kWh', 'kWh', 3600000, 0),
('Power', 'watt', 'W', 1, 0), ('Power', 'kW', 'kW', 1000, 0),
('Voltage', 'volt', 'V', 1, 0), ('Voltage', 'mV', 'mV', 0.001, 0),
('ElectricalConductivity', 'S_per_m', 'S/m', 1, 0), ('ElectricalConductivity', 'dS_per_m', 'dS/m', 0.1, 0),
('ElectricalConductivity', 'mS_per_cm', 'mS/cm', 0.1, 0), ('ElectricalConductivity', 'uS_per_cm', 'µS/cm', 0.0001, 0),
('Illuminance', 'lux', 'lx', 1, 0),
('LuminousFlux', 'lumen', 'lm', 1, 0),
('Irradiance', 'W_per_m2', 'W/m²', 1, 0),
('MassConcentration', 'kg_per_m3', 'kg/m³', 1, 0), ('MassConcentration', 'mg_per_L', 'mg/L', 0.001, 0),
('MassConcentration', 'mg_per_m3', 'mg/m³', 1e-6, 0), ('MassConcentration', 'ug_per_m3', 'µg/m³', 1e-9, 0),
('VolumetricFlowRate', 'm3_per_s', 'm³/s', 1, 0), ('VolumetricFlowRate', 'L_per_min', 'L/min', 1.6666666666666667e-5, 0),
('PrecipitationRate', 'm_per_s', 'm/s', 1, 0), ('PrecipitationRate', 'mm_per_hour', 'mm/h', 2.777777777777778e-7, 0),
('PrecipitationRate', 'mm_per_day', 'mm/d', 1.1574074074074074e-8, 0),
('AmountFraction', 'mol_per_mol', 'mol/mol', 1, 0), ('AmountFraction', 'percent', '%', 0.01, 0),
('AmountFraction', 'ppm', 'ppm', 1e-6, 0), ('AmountFraction', 'ppb', 'ppb', 1e-9, 0),
('MassFraction', 'kg_per_kg', 'kg/kg', 1, 0), ('MassFraction', 'percent', '%', 0.01, 0),
('MassFraction', 'mg_per_kg', 'mg/kg', 1e-6, 0), ('MassFraction', 'ppm', 'ppm', 1e-6, 0),
('VolumeFraction', 'm3_per_m3', 'm³/m³', 1, 0), ('VolumeFraction', 'percent', '%', 0.01, 0),
('RelativeHumidity', 'ratio', '1', 1, 0), ('RelativeHumidity', 'percent', '%', 0.01, 0),
('OxygenSaturation', 'ratio', '1', 1, 0), ('OxygenSaturation', 'percent', '%', 0.01, 0),
('Acidity', 'pH', 'pH', 1, 0),
('SoundPressureLevel', 'dB', 'dB', 1, 0),
('Count', 'count', '1', 1, 0),
('CountRate', 'per_second', '1/s', 1, 0), ('CountRate', 'per_minute', '1/min', 0.016666666666666666, 0),
('CountRate', 'per_hour', '1/h', 0.0002777777777777778, 0), ('CountRate', 'per_day', '1/d', 1.1574074074074074e-5, 0);
