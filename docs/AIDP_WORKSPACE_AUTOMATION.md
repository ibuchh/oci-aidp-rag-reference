# AIDP workspace, AI Compute and coded-agent automation

## Overview

The package uses Terraform for the AIDP instance, private DNS and IAM, plus Oracle’s signed data-plane REST API for the private workspace, AI Compute, Credential Store and agent deployment. Browser uploads and manual runtime-principal configuration are unnecessary for this workflow.

The intended request path is OCI-authenticated AIDP agent endpoint → attached AI Compute → private DNS name → verified HTTPS vLLM API. The agent retrieves its bearer key through an AIDP Credential Store Vault reference. The initial tool returns synthetic GPU inventory; it does not query customer data or the Lakehouse.

The reference deployment passed private chat, linked synthetic tool-call/result and final-answer acceptance. The customer must repeat these checks in their own tenancy. Resource creation alone does not prove connectivity.

## Preconditions / Prerequisites

- Run from this package with the configured API-key OCI profile and a Python-based OCI CLI installation.
- Deploy the inference stack, monitoring/Streamlit, AIDP foundation, IAM, Lakehouse and AIDP instance first.
- Confirm AIDP is Active and its AI feature status is Ready.
- Preserve `poc-config.json`, Terraform states and the resource inventory in `deployments/NAME/aidp-workspace.json`.
- Use synthetic data for the POC.
- Binary source upload uses the OCI SDK installed with the existing Python-based OCI CLI. Session-token support for this binary helper is not implemented yet.

## Procedure

### 1. Prepare trust and publish the existing inference key

```bash
cd "$POC_ROOT"
python3 scripts/poc.py runtime-bundle
python3 scripts/poc.py publish-key
```

Expected: a public CA certificate and non-secret runtime configuration in `deployments/NAME/runtime`, plus metadata identifying the inference secret in Vault. The key is transferred in memory through the existing verified access path; it is not embedded in Terraform or agent source. If these files already exist for the current deployment, inspect and reuse them. Do not rotate the GPU key as a retry.

### 2. Create private DNS

```bash
python3 scripts/poc.py plan --stage aidp-dns
python3 scripts/poc.py apply --stage aidp-dns
```

Expected: two resources, a private zone and A RRSet, in this VCN resolver’s default view. `inference.lab` points to the actual GPU private IP. This compatibility alias is already included in the deployed GPU certificate’s SAN. It is an internal TLS/DNS name; resource and application names retain the POC terminology.

No public DNS record or public GPU endpoint is created. The controller refuses a matching private zone outside this stage’s ownership.

### 3. Deploy the Credential Store Vault policy

```bash
python3 scripts/poc.py plan --stage aidp-credential-iam
python3 scripts/poc.py apply --stage aidp-credential-iam
```

Expected: one policy scoped to this AIDP instance, the existing inference secret and its dedicated encryption key. It permits reading that secret bundle, `SECRET_READ` and `SECRET_UPDATE` for the service’s required governance tagging, and use of that one KMS key. Vault-reference creation succeeded after adding metadata read alongside update; the service first needs the existing tags. All OCIDs come from deployment state/secret metadata. Leave `runtime_principal_type` and `runtime_principal_id` unset for this path.

The direct `runtime-iam` stage is a separate, optional resource-principal approach. The observed AI Compute identity belongs to an Oracle-managed `dataflowcomputecluster` in a service tenancy. The controller refuses to treat that identity as a verified customer-tenancy principal. Credential Store is the path validated here.

### 4. Create or resume the managed runtime with one command

After completing steps 1–3:

```bash
python3 scripts/aidp_workspace.py connect --wait-minutes 30
```

This performs the workspace/compute setup, credential preparation, private HTTPS and Vault probes, self-contained agent publication and real chat/tool acceptance. Re-running an already published deployment waits for readiness and reruns acceptance without replacing its entry with probe code.

The detailed phase commands below are available for inspection and recovery.

### 4a. Workspace, AI Compute and coded agent

```bash
python3 scripts/aidp_workspace.py plan
python3 scripts/aidp_workspace.py apply --wait-minutes 30
python3 scripts/aidp_workspace.py configure-private-dns --wait-minutes 30
```

Expected: workspace `NAME-private` uses the foundation’s exact workspace subnet and NSG. AI Compute has 1 OCPU, 16 GB memory, zero GPUs and one fixed replica. The coded agent is attached to that compute. Provisioning and start operations can take several minutes, including managed software installation.

`configure-private-dns` records the Terraform-owned alias for runtime testing and starts the owned compute if it is stopped. It does not stop running compute or change the workspace’s advanced DNS settings. AIDP rejected custom `dnsZones` in the reference tenancy with `dnsZones is not enabled for the reference tenancy`. The default private-network/VCN-resolver path must pass the runtime check below. If it does not, involve the AIDP service architect before changing network exposure.

Compute and agent names use underscores because the service rejects hyphens in compute names. The workspace name permits hyphens.

### 4b. Restore a legacy baseline agent's missing UI folder

Older packages created the baseline coded agent with the relative workspace path `agents`. If opening it reports `Error fetching workspace object: Unknown resource agents`, inspect the scoped repair:

```bash
python3 scripts/aidp_workspace.py repair-agent-path --config poc-config.json
```

Expected preview: create only the missing empty `/Workspace/agents` folder in the owned workspace. Apply after reviewing the preview:

```bash
python3 scripts/aidp_workspace.py repair-agent-path --config poc-config.json --apply
```

Expected: the legacy path resolves and the agent ID, source paths, AI Compute attachment and deployment state remain unchanged. Refresh the baseline agent in AIDP and check both Development and Playground. This restores workspace navigation; it does not republish the endpoint or move source files. The helper records its folder-creation intent and retry token in the existing inventory, refuses file collisions and never overwrites an existing object. Current releases use the existing absolute `/Workspace/Shared` path for new baseline agents. Native RAG schema/volume creation is a separate operation.

### 5. Prepare Credential Store references and run the probe

```bash
python3 scripts/aidp_workspace.py prepare-credential-probe
python3 scripts/aidp_workspace.py prepare-vault-credential
python3 scripts/aidp_workspace.py upload-probe
python3 scripts/aidp_workspace.py publish-probe
python3 scripts/aidp_workspace.py wait-active --wait-minutes 15
python3 scripts/aidp_workspace.py invoke-probe
```

On a previously published probe, update its packaged code with:

```bash
python3 scripts/aidp_workspace.py upload-probe
python3 scripts/aidp_workspace.py redeploy-probe
python3 scripts/aidp_workspace.py wait-active --wait-minutes 15
python3 scripts/aidp_workspace.py invoke-probe
```

Expected in `deployments/NAME/runtime-identity.json`:

- `credential_probe.status: PASS`: the named synthetic Credential Store item can be read in the actual deployed agent context.
- `vault_credential_probe.status: PASS`: the inference Vault reference resolves to a token-shaped string. No value is emitted.
- `private_transport.status: PASS`, `tls_verified: true`, `http_status: 401`: verified private HTTPS reaches vLLM and rejects a request with no authentication key.
- Identity metadata identifies the actual runtime. `policy_values_verified` remains false; the script does not turn token claims into IAM permissions automatically.

The probe contains only the private URL, public CA and credential names. It reports presence of a fixed set of proxy-variable names without exposing their values. If direct transport fails and runtime proxy configuration exists, it can test that configured path with certificate verification still enabled.

### 6. Publish the self-contained inference agent

Proceed only after the private transport and Vault reference checks pass.

```bash
python3 scripts/aidp_workspace.py upload-inference
python3 scripts/aidp_workspace.py redeploy-inference
python3 scripts/aidp_workspace.py wait-active --wait-minutes 15
python3 scripts/aidp_workspace.py invoke-inference
```

Expected: a normal `AgentBasic` entry backed by the actual LangGraph and private vLLM client. The generated file is saved as `deployments/NAME/runtime/inference_agent.generated.py` for review. It contains public trust material and a Credential Store name, with no model key or administrator signing key. This avoids the deployed runtime’s adjacent-file lookup limitation.

The acceptance call runs plain private chat, then the actual graph with `get_poc_inventory`, validates linked tool-call/result IDs and the exact synthetic inventory, and requires a final assistant answer. `deployments/NAME/aidp-inference-acceptance.json` must contain all four true fields:

```json
{
  "status": "PASS",
  "private_chat_verified": true,
  "tool_observed": true,
  "synthetic_inventory_verified": true,
  "final_answer_present": true
}
```

The agent also responds to normal user prompts. In AIDP, select the private workspace, open Agents, choose `NAME_with_underscores_agent`, and use Playground on the attached Active AI Compute. `__POC_VERIFY__` is the reserved synthetic acceptance prompt.

## Validation Steps

```bash
python3 scripts/aidp_workspace.py status
python3 scripts/aidp_workspace.py wait-active --wait-minutes 15
python3 scripts/aidp_workspace.py invoke-inference
```

Check the actual workspace subnet/NSG, Active compute, matching agent `computeKey`, Active deployment and the recorded acceptance result. Watch vLLM request/token and GPU activity in Grafana during the acceptance call. A Mac or monitoring-node test is useful but does not replace the AI Compute invocation.

After publishing inference, use `invoke-inference` for checks. `bootstrap`, `upload-probe` and `redeploy-probe` are preparation/debugging actions, not routine inference health checks. Replacing the entry with the probe changes what the agent executes.

## Rollback

Read `deployments/NAME/aidp-workspace.json`. Remove its owned deployed endpoints, agent and AI Compute before removing the private workspace. Include `previous_deployments` if a redeploy generated a new key. Remove only the recorded synthetic Credential Store item and Vault-reference item. Preserve unrelated workspaces and default Master Catalog compute.

These data-plane resources are separate from Terraform. After their cleanup, follow [CLEANUP.md](CLEANUP.md) for the Vault secret, scoped policy, private DNS and infrastructure. Preserve the working standalone vLLM stack when troubleshooting only AIDP.

## Sources

- [Oracle AIDP SDK and CLI](https://github.com/oracle/aidataplatform-sdk)
- [AIDP REST endpoints](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/rest-endpoints.html)
- [Agent authoring and testing](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/agent-creation.html)
- [Credential Store](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/credential-store.html)
- [Data Flow private-endpoint access](https://docs.oracle.com/en-us/iaas/Content/data-flow/using/pe-use-case.htm)


## Playground check after inference publication

The published endpoint and Playground use separate runtimes. A successful
`invoke-inference` check proves the published endpoint only. A stale Playground
test copy can keep returning the earlier identity probe after inference publication.

`upload-inference` now explicitly includes the existing compute attachment when
updating the inference entry. `connect` reports Playground TEST metadata separately
and always prints the required UI chat check. Metadata alone is never marked as
successful Playground inference acceptance.

```bash
python3 scripts/aidp_workspace.py playground-status
```

This read-only service check verifies the configured entry and compute, records
`playground_status.chat_verification: UI_CHECK_REQUIRED` in the local inventory,
and prints the test runtime state. The public endpoint URL can invoke PROD even
when supplied an existing session key, so it is deliberately not used to certify
Playground chat.

In AIDP, open the agent in Playground and ask **What is AI?** If the response is
`SIGNER_AVAILABLE` or other identity probe JSON:

1. Deselect the agent's Playground AI Compute and confirm detachment.
2. Select the same existing AI Compute and choose **Select & Deploy**.
3. Wait for Active, then create a new Playground session.
4. Ask the question again and confirm a normal language response.

Keep the published endpoint deployed. This refresh reuses the existing compute;
it does not require a stack rebuild or GPU restart. Old sessions retain their
historical probe messages, so test in a new session.
