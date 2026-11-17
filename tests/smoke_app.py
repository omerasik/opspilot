"""Run the offline procurement demo against a local running application."""

import argparse
import json
import time
from http.client import HTTPException
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    args = parser.parse_args()
    origin = args.url.rstrip("/")
    if urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Use a loopback demo server.")

    def call(path, body=None):
        request = Request(origin + "/api/" + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer opspilot-local-demo", "Content-Type": "application/json",
                                   "Idempotency-Key": str(uuid4())})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    for attempt in range(30):
        try:
            call("health")
            break
        except (URLError, ConnectionError, TimeoutError, HTTPException):
            if attempt == 29:
                raise
            time.sleep(1)
    for vendor, budget, decision, status in [("leaf-works", "400", "approve", "completed"),
                                             ("river-studio", "200", "reject", "rejected")]:
        run = call("runs", {"vendor_id": vendor, "quote": "Total: EUR 375.00", "budget_eur": budget,
                            "tool_budget": 3, "fail_first": True})
        path = "runs/" + run["id"]
        for _ in range(40):
            run = call(path)
            if run["status"] in {"awaiting_approval", "failed"}:
                break
            time.sleep(0.5)
        assert run["status"] == "awaiting_approval", run["state"]["history"]
        assert run["state"]["outputs"]["plan"]["provider"] == "deterministic"
        assert run["state"]["outputs"]["retrieve_vendor"]["vendor_id"] == vendor
        assert run["state"]["tool_calls"] == 2
        assert len(run["state"]["outputs"]["validate"]["flags"]) == (0 if decision == "approve" else 2)
        assert call(path + "/decision", {"decision": decision})["status"] == status
    print("PASS: actual MCP, transient retry, business flags, internal approval and rejection")


if __name__ == "__main__":
    main()
