-- migrate:up
-- Card 65 (issue #16, T15): v1's military-aircraft chart uses its own hand-written list of aircraft type codes,
-- which is not the same set as aircraft_type.is_military (that flag also holds seven codes v1 never listed: EA18,
-- EUFI, FA18, HAWK, K35R, KC10, KC30, and is used by other charts such as card 61). So the card 65 list gets its own
-- flag and is_military is left alone. The 225 codes below are the exact-match entries of v1's SQL IN list (v1 card 65,
-- docs/reference/v1_metabase_cards.json). The 24 wildcard entries in that list ('F-%', 'MQ%' and so on) never matched
-- anything in v1 either, because a LIKE pattern inside IN (...) is compared as plain text, so they are not carried over.
-- Codes the lookup lacks are inserted (as military) so the list is complete wherever the migration runs.
ALTER TABLE aircraft_type ADD COLUMN IF NOT EXISTS in_v1_card65_list boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN aircraft_type.in_v1_card65_list IS
    'True for the aircraft type codes in the IN list of v1 Metabase card 65 (most popular military aircraft). Separate from is_military, which is the broader flag used by other charts. To add a type, add a new migration; see docs/RUNBOOKS/update-reference-files.md.';

INSERT INTO aircraft_type (icao, category, is_military, in_v1_card65_list)
SELECT code, 'Military', true, true
FROM unnest(ARRAY[
  'A10', 'A12', 'A4', 'A400', 'A6', 'A7', 'AH1', 'AH1Z', 'AH6', 'AH64', 'AH64E', 'AN124', 'AN225', 'AV8', 'B1',
  'B1B', 'B2', 'B21', 'B21A', 'B2A', 'B52', 'B52H', 'C130', 'C130J', 'C141', 'C141B', 'C160', 'C160R', 'C17',
  'C17A', 'C27', 'C27J', 'C295', 'C295M', 'C5', 'C5M', 'CH47', 'CH53', 'CH60', 'E2', 'E2C', 'E3', 'E3A', 'E4',
  'E4B', 'E6', 'E6B', 'E8', 'E8C', 'EF2K', 'F111', 'F111F', 'F117', 'F117A', 'F14', 'F15', 'F15C', 'F15D',
  'F15E', 'F15EX', 'F15I', 'F15K', 'F15S', 'F15SG', 'F16', 'F16A', 'F16B', 'F16C', 'F16D', 'F16E', 'F16F',
  'F16I', 'F16V', 'F18', 'F18C', 'F18D', 'F18E', 'F18F', 'F18S', 'F18T', 'F18U', 'F18V', 'F18W', 'F18X', 'F18Y',
  'F18Z', 'F22', 'F22A', 'F35', 'F35A', 'F35B', 'F35C', 'F4', 'F4E', 'F5', 'F5E', 'F5F', 'FGR4', 'GR4', 'GRIP',
  'H1', 'H2', 'H3', 'H47', 'H53', 'H6', 'H60', 'H64', 'H8', 'HAR', 'HERC', 'IL76', 'JAG', 'M2K', 'MH47', 'MH53',
  'MH6', 'MH60', 'MH8', 'MIG21', 'MIG21B', 'MIG21F', 'MIG21M', 'MIG21S', 'MIG21U', 'MIG21UM', 'MIG23', 'MIG25',
  'MIG29', 'MIG31', 'MIG35', 'MIG41', 'MIR2', 'MIR4', 'MQ1', 'MQ1B', 'MQ1C', 'MQ1D', 'MQ9', 'MQ9A', 'MQ9B',
  'MQ9C', 'P1', 'P1A', 'P3', 'P3C', 'P8', 'P8A', 'RAF', 'RAPH', 'RC135', 'RQ11', 'RQ11B', 'RQ170', 'RQ180',
  'RQ4', 'RQ4A', 'RQ7', 'RQ7B', 'SR71', 'SR71A', 'SR72', 'SU27', 'SU27M', 'SU27S', 'SU27SM', 'SU27SM3',
  'SU27UB', 'SU27UBM', 'SU30', 'SU33', 'SU34', 'SU35', 'SU37', 'SU57', 'SU75', 'T1', 'T10', 'T11', 'T12', 'T13',
  'T14', 'T1A', 'T38', 'T38A', 'T45', 'T45A', 'T50', 'T6', 'T6A', 'T7', 'T8', 'T9', 'TORN', 'TR1', 'TR1A',
  'TU160', 'TU160M', 'TU22', 'TU22M', 'TU95', 'TU95MS', 'TYPH', 'U2', 'U2R', 'U2S', 'UH1', 'UH60', 'UH72',
  'VIGG', 'X35', 'X35A', 'X35B', 'X35C', 'X35D', 'X47', 'X47A', 'X47B', 'X47C', 'YF22', 'YF22A', 'YF22B',
  'YF23', 'YF23A', 'YF23B'
]) AS code
ON CONFLICT (icao) DO UPDATE SET in_v1_card65_list = true;

DO $$
BEGIN
    IF (SELECT count(*) FROM aircraft_type WHERE in_v1_card65_list) <> 225 THEN
        RAISE EXCEPTION 'card 65 list should hold 225 codes, found %', (SELECT count(*) FROM aircraft_type WHERE in_v1_card65_list);
    END IF;
END $$;

-- migrate:down
-- Forward-only by convention, but this one is safely reversible: it only drops the flag column.
ALTER TABLE aircraft_type DROP COLUMN IF EXISTS in_v1_card65_list;
