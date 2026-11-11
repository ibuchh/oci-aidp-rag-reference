# Customer configuration

## Account inputs

Run `./setup.sh` to produce `poc-config.json`. No fields from the demonstration account are prepopulated. The wizard reads profile names, tenancy ID and default region from your local OCI configuration; it never copies API signing credentials into JSON.

- `oci_profile`: your API-key profile name.
- `tenancy_id`: discovered from that profile and checked against the live tenancy.
- `region`: a selected Ready region subscription.
- `home_region`: the tenancy IAM home region, discovered independently of the workload region.
- `compartment_id`: selected existing accessible Active compartment, with ancestry validated.
- `availability_domain`: selected exact region/tenancy-specific AD name. A10.2 capacity is a separate prerequisite.
- `gpu_image_id`: your approved regional GPU image; the wizard validates status, OS metadata, size and shape compatibility, not operational driver health.
- `monitoring_image_id`: automatically discovered regional Ubuntu 24.04 platform image for the CPU monitoring shape.
- `ssh_private_key_path`: local SSH private key with adjacent `.pub` file. The public key is uploaded to the VMs; the private key stays on your workstation.
- `admin_cidr`: actual public SSH egress IPv4 `/32` for Bastion, or the supported `0.0.0.0/0` value when access from any IPv4 source is intended. SSH authentication and temporary sessions still apply. Public web access is separate.
- `vcn_cidr`: reviewed RFC1918 `/16`; `10.160.0.0/16` is only a proposed example. Validate connected networks and networks outside the visible compartment.
- `name`: resource/state namespace. Default `vllm-aidp-poc` gives agent `vllm_aidp_poc_agent`.
- `state_directory`: `deployments/NAME/states`, isolated from other POCs. Do not retarget existing state.

## Fixed reference baseline

The supplied stack serves one pinned Qwen3-8B revision using vLLM 0.11.0, FP16, TP=2, context 4096, up to four scheduled sequences and Hermes tool calling. The GPU shape is `VM.GPU.A10.2`. Changing model family/repo is not just a JSON field change; update serving templates and rerun tool/quality/performance acceptance.

The monitoring VM defaults to `VM.Standard.E5.Flex`, 2 OCPUs, 16 GB and a 100 GB metrics volume. Prometheus has a 15-day/60-GB retention policy. Review regional shape availability before applying.

Lakehouse defaults are database name `AIDPPOC`, 2 ECPUs, 1 TB and no compute/storage autoscaling. Review database-name uniqueness in the target tenancy before deployment. The Lakehouse is paid infrastructure.

Leave `runtime_principal_type` and `runtime_principal_id` null. Use the scoped AIDP Credential Store policy, not a guessed Oracle-managed compute principal.

`bootstrap_timeout_seconds` is 3600. It bounds controller waiting, not resource lifetime or a service provisioning guarantee.

## Network and access

Subnet offsets within the selected `/16` are 32 for Bastion, 33 for GPU, 34 for FSS, 35 for monitoring and 36 for AIDP/Lakehouse. The AIDP subnet and customer VCN resolver enable private model access. Private inference uses `https://inference.lab/v1/chat/completions` with bearer authentication and an explicitly trusted certificate.

The initial GPU certificate lasts 30 days. Coordinate certificate renewal, CA redistribution and agent republishing before expiry. The initial deployment does not implement automated private CA rotation.

With `grafana_hostname: null`, Grafana and Streamlit use generated sslip.io names and ACME certificates. Monitoring public TCP 80 is used for certificate issuance; user traffic uses HTTPS on 443. Application listeners are on loopback. Streamlit has a generated POC username/password separate from Grafana. Corporate SSO is not implemented.

If using a custom Grafana hostname, configure it before apply and point both that hostname and its `streamlit.` hostname to the new monitoring public IP. A private-only browser access variant requires a reviewed source/network change; the supplied setup does not silently select one.

## State and administrator roles

Each Terraform root has independent local state under the configured deployment directory. The controller creates protected local backend overrides and reviewed run directories. Do not run separate applies in source roots; that would create another state lineage. The customer is responsible for protected state backups and coordinating concurrent operators. Remote-state migration is not automated by this package.

The AIDP data-plane inventory is outside Terraform in `deployments/NAME/aidp-workspace.json`. Preserve it for retries and cleanup. Generated logins and TLS/private key material are customer secrets; never commit the deployment directory to source control.

Read [IAM.md](IAM.md) for service policies, scoped Credential Store access, administrator ownership and teardown roles.

## Optional native AIDP RAG

The RAG scripts reuse the configured name/profile/region and existing workspace inventory. Schema and agent names use the POC name with hyphens replaced by underscores. KB, managed-volume, embedding and chunk settings are documented in [RAG_KNOWLEDGE_BASE.md](RAG_KNOWLEDGE_BASE.md); they are not additional `poc-config.json` fields. Do not invent new configuration keys. Native KB creation and ingestion are UI operations, and original documents require separate backup and retention decisions.
