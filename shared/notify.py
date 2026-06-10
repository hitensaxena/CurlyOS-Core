"""NotifyPort — how CurlyOS-Core reaches a human, without knowing how.

The contract (curlyos-final/07 §2, Port 3): core calls `notify(text, ...)`;
an adapter delivers it. Core never imports anything Hermes/Telegram-shaped —
delete Hermes and notifications degrade to the log + the webapp surfaces,
nothing else changes (P1 replaceability).

Implementations:
  NullNotifier   — logs the notification; always available; the default.
  HermesNotifier — Phase A: POSTs to the Hermes relay so approvals/nudges
                   reach Telegram. Selecting it before then falls back to
                   NullNotifier with a warning (fail-safe, never fail-closed —
                   a notification is never worth blocking cognition for).

Selection: CURLYOS_NOTIFIER env var ("null" default | "hermes").
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("curlyos-core.notify")


class Notifier:
    """Abstract notifier. Returns True when the message was actually delivered
    to an external surface (False = logged only)."""

    async def notify(self, text: str, *, approval_id: str | None = None,
                     run_id: str | None = None) -> bool:
        raise NotImplementedError


class NullNotifier(Notifier):
    """Log-only delivery — the always-works floor."""

    async def notify(self, text: str, *, approval_id: str | None = None,
                     run_id: str | None = None) -> bool:
        refs = " ".join(f"{k}={v}" for k, v in
                        (("approval", approval_id), ("run", run_id)) if v)
        log.info("NOTIFY%s: %s", f" [{refs}]" if refs else "", text)
        return False


def get_notifier() -> Notifier:
    kind = os.environ.get("CURLYOS_NOTIFIER", "null").strip().lower()
    if kind in ("", "null", "log"):
        return NullNotifier()
    if kind == "hermes":
        log.warning("CURLYOS_NOTIFIER=hermes selected but HermesNotifier lands "
                    "in Phase A — using NullNotifier")
        return NullNotifier()
    log.warning("unknown CURLYOS_NOTIFIER=%r — using NullNotifier", kind)
    return NullNotifier()
