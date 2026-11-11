# Release validation and customer acceptance

## Performed checks

`VALIDATION.json` records the checks actually performed on this customer release. The source reference passed live private AIDP chat, a linked synthetic tool call/result and five native-KB/private-vLLM RAG acceptance cases; no demonstration state, credentials or evidence files are included, and this is not a customer-tenancy deployment result.

Local checks include Python compilation, shell syntax, Terraform formatting/initialization/validation, mock-provider plans, controller/configuration/ownership tests, real LangGraph entry tests, RAG ownership/grounding/citation/wallet checks, Streamlit tests and release-content/integrity checks. They do not provision or destroy customer cloud resources.

## Reproduce checks

```bash
python3 scripts/verify-release.py
python3 scripts/validate.py
```

The first command verifies distributed source hashes. It reports intentional source edits as differences. The second may download the locked provider, initializes local test directories and runs source/controller/mock checks, with no OCI requests or applies.

For optional UI and agent tests, use a supported Python with available platform wheels; the reference test runners used Python 3.13:

```bash
cd apps/streamlit
./setup.sh
cd ../..

python3 -m venv .local/agent-tests
.local/agent-tests/bin/python -m pip install \
  -r integration/aidp/requirements.txt

python3 scripts/validate.py \
  --app-python apps/streamlit/.venv/bin/python \
  --agent-python .local/agent-tests/bin/python
```

Use a locally installed supported Python interpreter explicitly when creating these environments if `python3` is too new for dependency wheels. OCI CLI uses its own Python/SDK installation for signing binary uploads; do not install application dependencies into that environment.

## Required customer live acceptance

- Correct OCI identity, compartment ancestry, subscribed region and current capacity/limits.
- Customer image cloud-init, per-instance identity, driver/DCGM and both GPUs visible inside the serving container.
- FSS persistence, private authenticated chat and strict synthetic tool round trip.
- Public web TLS, authentication denial/success, WebSocket/UI behavior, exporter targets and dashboard queries.
- Private Lakehouse availability and AIDP Active/AI features Ready.
- Private workspace subnet/NSG binding, attached Active AI Compute, trusted private DNS/HTTPS and Credential Store authorization.
- Deployed agent acceptance with all four true proof fields, plus an independent Playground test if required by the customer.
- If RAG is enabled: native KB/source identity, successful ingestion with expected file counts, all five Aster acceptance cases and a cited answer from the separate RAG Playground.
- For later documents: upload plus successful manual re-ingestion, expected changed facts/citations and unsupported-question checks.
- Full cleanup, retained storage/backup review, Vault retention deadlines and final service-side verification.

A complete create/destroy cycle in a new customer tenancy has not been executed by preparing this source release. High availability, production identity integration, customer-data SQL/RAG and performance guarantees are outside the demonstrated POC acceptance.

The packaged RAG suite runs with `--agent-python` when `integration/rag/tests` is present. Inspect `deployments/NAME/rag/rag-acceptance.json` after `python3 integration/rag/rag.py verify`; this is a live signed endpoint check, separate from offline release validation. See [DEPLOYMENT.md](DEPLOYMENT.md) for the native UI steps and [CLEANUP.md](CLEANUP.md) for vector/document deletion order.
