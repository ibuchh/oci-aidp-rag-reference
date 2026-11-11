# 🔐 Customer setup — non-admin POC deployment access

## 🔹 Overview

The customer administrator creates user `irshad.buchh@oracle.com`, group `poc-deployment` and compartment `poc`. The group receives resource-management access within that dedicated compartment. The administrator retains IAM policy creation and deletion, including the AIDP service-policy stages.

This guide applies to the standalone package. It does not grant tenancy administration or OKE deployment access. Resource-management grants include deletion and access to POC secrets. Keep unrelated production resources outside this compartment.

## ✅ Preconditions / Prerequisites

- Customer-approved existing OCI identity domain. Do not assume the domain is `Default`.
- Customer administrator authorized to create identity-domain users/groups, compartments and root-level IAM policies. These actions can be performed by separate authorized administrators.
- Approved deployment region with the required service availability, GPU capacity and limits. IAM home region may differ from deployment region.
- A workstation with Python 3 and OCI CLI for policy-file generation and operator validation.

## 🔹 Procedure

### 1️⃣ Create the user — customer administrator

In OCI Console, open Identity & Security → Domains and select the approved identity domain. Under its user-management area, open Users → Create.

- First name: `Irshad`
- Last name: `Buchh`
- Username: `irshad.buchh@oracle.com`
- Email: `irshad.buchh@oracle.com`
- Select email as username if that option is presented.

Create the user and complete the organization's activation/invitation process. Apply the organization's sign-in and MFA requirements. Do not assign the user to the OCI Administrators group or grant an identity-domain administrator role.

Expected: the active user exists in the selected domain. If an identically named identity already exists, confirm ownership rather than creating a duplicate.

[Oracle user creation instructions](https://docs.oracle.com/en-us/iaas/Content/Identity/users/create-user-accounts.htm).

### 2️⃣ Create the group and membership — customer administrator

In the same identity domain, open Groups → Create Group:

- Name: `poc-deployment`
- Description: `Deployment and operation of the standalone vLLM and AIDP POC`

Add `irshad.buchh@oracle.com` to the group. Record the group's OCID from its details page. Verify membership in this exact domain.

Expected: the user is a member of `poc-deployment` and is not given administrator membership as part of this setup.

[Oracle group creation](https://docs.oracle.com/en-us/iaas/Content/Identity/groups/create-groups.htm), [group membership](https://docs.oracle.com/en-us/iaas/Content/Identity/users/assign-groups-user-account.htm).

### 3️⃣ Create compartment poc — customer administrator

Open Identity & Security → Compartments. Select the tenancy root as the parent for this recommended dedicated POC layout, then select Create Compartment:

- Name: `poc`
- Description: `Standalone vLLM, monitoring, Streamlit and Oracle AI Data Platform POC`

Record its OCID and wait until it is Active. If `poc` already exists under that parent, confirm that it is dedicated to this POC before using it. Review inherited policies and tag defaults.

Compartment creation does not subscribe a region or reserve GPU capacity. [Oracle compartment creation](https://docs.oracle.com/en-us/iaas/Content/Identity/compartments/To_create_a_compartment.htm).

### 4️⃣ Set the customer variables — administrator workstation

Replace every placeholder. These are metadata, not passwords or private keys.

```bash
export OCI_REGION="<approved-deployment-region>"
export OCI_HOME_REGION="<tenancy-home-region>"
export TENANCY_OCID="<customer-tenancy-ocid>"
export POC_GROUP_OCID="<poc-deployment-group-ocid>"
export POC_COMPARTMENT_OCID="<poc-compartment-ocid>"
export OCI_PROFILE="CUSTOMER_POC"
```

`OCI_REGION` selects the deployment/API region. IAM policies below are not region-restricted and apply within their stated compartment across subscribed regions. If policy-enforced region restrictions are required, the security administrator must review region conditions separately and preserve the IAM home-region workflow.

### 5️⃣ Generate the deployment-group policy — customer administrator

Run this locally to generate a complete policy text file with actual OCIDs. It creates no OCI resources. Group OCIDs avoid ambiguity with identity-domain names.

```bash
umask 077
python3 - <<'PY'
import os
from pathlib import Path

group = os.environ['POC_GROUP_OCID']
compartment = os.environ['POC_COMPARTMENT_OCID']
if not group.startswith('ocid1.group.'):
    raise SystemExit('Set POC_GROUP_OCID to the real group OCID.')
if not compartment.startswith('ocid1.compartment.'):
    raise SystemExit('Set POC_COMPARTMENT_OCID to the real compartment OCID.')

prefix = f'Allow group id {group} to '
tenancy = [
    'inspect tenancies',
    'inspect compartments',
    'read objectstorage-namespaces',
    'inspect work-requests',
]
scoped = [
    'manage virtual-network-family',
    'manage instance-family',
    'manage volume-family',
    'read instance-agent-plugins',
    'manage file-family',
    'manage bastion-family',
    'manage dns-zones',
    'manage dns-records',
    'read dns-views',
    'read dns-resolvers',
    'manage vaults',
    'manage keys',
    'manage secret-family',
    'manage autonomous-databases',
    'manage ai-data-platforms',
    'inspect buckets',
]
lines = [prefix + rule + ' in tenancy' for rule in tenancy]
lines += [prefix + rule + ' in compartment id ' + compartment
          for rule in scoped]
path = Path('poc-deployment-policy.txt')
with path.open('x') as stream:
    stream.write('\n'.join(lines) + '\n')
print(f'Created {path}: {len(lines)} policy statements; no OCI requests.')
PY
```

Expected: a new `poc-deployment-policy.txt` containing 20 statements. The script refuses to overwrite an existing file. Review it before applying. The tenancy-level statements permit metadata discovery; the resource-management statements are scoped to `poc` and its descendants.

In OCI Console → Identity & Security → Policies, select the tenancy root and create policy `poc-deployment-access`. Use the manual editor and paste the contents of the generated text file. The policy is created by the customer administrator, not the new deployment user.

If the approved GPU image is in another compartment, add this statement after replacing its placeholders:

```text
Allow group id <POC_GROUP_OCID> to read instance-images in compartment id <IMAGE_COMPARTMENT_OCID>
```

These are reviewed service-family permissions for this package, not a mathematically minimal permission set. Customer governance can require additional review, including any enforced defined-tag namespace permissions. The group is not granted policy management, user management or group management.

References: [IAM discovery](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/iampolicyreference.htm), [Compute](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/corepolicyreference.htm), [Bastion](https://docs.oracle.com/en-us/iaas/Content/Bastion/Tasks/managingbastions.htm), [DNS](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/dnspolicyreference.htm), [Vault](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/keypolicyreference.htm), [AIDP IAM](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/iam-policies-oracle-ai-data-platform.html).

### 6️⃣ Configure the deployment user's API profile — deployment user

Activate the account and complete the approved sign-in process. Generate the API signing key locally and have its public key registered for this exact user through the customer's approved process. The administrator can register the public key if self-service key management is restricted. Never send the private key to the customer administrator or place it in this package.

Configure `~/.oci/config` using the registered user, tenancy and fingerprint, with the private key kept locally:

```bash
oci setup config
```

Use the profile name stored in `OCI_PROFILE`, for example `CUSTOMER_POC`. The account's browser password and the API signing key are separate credentials. The current package expects an API-key profile.

### 7️⃣ Verify access and generate POC configuration — deployment user

Set the same metadata variables on the operator workstation; shell variables on the administrator workstation are not transferred automatically. From the extracted `customer-vllm-aidp-poc` folder:

```bash
python3 scripts/verify-release.py

oci iam tenancy get \
  --profile "$OCI_PROFILE" \
  --region "$OCI_HOME_REGION" \
  --tenancy-id "$TENANCY_OCID" \
  --query 'data.name' \
  --raw-output

oci iam compartment get \
  --profile "$OCI_PROFILE" \
  --region "$OCI_HOME_REGION" \
  --compartment-id "$POC_COMPARTMENT_OCID" \
  --query 'data.name' \
  --raw-output

./setup.sh \
  --profile "$OCI_PROFILE" \
  --region "$OCI_REGION" \
  --compartment-id "$POC_COMPARTMENT_OCID"

python3 scripts/poc.py preflight
```

Expected: source integrity passes, the intended tenancy name and compartment `poc` are returned, and setup creates a protected `poc-config.json` for the selected region. Enter the approved regional GPU image, AD, SSH key, actual SSH egress address and non-overlapping VCN CIDR when prompted. Setup and preflight do not provision the stack; preflight does not prove authorization for every future create/delete operation.

### 8️⃣ Deploy with separate IAM administrator stages

Follow the staged route in [IAM.md](IAM.md), then the remaining steps in [DEPLOYMENT.md](DEPLOYMENT.md):

1. Operator deploys inference and AIDP foundation.
2. Customer administrator reviews and applies the `iam` Terraform stage, including its root-level AIDP service identity policy.
3. Operator deploys Lakehouse and AIDP, then prepares the model trust bundle, inference secret and private DNS.
4. Customer administrator applies `aidp-credential-iam`, scoped to the actual AIDP instance, inference secret and key.
5. Operator runs the AIDP workspace/AI Compute/coded-agent integration and acceptance.

Do not use the monolithic deployment path with the non-admin profile: it attempts the IAM stages using that same profile. Administrator and operator must retain matching protected Terraform states and resource inventories without exchanging private signing keys.

OCI policies and AIDP application permissions are separate. Verify workspace/AI Compute/agent access and Master Catalog `CREATE_CREDENTIAL` permission. If the customer creates the instance first, it must grant the operator the required application permissions. AIDP application roles are distinct from OCI tenancy administration. [AIDP permissions](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/permissions-model.html), [Credential Store](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/credential-store.html).

## 🔍 Validation Steps

- Confirm the exact identity-domain user and group membership in Console.
- Confirm the policy's group OCID, POC compartment OCID and attachment at root.
- Confirm reviewed plans target the approved tenancy, compartment and region.
- Complete private chat and observed synthetic-tool acceptance from AI Compute, as described in the deployment guide.
- Record any customer-required policy adjustments. This policy baseline has not been applied or live-tested in the customer's tenancy as part of documentation preparation.

## 🔄 Rollback and cleanup ownership

Agree on teardown ownership before provisioning. The documented full destroy runner requires rights to remove both infrastructure and POC policies; a customer-authorized cleanup identity can run it, or the administrator and operator must coordinate staged deletion. Preserve service permissions until AIDP completes dependent cleanup. Follow [CLEANUP.md](CLEANUP.md).

After cleanup is verified, the administrator can remove this deployment-access policy, remove the user's POC membership and disable the temporary user according to customer policy. The stack's destroy command intentionally retains the pre-created compartment, user and group. Do not delete those identities or revoke access while cleanup still needs them.
