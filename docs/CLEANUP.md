# Customer teardown — complete POC

## Overview

Cleanup permanently deletes the POC's VMs/disks, FSS/model files, monitoring data, Lakehouse data, native knowledge-base vectors, uploaded RAG documents, AIDP, policies and networking. The controller operates on recorded ownership, not a compartment-wide name scan. Imported images, the pre-existing compartment/operator identities and unrelated resources remain.

There are three phases: explicit RAG and baseline AIDP data-plane cleanup, inference-secret deletion scheduling, then guarded Terraform destruction. Vault/key/secret retention and service-managed storage need a final check after apply. No command in this document has been executed in the customer's tenancy as part of handover preparation.

## Preconditions / Prerequisites

- Stop traffic and load tests. Export anything that must survive, including remote FSS, metrics and database data.
- Preserve the same source, populated config, states, `.pocctl` inventory, `aidp-workspace.json` and `rag/inventory.json` used for deployment. Include RAG earlier deployment versions, native source/ingestion job IDs and any manually created resources.
- Decide whether to remove only RAG or the entire POC. RAG-only removal stops after step 2a and leaves baseline infrastructure and credentials in place.
- Use a customer identity authorized to delete the complete POC, including IAM policies. See [IAM.md](IAM.md) for split roles.
- Keep the same account, region, POC name and state directory. Do not remove state files or ownership records to bypass a failed deletion.

## Procedure

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

./destroy.sh --config "$CONFIG"
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
2. Delete `vllm_aidp_poc_ai_compute` only after both baseline and RAG agents are detached, and wait for completion.
3. In Credential Store, remove the recorded `vllm_aidp_poc_vllm_key` Vault reference and `vllm_aidp_poc_credential_probe` synthetic item. Removing a reference does not delete its underlying Vault secret.
4. Remove the owned uploaded code folders recorded by the baseline inventory. Delete the owned private workspace after its dependencies are gone.
5. Inspect other POC-owned ingestion jobs, workspaces, Master Catalog/Spark compute, managed data, buckets, objects, attachments and retention settings. Remove these through supported service cleanup only after their consumers are gone. Do not assume deleting the AIDP instance purges retained storage or unrelated assets.

Leave the Terraform-managed AIDP instance and Lakehouse for step 4. The `--confirm-aidp-runtime-removed` flag is the operator's acknowledgment, not an automatic RAG/KB dependency check. Set it only after the full recorded data-plane inventory has been reconciled. Neither `aidp_workspace.py` nor `rag.py` provides an automatic destroy command in this release.

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

### 4. Apply infrastructure destruction

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
