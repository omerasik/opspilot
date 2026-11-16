import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from opspilot.app import DEMO_TOKEN, create_app
from opspilot.store import Store

AUTH = {"Authorization": f"Bearer {DEMO_TOKEN}"}
QUOTE = {"vendor_id": "leaf-works", "quote": "Quote\nTotal: EUR 375.50", "budget_eur": "400"}


def test_recovers_one_tool_failure_then_uses_real_mcp_and_waits_for_approval(tmp_path):
    db = tmp_path / "runs.sqlite"
    with TestClient(create_app(db, DEMO_TOKEN, demo=True, auto_worker=False)) as api:
        assert api.get("/api/runs").status_code == 401
        run = api.post("/api/runs", headers={**AUTH, "Idempotency-Key": "quote-1"},
                       json={**QUOTE, "fail_first": True}).json()
        path = "/api/runs/" + run["id"]
        assert api.post(path + "/decision", json={"decision": "approve"}, headers=AUTH).status_code == 409
        assert api.post("/api/worker/tick", headers=AUTH).json()["state"]["stage"] == "retrieve_vendor"
        retry = api.post("/api/worker/tick", headers=AUTH).json()
        assert retry["state"]["history"][-1]["event"] == "step_retry"
        assert api.post("/api/worker/tick", headers=AUTH).json()["status"] == "idle"
    time.sleep(1.05)
    with TestClient(create_app(db, DEMO_TOKEN, demo=True, auto_worker=False)) as api:
        for _ in range(3):
            current = api.post("/api/worker/tick", headers=AUTH).json()
        assert current["status"] == "awaiting_approval", current["state"]["history"]
        assert current["state"]["outputs"]["retrieve_vendor"]["name"] == "Leaf Works"
        assert current["state"]["outputs"]["validate"]["total_eur"] == "375.50"
        assert current["state"]["tool_calls"] == 2
        assert api.post(path + "/decision", json={"decision": "approve"}, headers=AUTH).json()["status"] == "completed"
        assert api.post("/api/runs", headers={**AUTH, "Idempotency-Key": "quote-1"},
                        json={**QUOTE, "fail_first": True}).json()["id"] == run["id"]
        assert api.post("/api/runs", headers={**AUTH, "Idempotency-Key": "quote-1"},
                        json={**QUOTE, "budget_eur": "390"}).status_code == 409


def test_concurrent_claim_and_recovery_fences_old_attempt(tmp_path):
    one = Store(tmp_path / "runs.sqlite")
    two = Store(tmp_path / "runs.sqlite")
    run_id = one.submit(QUOTE, "one")["id"]
    with ThreadPoolExecutor(2) as executors:
        claims = list(executors.map(lambda store: store.claim(), [one, two]))
    assert sum(claim is not None for claim in claims) == 1
    old, old_token = next(claim for claim in claims if claim is not None)
    with one.connect() as conn:
        conn.execute("UPDATE runs SET lease_until = 0 WHERE id = ?", (run_id,))
    current, token = two.claim()
    assert current["state"]["history"][-1]["event"] == "lease_recovered"
    assert not one.checkpoint(old, old_token, "failed")
    assert two.checkpoint(current, token, "queued")
    assert one.decision(run_id, "cancel")["status"] == "cancelled"
    assert two.claim() is None


def test_rejects_tools_outside_allowlist_and_conflicting_quote(tmp_path):
    import asyncio

    import pytest

    from opspilot.tools import CatalogClient, ToolFault
    from opspilot.workflow import analyze_quote

    with pytest.raises(ToolFault, match="tool_not_allowed"):
        asyncio.run(CatalogClient().call("unknown", "leaf-works"))
    with pytest.raises(ToolFault, match="quote_total_missing_or_conflicting"):
        analyze_quote("Total: EUR 2\nTotal: EUR 8")
    with TestClient(create_app(tmp_path / "runs.sqlite", "operator".ljust(32), auto_worker=False)) as api:
        assert api.post("/api/runs", headers={"Authorization": "Bearer " + "operator".ljust(32)},
                        json={**QUOTE, "fail_first": True}).status_code == 422
