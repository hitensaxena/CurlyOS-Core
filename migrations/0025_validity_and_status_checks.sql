-- Bi-temporal and epistemic integrity.
--
-- These invariants were enforced only in application code (or not at all).
-- Existing data satisfies them (checked 2026-11-16), so the constraints can be
-- validated immediately. NOT VALID + VALIDATE keeps the lock window short.
--
-- The epistemic-status set matches shared.epistemic.ALL_STATUSES. `seed`,
-- `conjecture` and `possible_world` are included even when no current row uses
-- them, because simulation and speculation write them.

ALTER TABLE memories
    ADD CONSTRAINT memories_valid_interval
    CHECK (valid_to IS NULL OR valid_to >= valid_from) NOT VALID;
ALTER TABLE memories VALIDATE CONSTRAINT memories_valid_interval;

ALTER TABLE memories
    ADD CONSTRAINT memories_epistemic_status_check
    CHECK (epistemic_status IN (
        'seed', 'conjecture', 'hypothesis', 'canonical', 'belief', 'possible_world'
    )) NOT VALID;
ALTER TABLE memories VALIDATE CONSTRAINT memories_epistemic_status_check;

ALTER TABLE identity_facts
    ADD CONSTRAINT identity_facts_valid_interval
    CHECK (valid_to IS NULL OR valid_to >= valid_from) NOT VALID;
ALTER TABLE identity_facts VALIDATE CONSTRAINT identity_facts_valid_interval;

ALTER TABLE identity_facts
    ADD CONSTRAINT identity_facts_epistemic_status_check
    CHECK (epistemic_status IN (
        'seed', 'conjecture', 'hypothesis', 'canonical', 'belief', 'possible_world'
    )) NOT VALID;
ALTER TABLE identity_facts VALIDATE CONSTRAINT identity_facts_epistemic_status_check;

ALTER TABLE knowledge_entities
    ADD CONSTRAINT knowledge_entities_valid_interval
    CHECK (valid_to IS NULL OR valid_to >= valid_from) NOT VALID;
ALTER TABLE knowledge_entities VALIDATE CONSTRAINT knowledge_entities_valid_interval;

ALTER TABLE knowledge_edges
    ADD CONSTRAINT knowledge_edges_valid_interval
    CHECK (valid_to IS NULL OR valid_to >= valid_from) NOT VALID;
ALTER TABLE knowledge_edges VALIDATE CONSTRAINT knowledge_edges_valid_interval;
