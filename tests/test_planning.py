import asyncio
import json
import time

import httpx2 as httpx
import pytest

from opspilot.planning import ModelPlanner, rule_plan
from opspilot.store import Store
from opspilot.tools import CatalogClient, ToolFault
from opspilot.workflow import Orchestrator, Submission

PAYLOAD = Submission(vendor_id="leaf-works", quote="Total: EUR 375.50", budget_eur="400").model_dump(mode="json")


def provider_result(plan: dict) -> dict:
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 120}}


def test_valid_model_plan_drives_the_actual_allowlisted_mcp_call(tmp_path):
    candidate = rule_plan(PAYLOAD)
    candidate.pop("provider")
    candidate["focus"] = "budget_limit"

    def respond(request: httpx.Request):
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer test-planner-key"
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["messages"][0]["role"] == "system"
        assert json.loads(body["messages"][1]["content"])["vendor_id"] == "leaf-works"
        return httpx.Response(200, json=provider_result(candidate))

    planner = ModelPlanner("https://planner.invalid", "test-planner-key", "mock-planner", httpx.MockTransport(respond))
    assert "test-planner-key" not in repr(planner)
    store = Store(tmp_path / "plans.sqlite")
    run = store.submit(PAYLOAD, "planned")
    workflow = Orchestrator(store, CatalogClient(), planner)

    async def execute():
        for _ in range(4):
            await workflow.tick()

    asyncio.run(execute())
    result = store.get(run["id"])
    assert result["status"] == "awaiting_approval"
    assert result["state"]["outputs"]["plan"]["provider"] == "openai"
    assert result["state"]["outputs"]["plan"]["usage"]["prompt_tokens"] == 80
    assert result["state"]["outputs"]["retrieve_vendor"]["vendor_id"] == "leaf-works"
    assert result["state"]["outputs"]["validate"]["total_eur"] == "375.50"
    assert result["state"]["tool_calls"] == 1


@pytest.mark.parametrize("mutation", ["unknown_tool", "wrong_vendor", "omit_approval", "extra_argument"])
def test_rejects_plans_that_bypass_the_permitted_workflow(mutation: str):
    candidate = rule_plan(PAYLOAD)
    candidate.pop("provider")
    if mutation == "unknown_tool":
        candidate["tool_call"]["name"] = "make_payment"
    elif mutation == "wrong_vendor":
        candidate["tool_call"]["arguments"]["vendor_id"] = "other-vendor"
    elif mutation == "extra_argument":
        candidate["tool_call"]["arguments"]["command"] = "unapproved"
    else:
        candidate["steps"][-1] = "validate"
    planner = ModelPlanner("https://planner.invalid", "test-only", "mock", httpx.MockTransport(
        lambda _: httpx.Response(200, json=provider_result(candidate)),
    ))
    with pytest.raises(ToolFault, match="planner_(invalid_response|forbidden_plan)"):
        asyncio.run(planner.plan(PAYLOAD))


def test_rate_limit_is_persisted_and_invalid_responses_do_not_expose_provider_content(tmp_path):
    planner = ModelPlanner("https://planner.invalid", "test-only", "mock", httpx.MockTransport(
        lambda _: httpx.Response(429, headers={"Retry-After": "7"}, text="sensitive response"),
    ))
    store = Store(tmp_path / "retry.sqlite")
    run = store.submit(PAYLOAD, "rate-limit")
    before = time.time()
    asyncio.run(Orchestrator(store, CatalogClient(), planner).tick())
    result = store.get(run["id"])
    assert result["status"] == "queued"
    assert result["state"]["history"][-1]["code_detail"] == "planner_rate_limited"
    with store.connect() as connection:
        assert connection.execute("SELECT available FROM runs WHERE id=?", (run["id"],)).fetchone()[0] >= before + 7
    assert store.claim() is None
    for response in [httpx.Response(200, content=b"x" * 40_000), httpx.Response(401, text="sensitive response")]:
        invalid = ModelPlanner("https://planner.invalid", "test-only", "mock", httpx.MockTransport(lambda _, response=response: response))
        with pytest.raises(ToolFault) as failure:
            asyncio.run(invalid.plan(PAYLOAD))
        assert "sensitive" not in str(failure.value)


def test_malformed_tool_arguments_are_rejected_before_transport():
    with pytest.raises(ToolFault, match="invalid_vendor_id"):
        asyncio.run(CatalogClient().call("lookup_vendor", "../../other"))
