# Invoice Action A2A Agent

## Local setup

Use Python 3.12+, create a virtual environment, then install `requirements.txt`.
Set `GROQ_API_KEY`, a strong `A2A_BEARER_TOKEN`, and `A2A_BASE_URL` in the
process environment. `A2A_BASE_URL` must be the externally deployed HTTPS URL
including `/a2a/`, for example `https://agent.example.com/a2a/`. Do not commit
secrets. Run with `uvicorn main:app --host 0.0.0.0 --port 8000`.

For multiple principals, `A2A_BEARER_TOKENS` may contain a JSON object mapping
each bearer token to a stable principal name. The single `A2A_BEARER_TOKEN`
setting remains available for a single-principal installation.
In production mode, startup rejects a missing bearer configuration, Groq key,
or a non-HTTPS `A2A_BASE_URL` that does not end in `/a2a/`.

The database defaults to `invoice_agent.db` beside `storage.py`; set
`A2A_DB_PATH` to a durable writable volume in deployments.

## Tests

Run `python -m pytest -q`. The normal suite mocks Groq. The live smoke test is
opt-in: set `RUN_GROQ_TESTS=1` and `GROQ_API_KEY` before running pytest.

## HTTPS deployment

Build and run with Docker Compose on a host that provides an HTTPS reverse
proxy or managed TLS ingress. Configure DNS and TLS for the public hostname,
then set `A2A_BASE_URL` to the exact public HTTPS interface URL and configure
`GROQ_API_KEY` and a randomly generated `A2A_BEARER_TOKEN` through the host's
secret manager (not a checked-in `.env` file). `compose.yaml` mounts the SQLite
database on the persistent `invoice-data` volume. Back up that volume and test
restore procedures. After ingress is live, verify the agent card's
`supportedInterfaces[0].url` matches the public URL. Deployment and publishing
are intentionally not performed here.

## Continuation envelope

Result continuations carry `taskId` and `contextId` on the message, and
`batchId` plus `results` in the results Part data. Each result carries
`packageId`, `actionId`, `action`, `outcome` (`ACCEPTED` or `REJECTED`), and
the grader's `receiptNonce` when accepted. The receipts Part data contains
`batchId` and `executions`; each execution copies the stored proposal's
package, action, facts, and evidence references and preserves that nonce.

The assignment attachment supplied requirements but no real package fixtures.
The local decision tests use a tiny synthetic unit fixture only to check typed
validation; they do not establish decision accuracy on the grader's invoice
packages or its precise source-document encoding.
