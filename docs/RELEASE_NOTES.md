# Customer handover fixes — November 10, 2026

- Image and Terraform state validation recognize legacy OCI region keys, including `iad` for Ashburn.
- SSH egress validation accepts explicit `0.0.0.0/0` for an unrestricted Bastion client allowlist.
- SSH host-key discovery waits for guest SSH readiness before console fingerprint verification.
- Previously verified instance host-key pins remain authoritative after console log rotation.
- Streamlit and AIDP dependency manifests are included in the source package.
- Monitoring installation includes Docker Buildx and repairs a missing plugin before the Streamlit build.
- Application packaging fails early if required source files are missing.
- Failed Streamlit builds save private local diagnostics without printing captured credentials.
- Terraform lifecycle progress and periodic elapsed-time messages appear during execution.
- AIDP applies periodically display the owned instance's work-request status and percentage when available.
- Full Terraform output remains in private logs; JSON plans and credential-related output remain private.

This source release contains no customer configuration, credentials, deployment state, or model data.
The changes apply to newly started controller processes. An existing controller process retains its loaded code.

## Playground recovery and status — November 11, 2026

Inference publication now explicitly includes the existing compute attachment.
The connect workflow reports Playground TEST metadata separately from published
endpoint acceptance and prints the required UI chat check. The new
`playground-status` command checks the owned source/compute binding without
changing cloud resources. A normal published endpoint response is never treated
as proof of Playground chat. The workspace guide includes the verified procedure
for refreshing a stale probe test copy on the same existing compute.

## Automated full cleanup — November 11, 2026

`destroy.sh --include-runtime` adds GET-only runtime discovery, automatic verified local controller backups, inventory-scoped AIDP/RAG removal, inference-secret scheduling and resumable progress before Terraform destruction. Dedicated RAG schemas use documented cascade deletion. Additional workspaces require an explicitly reviewed key. Unrecorded resources, unresolved creates, permission failures and incomplete asynchronous operations stop later phases. Existing manual cleanup remains available. Remote data exports and retention verification are separate. Live destructive acceptance remains to be performed.
