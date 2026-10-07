-- migrate:up
INSERT INTO controller_rating (id, short, long) VALUES
    (-1, 'INAC', 'Inactive'),
    (0,  'SUS',  'Suspended'),
    (1,  'OBS',  'Observer'),
    (2,  'S1',   'Tower Trainee'),
    (3,  'S2',   'Tower Controller'),
    (4,  'S3',   'Senior Student'),
    (5,  'C1',   'Enroute Controller'),
    (6,  'C2',   'Controller 2 (not in use)'),
    (7,  'C3',   'Senior Controller'),
    (8,  'I1',   'Instructor'),
    (9,  'I2',   'Instructor 2 (not in use)'),
    (10, 'I3',   'Senior Instructor'),
    (11, 'SUP',  'Supervisor'),
    (12, 'ADM',  'Administrator')
ON CONFLICT (id) DO NOTHING;

INSERT INTO facility (id, short, long) VALUES
    (0, 'OBS', 'Observer'),
    (1, 'FSS', 'Flight Service Station'),
    (2, 'DEL', 'Clearance Delivery'),
    (3, 'GND', 'Ground'),
    (4, 'TWR', 'Tower'),
    (5, 'APP', 'Approach/Departure'),
    (6, 'CTR', 'Enroute')
ON CONFLICT (id) DO NOTHING;

-- Pilot ratings are bit flags (the feed value is the bitmask).
INSERT INTO pilot_rating (id, short, long) VALUES
    (0,  'NEW',  'Basic Member'),
    (1,  'PPL',  'Private Pilot Licence'),
    (3,  'IR',   'Instrument Rating'),
    (7,  'CMEL', 'Commercial Multi-Engine Licence'),
    (15, 'ATPL', 'Airline Transport Pilot Licence'),
    (31, 'FI',   'Flight Instructor'),
    (63, 'FE',   'Flight Examiner')
ON CONFLICT (id) DO NOTHING;

-- migrate:down
-- Forward-only project: no automatic rollback.
