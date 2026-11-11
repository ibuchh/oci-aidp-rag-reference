locals {
  tags = { Project = var.name, Purpose = "Standalone-vLLM-POC" }
}
data "terraform_remote_state" "shared" {
  backend = "local"
  config  = { path = "${path.module}/../shared/terraform.tfstate" }
}
resource "oci_core_instance" "gpu" {
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [metadata["user_data"]]
  }
  availability_domain  = var.availability_domain
  compartment_id       = var.compartment_id
  shape                = var.shape
  display_name         = "${var.name}-vllm"
  freeform_tags        = local.tags
  preserve_boot_volume = true
  instance_options { are_legacy_imds_endpoints_disabled = true }
  create_vnic_details {
    subnet_id        = data.terraform_remote_state.shared.outputs.gpu_subnet_id
    assign_public_ip = false
    private_ip       = var.private_ip
    nsg_ids          = var.monitoring_nsg_ids
    hostname_label   = "inference"
  }
  source_details {
    source_type             = var.boot_volume_id == null ? "image" : "bootVolume"
    source_id               = var.boot_volume_id == null ? var.image_id : var.boot_volume_id
    boot_volume_size_in_gbs = var.boot_volume_id == null ? 200 : null
  }
  metadata = merge({
    ssh_authorized_keys = file(pathexpand(var.ssh_public_key_path))
    }, var.boot_volume_id != null ? {} : {
    user_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tftpl", {
      mount_ip             = data.terraform_remote_state.shared.outputs.fss_mount_ip
      model_revision       = var.model_revision
      vllm_image           = var.vllm_image
      bastion_cidr         = data.terraform_remote_state.shared.outputs.bastion_subnet_cidr
      inference_private_ip = var.private_ip
    }))
  })
}
