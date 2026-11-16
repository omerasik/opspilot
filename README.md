# OpsPilot

OpsPilot reviews vendor quotes against a procurement catalog and a spending limit. A planning step defines the workflow, an MCP client reads the catalog, a document step extracts the quoted total, and a validator prepares a brief. An operator approves or rejects the brief.

## Run locally

Use Python 3.12.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
DEMO_MODE=true .venv/bin/python -m uvicorn opspilot.app:from_env --factory --host 127.0.0.1
```

On PowerShell, use `.venv/Scripts/python.exe` and set `$env:DEMO_MODE = 'true'` before starting Uvicorn. Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Submit the sample quote with the transient failure checkbox selected. Wait for the backoff and catalog retry, inspect the output and approve the brief.

For Docker, build with `docker build -t opspilot .` and run with `docker run --rm -p 127.0.0.1:8000:8000 -e DEMO_MODE=true -v opspilot-data:/app/data opspilot`.

## Workflow and API

| Method | Path | Result |
| --- | --- | --- |
| POST | `/api/runs` | Submits a quote. An optional `Idempotency-Key` prevents duplicate jobs. |
| GET | `/api/runs` | Lists the latest 100 runs. |
| GET | `/api/runs/{id}` | Reads a run's current stage, outputs and trace. |
| POST | `/api/runs/{id}/decision` | Takes `{"decision":"approve"}`, `reject` or `cancel`. |
| POST | `/api/worker/tick` | Processes one ready stage. |

All run operations require `Authorization: Bearer <operator-token>`. The demo uses an embedded sample token. For deployment, disable demo mode, set a random `OPERATOR_TOKEN` with at least 32 characters and set `DATABASE_PATH` to a persistent location.

SQLite stores the state machine, retry deadlines, lease tokens and trace. Each step checkpoints before the next can start. A worker can reclaim an expired lease, while the old worker's token cannot save a stale result. Catalog attempts are reserved durably before transport starts and count towards the per-run budget. The SDK launches a project-owned MCP STDIO process, negotiates a session and can invoke only the validated `lookup_vendor` operation.

The application uses deterministic planning and analysis for this procurement template. The catalog contains synthetic vendors and the quote must contain one `Total: EUR 375.00` style line. The approval records the operator's decision and makes zero purchase or payment calls. This application uses one operator role and one local database.

## Verify

```sh
python -m pytest -q
ruff check opspilot tests
python -m pip_audit
```

The tests run the actual SDK client and catalog subprocess in the main workflow, simulate a temporary failure and a restart, and verify the approval gate and contention between workers. The execution trace reports local tool attempts and zero external model expense. [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
