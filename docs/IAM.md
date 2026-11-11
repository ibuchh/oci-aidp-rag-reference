# 🔐 Customer IAM and deployment ownership

## 🔹 Overview

Operator permissions and AIDP service permissions are separate. The customer should use approved identities and its normal administrator process; the package does not create an administrator user/group or bypass governance denies. Review the statements against the customer's compartment and actual service principal type. [Oracle AIDP IAM guidance](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/iam-policies-oracle-ai-data-platform.html).

## ✅ Operator prerequisites

For customer administrator instructions to create `irshad.buchh@oracle.com`, group `poc-deployment`, compartment `poc` and the scoped deployment permissions, start with [NON_ADMIN_SETUP.md](NON_ADMIN_SETUP.md). The deployment region is supplied as a variable.

The deployment operator needs the approved ability to manage this POC's networking, Compute, console-history evidence, FSS, volumes, Bastion, DNS, Vault/key/secrets, AIDP and Autonomous AI Database resources, plus metadata reads for tenancy/compartment/region discovery. Use compartment-scoped grants and approved ownership conditions where supported. This package does not prescribe tenancy-wide administration for the operator.

For example, the administrator can bind the operator's existing approved group to the AIDP/Lakehouse permissions below, if those permissions are missing and those resources are within the operator's assigned scope:

```text
Allow group id <OPERATOR_GROUP_OCID> to manage ai-data-platforms in compartment id <POC_COMPARTMENT_OCID>
Allow group id <OPERATOR_GROUP_OCID> to manage autonomous-databases in compartment id <POC_COMPARTMENT_OCID>
```

These two statements alone do not grant all infrastructure permissions listed above. Group identity/domain and existing grants must be reviewed by the customer administrator. AIDP application permissions must separately permit private workspace, AI Compute, coded-agent and Credential Store creation. Credential Store creation requires the corresponding Master Catalog permission. Optional RAG additionally requires application-level access to create/manage its dedicated schema, managed volume, native knowledge base, sources/ingestion jobs and separate coded agent, and to use the existing ingestion compute. Have the customer administrator review these scoped catalog/workspace permissions; baseline inference acceptance alone does not prove RAG access. This extension does not automatically add IAM grants.

## 🔹 Terraform service-policy roots

- `terraform/iam/main.tf` creates three policies: service identity inspection/tag/namespace access, compartment resource access with ownership-conditioned storage operations, and exact Lakehouse ADMIN-secret bundle access.
- `terraform/aidp-credential-iam/main.tf` creates one policy scoped to the actual AIDP instance, inference secret and encryption key. It permits secret-bundle read, secret metadata read/update for required governance tagging, and exact-key use. [Oracle Credential Store documentation](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/credential-store.html).

Both roots use the discovered IAM home region. The identity policy attaches at tenancy root; compartment-resource and credential policies attach at the configured POC compartment. Statements granting `any-user` are conditioned on the AIDP principal type and, where applicable, actual resource ownership/IDs. They are service grants, not unauthenticated public access.

The supplied baseline includes compartment-scoped OCI Generative AI use. Direct private vLLM inference does not invoke OCI Generative AI. Review that grant if only the private model is in scope, and review any future embedding/hosted-model requirements before removing it. Optional Object Storage lifecycle grants are not added automatically.

The reference uses `aidataplatform`. A legacy `datalake` instance needs its actual principal type; do not grant both types by default. The observed AI Compute caller in the reference was Oracle-managed. Do not grant that cluster principal broad secret access. Leave generic `runtime_principal_type` and `runtime_principal_id` null for this Credential Store workflow.

## 🔹 Separate administrator deployment

The simplest execution uses an approved customer deployment profile that may create the POC policies. If policy creation must be performed separately, use this staged sequence instead of the monolithic `--through aidp` command.

### 1️⃣ Operator deploys inference and foundation

```bash
./deploy.sh --apply --through inference

python3 scripts/poc.py plan --stage aidp-foundation
python3 scripts/poc.py apply --stage aidp-foundation
```

### 2️⃣ Administrator prepares its profile configuration

On a customer-controlled workstation/runner with the same source, protected state and an approved administrator profile, create a separate config. Never copy another person's API signing key. If identities must run on separate machines, use the customer's protected metadata/state handoff process; do not send app login files or an entire `.oci` directory.

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path
os.umask(0o077)
data = json.loads(Path('poc-config.json').read_text())
data['oci_profile'] = input('Approved local administrator profile: ').strip()
if not data['oci_profile']:
    raise SystemExit('A profile name is required.')
with Path('poc-config.admin.json').open('x') as stream:
    json.dump(data, stream, indent=2)
    stream.write('\n')
PY
```

Only the profile changes; tenancy, region, compartment, POC name and state namespace remain the same. The configured SSH key path must be valid on the runner because the shared configuration validator checks it, even for IAM phases. The profile/tenancy guard runs before apply.

### 3️⃣ Administrator applies bootstrap policies

```bash
python3 scripts/poc.py plan \
  --stage iam \
  --config poc-config.admin.json

python3 scripts/poc.py apply \
  --stage iam \
  --config poc-config.admin.json
```

Expected: three new policies, no unrelated modifications. Review them before confirmation and allow propagation. Preserve their canonical states; duplicate Console creation without import creates an ownership problem.

### 4️⃣ Operator provisions Lakehouse and AIDP

```bash
python3 scripts/poc.py plan --stage lakehouse
python3 scripts/poc.py apply --stage lakehouse

python3 scripts/poc.py plan --stage aidp
python3 scripts/poc.py apply \
  --stage aidp \
  --ack-aidp-bootstrap-limit
```

Wait for AIDP Active/AI features Ready. Continue with deployment guide steps 6–8: host health, trust/key preparation and private DNS.

### 5️⃣ Administrator applies the inference Credential Store policy

After `publish-key` records the actual inference-secret ID:

```bash
python3 scripts/poc.py plan \
  --stage aidp-credential-iam \
  --config poc-config.admin.json

python3 scripts/poc.py apply \
  --stage aidp-credential-iam \
  --config poc-config.admin.json
```

The operator can then run `aidp_workspace.py connect` using the original profile and matching state/ownership inventory.

## 🔍 Validation Steps

Record policy IDs and attachments; verify applicable denies, exact AIDP/secret/key conditions and service propagation. Require live private TLS, Credential Store access, chat and linked synthetic-tool acceptance. Terraform mocks do not prove server-side IAM authorization.

## 🔄 Cleanup ownership

Agree on the cleanup identity before provisioning. The final destroy runner must be authorized to delete all owned resources, including root/compartment POC policies. Where approved, run the documented destroy command with `--config poc-config.admin.json` using that authorized teardown profile.

If no single identity has those rights, the customer IAM administrator must coordinate staged deletion with the infrastructure owner and preserve each root's state. Do not remove service policies before AIDP finishes service-side storage cleanup. The supplied end-to-end destroy script expects its executing identity to have all required deletion permissions; it does not delegate API calls to another identity automatically.

## 📦 Files Collected

Keep reviewed policy plans, policy IDs, canonical IAM states and administrator config in protected customer storage. No policy-management rights need to be granted to the POC operator solely to avoid this administrator workflow.
