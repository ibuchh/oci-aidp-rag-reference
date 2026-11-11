# Customer teardown — complete POC

## Overview

Cleanup permanently deletes the POC's VMs/disks, FSS/model files, monitoring data, Lakehouse data, native knowledge-base vectors, uploaded RAG documents, AIDP, policies and networking. The controller operates on recorded ownership, not a compartment-wide name scan. Imported images, the pre-existing compartment/operator identities and unrelated resources remain.

For full-stack teardown, follow **Automated full cleanup**: preview, review any additional workspace selection, apply, then verify completion. The command creates and verifies a local controller backup, removes the owned AIDP/RAG runtime, schedules the recorded inference secret for deletion, and destroys infrastructure. You do not need to perform the manual backup, runtime-removal or secret-scheduling steps separately. Vault/key/secret retention and service-managed storage still need a final check after apply.

## Preconditions / Prerequisites

- Stop traffic and load tests. Export anything that must survive, including remote FSS, metrics and database data.
- Preserve the same source, populated config, states, `.pocctl` inventory, `aidp-workspace.json` and `rag/inventory.json` used for deployment. Include RAG earlier deployment versions, native source/ingestion job IDs and any manually created resources.
- Decide whether to remove only RAG or the entire POC. RAG-only removal stops after step 2a and leaves baseline infrastructure and credentials in place.
- Use a customer identity authorized to delete the complete POC, including IAM policies. See [IAM.md](IAM.md) for split roles.
- Keep the same account, region, POC name and state directory. Do not remove state files or ownership records to bypass a failed deletion.

## Automated full cleanup

For a whole-stack teardown, use `--include-runtime` to automate the local backup, AIDP/RAG runtime removal and inference-secret scheduling before Terraform destruction. Choose this workflow or the manual alternative below. RAG-only removal still uses manual step 2a.

Use the same complete deployment folder, populated configuration and ownership inventories. The cleanup identity needs AIDP workspace/agent/compute/credential deletion permissions, ADMIN access to the dedicated RAG schema/volume, inference-secret deletion scheduling and the documented Terraform/IAM deletion rights. Missing ownership, unresolved create intents, API authorization failures and unexpected resources stop automated apply.

### 1. Preview the complete scope

Run from the extracted deployment folder that contains `destroy.sh` and your populated configuration. The commands below work in Bash or Zsh; keep the same shell session through preview and apply.

```bash
CONFIG=poc-config.json
CLEANUP_SCOPE=(--include-runtime)
POC_NAME=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["name"])' \
  "$CONFIG")
DEPLOYMENT_DIR="deployments/$POC_NAME"

./destroy.sh \
  --config "$CONFIG" \
  --plan \
  "${CLEANUP_SCOPE[@]}"
```

Expected: an AIDP/RAG cleanup inventory, any blockers, a proposed secret-deletion schedule and the Terraform destroy plans. Preview sends no mutating AIDP requests, schedules no secret deletion and applies no Terraform changes. Reports are private files under `deployments/NAME/cleanup/`.

The scope includes all endpoint versions discovered under the exact recorded agents, Playground compute detachment, those agents, recorded document files, a cascading deletion of the owned dedicated RAG schema, recorded Credential Store entries, the shared owned AI Compute and private workspace. The schema cascade includes every child object in that dedicated namespace, including the native KB, indexed vectors and managed document volume; it never targets the entire default catalog. An unrecorded file/folder, agent, compute, job or credential blocks automatic deletion. The instance's named default workspace stays until Terraform removes the AIDP instance. An explicitly selected workspace associated with the Vector Database may also have to remain until AIDP deletion; see the parent-deletion exception below.

To find an additional workspace key, first run the preview command above **without** `--additional-workspace`. Read the `BLOCKED: Untracked workspace` lines, for example:

```text
BLOCKED: Untracked workspace example-workspace [WORKSPACE_KEY]
```

The value inside the square brackets is the workspace key returned by AIDP's read-only workspace listing. Copy that value into `EXTRA_WORKSPACE_KEY` below. The key identifies a workspace inside the selected AIDP instance; it is not a compartment OCID. Use the key from your own deployment's output. Do not copy a key from another tenancy or example. If no untracked workspace is reported, omit `--additional-workspace`.

An additional workspace is never adopted by display name. Review the discovered workspace and all its contents, then explicitly select its key in both preview and apply:

```bash
EXTRA_WORKSPACE_KEY=REPLACE_WITH_KEY_FROM_PREVIEW
CLEANUP_SCOPE+=(--additional-workspace "$EXTRA_WORKSPACE_KEY")

./destroy.sh \
  --config "$CONFIG" \
  --plan \
  "${CLEANUP_SCOPE[@]}"
```

For each additional approved workspace, append another `--additional-workspace KEY` pair to `CLEANUP_SCOPE`, then rerun preview. The same selection is reused by apply below. This selects that entire workspace, including all contained files, jobs and compute. A missing or unknown key blocks apply. Do not select shared or unrelated workspaces.

### 2. Apply the reviewed cleanup

After the complete preview has no blockers:

```bash
./destroy.sh \
  --config "$CONFIG" \
  --apply \
  "${CLEANUP_SCOPE[@]}" \
  --delete-data \
  --confirm-name "$POC_NAME"
```

Keep `CLEANUP_SCOPE` unchanged from the reviewed preview so apply includes the same approved workspaces. Do not add `--confirm-aidp-runtime-removed` to automated cleanup; the script verifies runtime removal itself. It asks for `DELETE NAME INCLUDING RUNTIME` and the existing per-component confirmations. For a previously reviewed noninteractive run, append `--yes`; the matching `--confirm-name` and `--delete-data` remain mandatory.

The apply sequence is:

1. Recheck the target and ownership, and generate all infrastructure destroy previews before any deletion.
2. Create and verify a private local archive of the supplied configuration, source files, Terraform states and deployment inventories under `.local/cleanup-backups/TIMESTAMP/`. The archive and checksum manifest contain sensitive information and must stay private. Provider caches and symlinks are excluded.
3. Remove endpoint versions, detach Playground compute and remove owned agents. Delete recorded RAG originals, then cascade-delete only the owned dedicated RAG schema and verify its disappearance. Remove the recorded credentials, compute and private workspace. Remove only explicitly selected additional workspaces after RAG cleanup, keeping their ingestion compute available until then.
4. Verify runtime pre-cleanup, recording any service-confirmed Vector Database workspace deferral, then schedule an Active inference secret for deletion with an eight-day delay. An already pending deletion keeps its original deadline. Metadata identity, compartment and Vault must match; no secret contents are retrieved.
5. Apply newly reviewed Terraform destroy plans in dependency order. Immediately after AIDP destruction, verify that its Terraform state is empty and the exact instance is deleted or absent from a successful OCI control-plane listing. This completes any deferred workspace removal before Lakehouse and supporting policies are removed. Continue with the remaining infrastructure and recorded retained boot volumes.

**The automatic archive contains local controller files, not remote FSS/model data, database contents, Grafana history or uploaded document originals.** Export remote data separately before applying if it must survive. `--delete-data` explicitly approves losing that remote data. Keep the imported GPU image for rebuilding.

Playground cleanup deselects each owned agent's Playground AI Compute and waits for its attachment and TEST runtime to clear before deleting that agent. If detachment cannot be verified, cleanup stops before secret scheduling or infrastructure destruction. After a failure or timeout, resolve the reported issue and rerun the same apply command with the same configuration and workspace selections. Keep the existing journal; completed deletions are reconciled and already removed endpoints are not recreated.

**Vector Database workspace exception:** AIDP can reject a standalone workspace deletion with `403 NotAllowed` and a detailed message explaining that a workspace associated with the Vector Database can only be deleted as part of AI Data Platform deletion. For an explicitly selected additional workspace, cleanup recognizes only that exact service restriction and records `DEFERRED_TO_PARENT`; it does not report the workspace as deleted or change its permissions. Other 403 responses still stop cleanup. The existing `--apply --include-runtime --delete-data --confirm-name` guards remain required.

When ordinary runtime cleanup is verified, `runtime_precleanup_verified` becomes `true`. If a workspace is deferred, `runtime_verified` remains `false` until parent AIDP deletion is confirmed. The controller then records `REMOVED_WITH_PARENT` and `parent_deletion_verified`. If parent verification fails, it stops before Lakehouse and supporting policies are removed. Keep the same workspace selection and journal when resuming; do not attempt to bypass the service restriction by changing workspace permissions or injecting internal deletion headers.

Progress is recorded under `deployments/NAME/cleanup/journal.json`. Accepted asynchronous operations are polled and recorded. If a request, permission check, dependency removal or wait fails, later phases stop. Rerun the same command with the same configuration and inventories to resume. `--cleanup-wait-minutes 60` extends the per-operation wait from its default 30 minutes. Never erase state or the journal to bypass a failure.

A failed runtime cleanup prevents inference-secret scheduling and infrastructure apply. A failed backup prevents all automated deletions. Authentication/authorization failures are not treated as missing resources; an HTTP 404 requires a successful parent listing to establish absence. Service dependency conflicts or unsupported API behavior stop cleanup instead of bypassing runtime verification.

If an AIDP request is rejected, the error identifies the HTTP method and resource path. Read `deployments/NAME/cleanup/last-api-error.json` locally for the service's detailed message, status and request ID. This private file excludes request bodies and authorization headers; keep it out of GitHub and customer ZIPs. `403 NotAllowed` alone does not establish the cause: check the service message and the exact workspace's application permissions before changing IAM. Resolve the reported restriction, then rerun the same apply command; do not mark a denied deletion as complete or erase its journal entry.

### 3. Verify completion and secret retention

The automated apply handles both kinds of recorded credentials:

- AIDP Credential Store entries, including the Vault reference and synthetic probe, are deleted during runtime cleanup.
- The separately recorded inference API-key secret in OCI Vault is scheduled for deletion eight days after successful runtime cleanup. An existing pending deadline is preserved. Unrelated secrets are outside this cleanup scope.
- A preview schedules nothing. If runtime cleanup stops on an unresolved error, the command does not proceed to inference-secret scheduling or infrastructure destruction. A recorded Vector Database workspace deferral is an expected dependency on parent AIDP deletion, so verified pre-cleanup can proceed to secret scheduling and the reviewed infrastructure plans. A secret already pending deletion from an earlier run retains that state.

`PENDING_DELETION` is the expected immediate Vault result, rather than permanent deletion. The secret becomes unavailable while deletion is pending; permanent removal follows its scheduled deadline. See [Oracle secret deletion](https://docs.oracle.com/en-us/iaas/Content/secret-management/Tasks/delete-secret.htm). Vault and encryption-key deletion can have their own pending deadlines.

Read the local completion record without displaying secret contents:

```bash
python3 - "$DEPLOYMENT_DIR/cleanup/journal.json" <<'PYCODE'
import json, sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())
print(json.dumps({name: record.get(name) for name in (
    "runtime_precleanup_verified", "runtime_verified",
    "parent_deletion_verified", "infrastructure_completed", "secret"
)}, indent=2))
PYCODE

python3 scripts/poc.py status --config "$CONFIG"
```

Expected: `runtime_verified` and `infrastructure_completed` are both `true`, with no remaining Terraform-managed resources reported by status. If an inference secret was recorded, its journal entry shows `PENDING_DELETION` and `time_of_deletion`, or `DELETED` if already removed. The journal records the last observation; verify the secret's current state and deadline in OCI Vault, then complete **Validation Steps** below. Keep the local backup and inventories until all pending deletions and service-side checks are complete.

Oracle API references: [schema cascade deletion](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/op-aidataplatforms-aidataplatformid-schemas-schemakey-delete.html), [AIDP REST endpoints](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/rest-endpoints.html).

## Manual cleanup alternative

Use this path for RAG-only removal or when automatic runtime cleanup cannot be used. For a successful full-stack automated cleanup, skip these manual steps; do not schedule the same secret again.

### 1. Inventory and preserve evidence

From the extracted source package:

```bash
export POC_ROOT="$PWD"
CONFIG=poc-config.json

POC_NAME=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["name"])' \
  "$CONFIG")

DEPLOYMENT_DIR="deployments/$POC_NAME"
python3 scripts/poc.py status --config "$CONFIG"

umask 077
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=".local/cleanup-$STAMP"
mkdir -p "$BACKUP_DIR"
cp "$CONFIG" "$BACKUP_DIR/"
tar -czf "$BACKUP_DIR/deployment-records.tgz" "$DEPLOYMENT_DIR"

./destroy.sh --config "$CONFIG" --plan
```

Expected: local destroy plans and recorded boot-volume IDs are displayed, with no cloud deletion. These Terraform plans do not include the native KB, RAG files, data-plane agents or managed workspaces; reconcile their inventories separately before apply. Review them. This archive contains deployment evidence and possibly passwords/sensitive state; keep it private. It does not back up remote model, metrics or database data.

### 2. Remove RAG and baseline AIDP runtime resources explicitly

Open the exact AIDP instance recorded in the deployment. Read both `deployments/NAME/aidp-workspace.json` and, if RAG was prepared, `deployments/NAME/rag/inventory.json`. Use resource IDs, `resources`, `previous_deployments`, accepted create intents and entry-file paths to establish ownership; names alone are insufficient. If an inventory is missing or a create was interrupted, reconcile the exact resources with the administrator before deletion. A missing local inventory does not establish that RAG is absent.

For default name `vllm-aidp-poc`, the names below use `vllm_aidp_poc`. Substitute your configured name with hyphens converted to underscores for schema and agent names.

#### 2a. Remove only the RAG extension first

Perform these steps whenever RAG was created, including before a whole-stack teardown:

1. Stop RAG users, requests and any active load tests. Undeploy every owned published endpoint for `vllm_aidp_poc_rag_agent`, including earlier versions recorded in `previous_deployments`. Detach its Playground test runtime and compute attachment, then delete the separate RAG agent. Keep the shared AI Compute running while other agents depend on it.
2. Inspect the KB's **Data Source → source → Job runs**. Let active ingestion finish or cancel it through the supported service workflow, and verify it has stopped before removing the source. Keep ingestion Spark compute available until native KB cleanup completes.
3. In **Master catalog → default → vllm_aidp_poc_rag → Knowledge Bases → aster_knowledge_base**, delete the owned ingestion source. This removes that source's indexed vector embeddings. It does not stand in for deleting the original document files. Then delete the now-unused native knowledge base.
4. In the same dedicated schema's **Volumes → synthetic_documents**, review the recorded `uploaded_documents` filenames/hashes and any files added manually. Export required originals, then delete only the owned files and volume using the supported UI. Check for retained managed-storage objects/backups. Delete `vllm_aidp_poc_rag` only when it contains no other resources.
5. Remove only the RAG code folder identified by `rag/inventory.json`'s `entry_file`, normally `/Workspace/Shared/poc_rag_<owned_agent_key>/`. Do not delete the entire Shared folder. Keep the local inventory and acceptance records through final reconciliation.

Expected: the recorded RAG agent, all its endpoint versions/test runtime, KB, source vectors, document volume/files and empty dedicated schema are gone. Check the recorded IDs in AIDP; Terraform state cannot certify their removal. Deleting the native source also deletes its vectors, as described in [Oracle knowledge-base cleanup](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/knowledge-bases.html).

**For RAG-only removal, stop here.** Preserve the baseline agent, shared AI Compute/workspace, Credential Store, inference secret, private DNS, policies, GPU/FSS, monitoring, AIDP, ingestion compute and Lakehouse. Recheck the baseline using:

```bash
python3 scripts/aidp_workspace.py invoke-inference \
  --config "$CONFIG"
```

Expected: baseline chat/tool acceptance still passes. Do not schedule secret deletion or run destructive Terraform commands for RAG-only removal.

#### 2b. Continue with baseline resources for a whole-stack teardown

1. In `vllm-aidp-poc-private`, undeploy the endpoint for `vllm_aidp_poc_agent` and every remaining owned earlier version. Detach its Playground test runtime and agent compute attachment, then delete the agent.
2. Delete `vllm_aidp_poc_ai_compute` only after both baseline and RAG agents are detached, and wait for completion. A workspace associated with the Vector Database must remain for parent AIDP deletion; record that service restriction rather than trying to delete it independently.
3. In Credential Store, remove the recorded `vllm_aidp_poc_vllm_key` Vault reference and `vllm_aidp_poc_credential_probe` synthetic item. Removing a reference does not delete its underlying Vault secret.
4. Remove the owned uploaded code folders recorded by the baseline inventory. Delete the owned private workspace after its dependencies are gone.
5. Inspect other POC-owned ingestion jobs, workspaces, Master Catalog/Spark compute, managed data, buckets, objects, attachments and retention settings. Remove these through supported service cleanup only after their consumers are gone. Do not assume deleting the AIDP instance purges retained storage or unrelated assets.

Leave the Terraform-managed AIDP instance, its default workspace, any service-protected Vector Database workspace and Lakehouse for step 4. The `--confirm-aidp-runtime-removed` flag is the operator's acknowledgment, not an automatic RAG/KB dependency check. Set it only after the full recorded data-plane inventory has been reconciled. For automated whole-stack teardown, use `destroy.sh --include-runtime` above instead of these manual steps. `aidp_workspace.py` and `rag.py` do not expose standalone destroy actions.

Oracle procedures: [AI Compute](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/ai-compute.html), [Workspaces](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/workspaces.html), [Credential Store](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/credential-store.html).

### 3. Schedule the separately recorded inference secret for deletion

Skip only if `publish-key` was never run and there is no inference-secret record or interrupted create intent. Otherwise extract non-secret metadata:

```bash
PROFILE=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["oci_profile"])' \
  "$CONFIG")

REGION=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["region"])' \
  "$CONFIG")

SECRET_RECORD="$DEPLOYMENT_DIR/runtime/inference-secret.json"
SECRET_ID=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["id"])' \
  "$SECRET_RECORD")

oci vault secret get \
  --profile "$PROFILE" \
  --region "$REGION" \
  --secret-id "$SECRET_ID" \
  --query 'data.{name:"secret-name",state:"lifecycle-state",vault:"vault-id"}'
```

Verify the secret and Vault match this POC. If already pending deletion, preserve its deadline. For an Active secret, schedule deletion:

```bash
DELETE_AT=$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
deadline = datetime.now(timezone.utc) + timedelta(days=8)
print(deadline.strftime('%Y-%m-%dT%H:%M:%SZ'))
PY
)

oci vault secret schedule-secret-deletion \
  --profile "$PROFILE" \
  --region "$REGION" \
  --secret-id "$SECRET_ID" \
  --time-of-deletion "$DELETE_AT"

oci vault secret get \
  --profile "$PROFILE" \
  --region "$REGION" \
  --secret-id "$SECRET_ID" \
  --query 'data."lifecycle-state"' \
  --raw-output
```

Expected: `PENDING_DELETION`. The secret becomes unavailable immediately; permanent deletion follows retention. [Oracle secret deletion](https://docs.oracle.com/en-us/iaas/Content/secret-management/Tasks/delete-secret.htm).

If a create intent exists without a secret record, reconcile the exact name/Vault/compartment through metadata with the customer administrator. Do not retrieve secret contents or adopt an unrelated secret as a shortcut. Resolve permission/not-found ambiguity before continuing.

### 4. Plan and apply infrastructure destruction

First generate and review the destroy plans:

```bash
./destroy.sh \
  --config "$CONFIG" \
  --plan
```

Expected: Terraform destroy plans are written locally for review; no cloud resources are deleted. `--plan` explicitly selects the existing default preview behavior. It cannot be combined with `--apply`. The earlier AIDP runtime cleanup and secret-deletion prerequisites still apply before destructive execution.

After reviewing the plans, run the separate apply command:

```bash
./destroy.sh \
  --config "$CONFIG" \
  --apply \
  --delete-data \
  --confirm-name "$POC_NAME" \
  --confirm-aidp-runtime-removed
```

Use `CONFIG=poc-config.admin.json` before step 1 if the agreed teardown identity is the administrator profile and it has full POC deletion rights. Do not change POC target/name/state when selecting that profile.

Each component creates a fresh plan for review and asks for confirmation. Source lifecycle protection is disabled only in the controller's isolated reviewed destroy copies; do not edit protection manually. The controller removes optional runtime IAM, Credential Store IAM, private DNS, AIDP, Lakehouse, service policies, foundation, GPU/monitoring resources and shared network/FSS in dependency order, then processes recorded retained boot volumes. Imported images and the existing compartment are retained.

Vault/key/secret deletion can remain pending after Terraform finishes. Record the actual deadline instead of declaring them physically deleted. [Oracle Vault deletion](https://docs.oracle.com/en-us/iaas/Content/KeyManagement/Tasks/managingvaults_topic-To_delete_a_vault.htm).

## Validation Steps

```bash
python3 scripts/poc.py status --config "$CONFIG"
```

Expected: zero managed resource records in the package's deployed roots, unless a deletion failed. Also verify actual recorded IDs in OCI:

- GPU/monitoring VMs terminated and their recorded boot/metrics volumes removed.
- FSS exports, mount target and file system removed.
- RAG KB, ingestion source/vectors, original uploaded documents, dedicated managed volume/schema and owned code folders removed or explicitly recorded for retention.
- Both agents, all owned endpoint versions and Playground test runtimes, AI Compute, workspaces, AIDP and Lakehouse removed.
- POC IAM policies, private DNS, Bastion and networking removed.
- Vault/key/secrets deleted or recorded as pending with final verification dates.
- No unintentionally retained POC-owned buckets, objects, database/volume backups, manual endpoints or service artifacts remain.

Public apps and inference must stop serving. An empty state alone does not prove untracked/manual resources or retained backups were deleted. Retention can require a later customer follow-up.

## Rollback

Cancel before apply to leave resources intact. After deletion starts, restore from actual retained backups/images where supported. Recreating Terraform resources does not restore deleted model, metrics, database or original document data. Rebuild a removed RAG KB only from backed-up originals, re-ingest them and rerun acceptance; embeddings, hashes and acceptance reports are not source-document backups. RAG-only rollback must leave baseline dependencies intact. Pending secret deletion can be canceled through OCI before its deadline if the customer deliberately chooses recovery.
