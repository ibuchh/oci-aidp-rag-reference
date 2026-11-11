# GPU image requirements

## Customer GPU image contract

Supply a sanitized Ubuntu 22.04 GPU image with:

1. A kernel and NVIDIA driver combination already tested on OCI A10.2. The reference image used kernel 6.8, NVIDIA driver 595 and CUDA 13.2; these version strings alone do not prove compatibility.
2. Both A10 GPUs visible to `nvidia-smi`, healthy devices and a functioning NVIDIA container runtime. Bootstrap installs/configures container components, but requires a working host driver.
3. Working DCGM compatible with the host driver. The exporter install checks for the required host libraries.
4. OCI-compatible networking, cloud-init and the `ubuntu` SSH user with sudo.
5. OCI metadata v2 compatibility. Both fresh VM definitions explicitly set `are_legacy_imds_endpoints_disabled = true` to meet this tenancy's governance restrictions.
6. No old API keys, TLS private keys, SSH host keys, user authorized keys, shell history, customer data, service containers, model auto-start units or completed cloud-init identity from a previous VM. Clean the source image using your approved image pipeline before export.
7. Image/shape compatibility for `VM.GPU.A10.2`, and an image that fits the 200 GiB GPU boot disk.

The wizard checks image availability, Ubuntu metadata, image size and declared shape compatibility. It cannot inspect the driver's operational state inside a stopped image. GPU/container validation occurs after launch. A customer's import procedure and image licensing remain prerequisites; the package does not include a disk image or infer that an image is clean.

