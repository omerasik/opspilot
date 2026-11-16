import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .store import Store
from .tools import CatalogClient, ToolFault

log = logging.getLogger(__name__)
STAGES = ["plan", "retrieve_vendor", "analyze_document", "validate", "approval"]


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vendor_id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,40}$")]
    quote: Annotated[str, Field(min_length=1, max_length=3000)]
    budget_eur: Annotated[Decimal, Field(gt=0, le=1_000_000, decimal_places=2)]
    tool_budget: Annotated[int, Field(ge=1, le=3)] = 3
    fail_first: bool = False


def analyze_quote(text: str) -> dict:
    pattern = r"(?im)^\s*Total\s*:\s*EUR\s+(\d{1,9}(?:\.\d{1,2})?)\s*$"
    amounts = re.findall(pattern, text)
    if len(amounts) != 1:
        raise ToolFault("quote_total_missing_or_conflicting")
    try:
        total = Decimal(amounts[0]).quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ToolFault("invalid_quote_amount") from error
    return {"total_eur": str(total), "evidence": f"Total: EUR {amounts[0]}"}


class Orchestrator:
    def __init__(self, store: Store, tool: CatalogClient):
        self.store = store
        self.tool = tool

    async def tick(self):
        claimed = self.store.claim()
        if claimed is None:
            return None
        run, lease = claimed
        state = run["state"]
        stage = state["stage"]
        attempt = state["attempt"]
        try:
            result = await self.execute(stage, run["payload"], state)
        except ToolFault as error:
            retry = error.retryable and attempt < 3
            self.store.event(state, "step_retry" if retry else "step_failed", code_detail=error.code)
            self.store.checkpoint(run, lease, "queued" if retry else "failed", 2 ** (attempt - 1) if retry else 0)
        else:
            state["outputs"][stage] = result
            self.store.event(state, "step_completed")
            state["stage"] = STAGES[STAGES.index(stage) + 1]
            state["attempt"] = 0
            status = "awaiting_approval" if state["stage"] == "approval" else "queued"
            if status == "awaiting_approval":
                self.store.event(state, "approval_requested")
            self.store.checkpoint(run, lease, status)
        log.info("workflow_step run_id=%s stage=%s attempt=%d", run["id"], stage, attempt)
        return self.store.get(run["id"])

    async def execute(self, stage: str, payload: dict, state: dict) -> dict:
        if stage == "plan":
            return {"objective": "Verify a vendor quote against the procurement catalog and spending limit.",
                    "steps": STAGES[1:]}
        if stage == "retrieve_vendor":
            self.store.event(state, "tool_call", tool="lookup_vendor", reservation=state["tool_calls"])
            if payload["fail_first"] and state["attempt"] == 1:
                raise ToolFault("simulated_catalog_timeout", retryable=True)
            vendor = await self.tool.call("lookup_vendor", payload["vendor_id"])
            return vendor.model_dump()
        if stage == "analyze_document":
            return analyze_quote(payload["quote"])
        vendor = state["outputs"]["retrieve_vendor"]
        quote = state["outputs"]["analyze_document"]
        flags = []
        if not vendor["approved"]:
            flags.append("Vendor is pending catalog approval.")
        if Decimal(quote["total_eur"]) > Decimal(payload["budget_eur"]):
            flags.append("Quoted amount exceeds the spending limit.")
        return {"vendor": vendor["name"], "total_eur": quote["total_eur"], "budget_eur": payload["budget_eur"],
                "flags": flags, "recommendation": "escalate" if flags else "approve draft",
                "estimated_tool_cost_usd": 0}
