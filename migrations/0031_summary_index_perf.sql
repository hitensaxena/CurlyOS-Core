-- Summary-index lookup support.
--
-- load_summary_index() ran a scope-wide index scan over ~28k memories and
-- filtered three LIKE prefixes (129 ms), then joined every conversation to
-- memories (9 ms). The partial index holds only the ~1.5k summary rows, so the
-- prefix filter is an index lookup instead of a scan-and-discard.

CREATE INDEX IF NOT EXISTS idx_memories_summary_prefix
    ON memories (scope, statement text_pattern_ops)
    WHERE valid_to IS NULL
      AND (
          statement LIKE 'ChatGPT conversation %'
          OR statement LIKE 'ChatGPT document %'
          OR statement LIKE 'ChatGPT Site %'
      );

CREATE INDEX IF NOT EXISTS idx_chatgpt_conv_summary_mem
    ON chatgpt_conversations (summary_mem_id)
    WHERE summary_mem_id IS NOT NULL;
