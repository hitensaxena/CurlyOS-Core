"""Hermes-level smoketest — uses the CurlyOSMemoryProvider directly."""
import os
import json

os.environ.setdefault("CURLYOS_DATABASE_URL", "postgresql://curlyos:***@localhost:54321/curlyos")

import sys
# Import from curlyos-core directly (standalone test, not inside Hermes)
sys.path.insert(0, os.path.expanduser("~/curlyos-core"))
sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/curlyos"))
# Stub out the Hermes agent + tools modules that the plugin imports
import types, sys as _sys
_agent_mod = types.ModuleType("agent")
_memprov_mod = types.ModuleType("agent.memory_provider")
class _DummyMemProvider:
    pass
_memprov_mod.MemoryProvider = _DummyMemProvider
_agent_mod.memory_provider = _memprov_mod
_sys.modules["agent"] = _agent_mod
_sys.modules["agent.memory_provider"] = _memprov_mod
_tools_mod = types.ModuleType("tools")
_reg_mod = types.ModuleType("tools.registry")
def _dummy_tool_error(msg): return {"error": msg}
_reg_mod.tool_error = _dummy_tool_error
_tools_mod.registry = _reg_mod
_sys.modules["tools"] = _tools_mod
_sys.modules["tools.registry"] = _reg_mod
from __init__ import CurlyOSMemoryProvider

passed = 0
failed = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        print(f"  ❌ {label} — {detail}")

p = CurlyOSMemoryProvider()

print("\n[1] Provider initialization")
check("is_available", p.is_available())
check("name", p.name == "curlyos")
check("5 tool schemas", len(p.get_tool_schemas()) == 5)

p.initialize("smoketest-session-001", user_id="hiten")
check("scope set", "usr_hiten" in p._scope_text)

print("\n[2] Add facts via provider")
r1 = p.handle_tool_call("curlyos_add_fact", {"statement": "Mintrix AI is Hiten's primary project"})
d1 = json.loads(r1)
check("fact stored", d1.get("result") == "Fact stored.", r1)

r2 = p.handle_tool_call("curlyos_add_fact", {"statement": "Hiten uses Zed as primary code editor"})
d2 = json.loads(r2)
check("second fact stored", d2.get("result") == "Fact stored.", r2)

r3 = p.handle_tool_call("curlyos_add_fact", {"statement": "Hiten loves techno music"})
d3 = json.loads(r3)
check("third fact stored", d3.get("result") == "Fact stored.", r3)

print("\n[3] Recall via provider (BM25 path — embeddings still fake)")
r4 = p.handle_tool_call("curlyos_recall", {"query": "What is Hiten working on?", "k": 5})
d4 = json.loads(r4)
check("recall executed", "results" in d4 or "error" not in d4, r4[:200])
print(f"  ℹ️ recall results: {d4.get('count', 0)} items")

print("\n[4] Identity context")
r5 = p.handle_tool_call("curlyos_identity", {})
d5 = json.loads(r5)
check("identity executed", "identity" in d5, r5[:200])

print("\n[5] Add note")
r6 = p.handle_tool_call("curlyos_add_note", {"content": "Session plan: build curlyos memory engine for hermes", "title": "CurlyOS build plan"})
d6 = json.loads(r6)
check("note stored", "id" in d6, r6)

print("\n[6] Invalidate a fact")
if d2.get("id"):
    r7 = p.handle_tool_call("curlyos_invalidate", {"mem_id": d2["id"], "reason": "Hiten switched back to VS Code"})
    d7 = json.loads(r7)
    check("fact invalidated", "Fact invalidated" in d7.get("result", ""), r7)

print("\n[7] Session sync (auto-record episode)")
p.sync_turn("What are you working on?", "I'm building CurlyOS Core, a cognitive architecture for Hermes.")
check("turn recorded", p._turn_count == 1)

total = passed + failed
print(f"\n{'='*60}")
print(f"HERMES PROVIDER SMOKE: {passed}/{total} passed, {failed} failed")
print(f"{'='*60}")
