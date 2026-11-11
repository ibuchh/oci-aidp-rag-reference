# AIDP integration guide

Use [DEPLOYMENT.md](DEPLOYMENT.md) for the complete customer deployment, [IAM.md](IAM.md) for service and operator policies, and [AIDP_WORKSPACE_AUTOMATION.md](AIDP_WORKSPACE_AUTOMATION.md) for detailed managed-runtime phases.

The supported model path uses an AIDP Credential Store Vault reference, exact-instance/secret/key IAM, private `inference.lab` DNS and verified TLS. The agent runs on Oracle-managed AI Compute; the model runs on the standalone customer A10.2 VM. The initial tool returns synthetic inventory and does not query the Lakehouse.

Do not populate direct-runtime principal fields from a managed compute token. After prerequisites, `python3 scripts/aidp_workspace.py connect --wait-minutes 30` creates/resumes and verifies the coded-agent path. Use `invoke-inference` for routine acceptance afterward.

Preserve both Terraform state and `aidp-workspace.json`. Follow [CLEANUP.md](CLEANUP.md) before removing infrastructure. No automatic runtime destroy command is included in this release.
