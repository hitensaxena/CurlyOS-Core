-- Dispatcher watermarks.
--
-- Each handler processes events after its own last_seq. Delivery is
-- at-least-once: a handler that fails mid-batch keeps the watermark at the
-- last success and retries the failed event on the next poll, so handlers
-- must be idempotent.

CREATE TABLE IF NOT EXISTS event_dispatch_watermarks (
    scope       text NOT NULL,
    handler     text NOT NULL,
    last_seq    bigint NOT NULL DEFAULT 0,
    last_error  text,
    error_count integer NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, handler)
);
