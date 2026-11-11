# Inference, monitoring and Streamlit operations

## Overview

Run commands from the package root on the Mac. `scripts/node.py` reads `poc-config.json`, opens temporary Bastion sessions, checks SSH host fingerprints against OCI console evidence, runs the selected check and closes its sessions. No inference API key needs to be copied into a terminal.

## Preconditions / Prerequisites

Complete the inference deployment, confirm the current Bastion allowlist, and retain the configured SSH key and state directory. Public Grafana and Streamlit URLs are printed by deployment and stored in the protected login files described in the README.

## Procedure

### 1 Validate hosts

```bash
python3 scripts/node.py gpu-health
python3 scripts/node.py monitoring-health
```

Expected: two NVIDIA A10 GPUs, active vLLM/Nginx, a mounted model filesystem, healthy Prometheus targets and passing dashboard query checks. The monitoring validator runs against the rendered configuration on the VM, not the source files containing replacement markers.

### 2 Send one inference request

```bash
python3 scripts/node.py chat
```

Expected: `PASS` with token usage. This bounded synthetic request calls the new VM's authenticated private endpoint at `/v1/chat/completions` using model alias `qwen3-8b`. Prompts/responses and the API key are not printed by this check.

For a strict synthetic tool round trip:

```bash
python3 scripts/node.py tool-test
```

Expected: `PASS` and `tool_observed: true`. A model answer alone is insufficient; the client requires a real allowed tool call and the follow-up response.

### 3 Use the public chat UI

Open the URL in `states/.pocctl/streamlit-login.json`, authenticate with its username/password, and enter a synthetic prompt. The UI supports a system prompt, model selection, temperature, output-token limit, top-p, penalties, stop sequences, streaming and timeout. Review the redacted request/response inspector as needed.

The app retains conversation data in session memory, with optional manual export. It has no automatic chat-history database. Model/API credentials stay server-side. The public HTTPS proxy strips the browser's Basic-auth header before forwarding to Streamlit. Caddy supports WebSocket proxying used by Streamlit. Its hashed-password `basic_auth` rule covers the complete hostname. [Caddy authentication reference](https://caddyserver.com/docs/caddyfile/directives/basic_auth).

Separate visitors authenticate with the shared POC browser login but receive separate Streamlit sessions. This is POC authentication, without a corporate identity lifecycle or per-user authorization model.

### 4 Generate dashboard traffic

Keep Grafana open, choose **Last 15 minutes** and a refresh interval such as 5 seconds. On the Mac:

```bash
python3 scripts/node.py load \
  --max-requests 500
```

The load driver runs on the monitoring VM, apart from the inference process. It uses bounded warmup, steady, queued and cooldown phases with concurrency 1, 4, 8 and 2. The full phases total approximately seven minutes; the maximum-request cap or error limits can end the run sooner. This is a synthetic metrics demonstration, not a customer throughput benchmark.

Results are saved on the monitoring VM under `/var/lib/monitoring/load-tests/UTC_TIMESTAMP`. `Ctrl+C` stops the foreground SSH command and triggers tunnel cleanup; check for a surviving remote load process if the network disconnects unexpectedly before starting another test.

### 5 Read the dashboards

- **Inference traffic:** successful requests, request failures, prompt/output token rates and aggregate throughput.
- **Responsiveness:** time to first token, inter-token latency and request duration. Queued load can increase waiting time even when utilization is high.
- **Scheduling/cache:** running versus waiting requests and KV-cache utilization. Context and active sequences consume memory in addition to model weights.
- **GPUs:** both GPU utilization series, framebuffer usage, temperature/power and available error/health indicators. Some DCGM counters depend on device/driver support.
- **Hosts:** CPU, RAM, disk/filesystem pressure, network and exporter reachability on both hosts.
- **Synthetic probe:** authenticated private request success and latency. It runs periodically, so low background traffic is expected when no user is chatting.

Two 24 GiB GPUs provide distributed capacity; they do not form one automatically pooled GPU and do not guarantee twice the speed. The serving baseline explicitly uses tensor parallelism across both devices.

### 6 Reinstall or repair the public app

After fixing a failed app build, proxy configuration or certificate issuance prerequisite:

```bash
python3 scripts/configure_public_apps.py
```

Expected: Streamlit listens on monitoring loopback port 8501, authenticated public HTTPS returns success, and unauthenticated HTTPS returns 401. The existing generated password is preserved. Login metadata is refreshed locally with mode 0600. Diagnose the failure before rerunning repeatedly against ACME issuance limits.

An optional Mac client is included:

```bash
cd apps/streamlit
./setup.sh
./run-local.sh --poc-config ../../poc-config.json
```

This uses the same inference stack through Bastion and opens a local Streamlit server. The primary deployment remains the authenticated public app on the monitoring VM.

## Validation Steps

Run `gpu-health`, `monitoring-health`, `chat` and `tool-test`; open both public URLs; verify a browser session can chat and an unauthenticated request cannot open Streamlit. During load, confirm request/token metrics and both GPU series change. During AIDP testing, additionally verify actual requests from its private managed runtime.

## Rollback

Stop synthetic traffic first. A failed Streamlit installation leaves inference/monitoring infrastructure available for repair. Before tearing down the stack, follow [CLEANUP.md](CLEANUP.md), including AIDP dependencies and recorded secrets.
