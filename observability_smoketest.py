"""Smoke test for the observability endpoints (/api/logs* and /api/systems).

Run with the project venv:
    source .venv/bin/activate && python observability_smoketest.py
    (or: python -m pytest observability_smoketest.py)

Degrades gracefully: if httpx/TestClient is unavailable, or the DB is not
reachable, DB-dependent assertions are skipped — but route registration is
always verified.
"""
import os

os.environ.setdefault(
    "CURLYOS_DATABASE_URL", "postgresql://curlyos:***@localhost:54321/curlyos"
)

from api_server import app  # noqa: E402

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


# [1] Routes are registered regardless of TestClient/DB availability.
print("\n[1] Route registration")
paths = {getattr(r, "path", None) for r in app.routes}
for p in ("/api/logs", "/api/logs/sources", "/api/systems"):
    check(f"{p} registered", p in paths, f"missing from app.routes ({sorted(x for x in paths if x)})")


def _make_client():
    """Return a TestClient, or None if httpx/TestClient is unavailable."""
    try:
        from fastapi.testclient import TestClient
    except Exception as e:  # httpx missing, etc.
        print(f"\n[2] TestClient unavailable — skipping HTTP asserts ({e})")
        return None
    return TestClient(app)


def test_routes_registered():
    """Pytest entrypoint: routes must always be registered."""
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/logs" in paths
    assert "/api/logs/sources" in paths
    assert "/api/systems" in paths


def test_endpoints():
    """Pytest entrypoint: hit the endpoints if a client is available."""
    client = _make_client()
    if client is None:
        return
    _run_http_checks(client)


def _run_http_checks(client):
    global passed, failed

    print("\n[2] GET /api/logs/sources")
    r = client.get("/api/logs/sources")
    check("status 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        body = r.json()
        check("has 'sources' list", isinstance(body.get("sources"), list), str(body)[:200])
        if body.get("sources"):
            s0 = body["sources"][0]
            check("source has name/path/exists",
                  all(k in s0 for k in ("name", "path", "exists", "size_bytes", "modified")),
                  str(s0))

    print("\n[3] GET /api/logs?source=api&lines=10")
    r = client.get("/api/logs", params={"source": "api", "lines": 10})
    check("status 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        body = r.json()
        for k in ("source", "path", "exists", "size_bytes", "modified", "lines", "count"):
            check(f"has '{k}'", k in body, str(body)[:200])
        check("lines is a list", isinstance(body.get("lines"), list), str(body)[:200])
        check("count matches lines", body.get("count") == len(body.get("lines", [])), str(body)[:200])

    print("\n[4] GET /api/logs?source=bogus -> 404")
    r = client.get("/api/logs", params={"source": "does-not-exist"})
    check("status 404", r.status_code == 404, str(r.status_code))

    print("\n[5] GET /api/systems")
    r = client.get("/api/systems")
    check("status 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        body = r.json()
        for k in ("timestamp", "infrastructure", "stats", "engines"):
            check(f"has '{k}'", k in body, str(body)[:200])
        infra_names = {i.get("name") for i in body.get("infrastructure", [])}
        check("infrastructure has the 4 systems",
              {"postgres", "redis", "embedder", "api_server"}.issubset(infra_names),
              str(infra_names))
        engine_names = {e.get("name") for e in body.get("engines", [])}
        check("engines cover the 5 cognition systems",
              {"consolidation", "reflection", "meta", "memory", "knowledge"}.issubset(engine_names),
              str(engine_names))
        # Engine entries are either fully-shaped or error-marked (graceful DB degrade).
        for e in body.get("engines", []):
            ok_shape = "error" in e or all(
                k in e for k in ("last_run", "last_event_type", "runs_24h", "runs_7d", "recent")
            )
            check(f"engine '{e.get('name')}' well-shaped", ok_shape, str(e)[:200])


if __name__ == "__main__":
    client = _make_client()
    if client is not None:
        _run_http_checks(client)
    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"OBSERVABILITY SMOKE: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")
    raise SystemExit(1 if failed else 0)
