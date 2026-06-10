-- 0005_decision_properties.sql — extensibility seam on decisions (Phase X).
-- Council-mode stress-test reports land at properties.council; future
-- decision annotations accrue without schema churn (same pattern as
-- goals.properties). Additive only.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS properties jsonb NOT NULL DEFAULT '{}';
