data "terraform_remote_state" "shared" {
  backend = "local"
  config  = { path = "${path.module}/../shared/terraform.tfstate" }
}
data "oci_core_subnet" "gpu" {
  subnet_id = data.terraform_remote_state.shared.outputs.gpu_subnet_id
}
locals {
  tags = { Project = var.name, Purpose = "vLLM-Monitoring" }
}
resource "oci_core_internet_gateway" "monitoring" {
  compartment_id = var.compartment_id
  vcn_id         = data.terraform_remote_state.shared.outputs.vcn_id
  display_name   = "${var.name}-monitoring-igw"
}
resource "oci_core_route_table" "monitoring" {
  compartment_id = var.compartment_id
  vcn_id         = data.terraform_remote_state.shared.outputs.vcn_id
  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_internet_gateway.monitoring.id
  }
}
resource "oci_core_security_list" "monitoring" {
  compartment_id = var.compartment_id
  vcn_id         = data.terraform_remote_state.shared.outputs.vcn_id
  display_name   = "${var.name}-monitoring"
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
  dynamic "ingress_security_rules" {
    for_each = toset([80, 443])
    content {
      source   = "0.0.0.0/0"
      protocol = "6"
      tcp_options {
        min = ingress_security_rules.value
        max = ingress_security_rules.value
      }
    }
  }
  ingress_security_rules {
    source   = data.terraform_remote_state.shared.outputs.bastion_subnet_cidr
    protocol = "6"
    tcp_options {
      min = 22
      max = 22
    }
  }
}
resource "oci_core_subnet" "monitoring" {
  compartment_id             = var.compartment_id
  vcn_id                     = data.terraform_remote_state.shared.outputs.vcn_id
  cidr_block                 = var.subnet_cidr
  display_name               = "${var.name}-monitoring"
  dns_label                  = "monitoring"
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.monitoring.id
  security_list_ids          = [oci_core_security_list.monitoring.id]
}
# The NSG is attached to the existing GPU VNIC without changing the GPU instance.
resource "oci_core_network_security_group" "metrics" {
  compartment_id = var.compartment_id
  vcn_id         = data.terraform_remote_state.shared.outputs.vcn_id
  display_name   = "${var.name}-gpu-metrics"
}
resource "oci_core_network_security_group_security_rule" "metrics" {
  for_each                  = toset(["443", "9443"])
  network_security_group_id = oci_core_network_security_group.metrics.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "${var.private_ip}/32"
  source_type               = "CIDR_BLOCK"
  tcp_options {
    destination_port_range {
      min = tonumber(each.value)
      max = tonumber(each.value)
    }
  }
}
resource "oci_core_instance" "monitoring" {
  availability_domain  = var.availability_domain
  compartment_id       = var.compartment_id
  shape                = var.shape
  display_name         = "${var.name}-monitoring"
  freeform_tags        = local.tags
  preserve_boot_volume = true
  instance_options { are_legacy_imds_endpoints_disabled = true }
  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gbs
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.monitoring.id
    assign_public_ip = true
    private_ip       = var.private_ip
    hostname_label   = "observability"
  }
  source_details {
    source_type             = "image"
    source_id               = var.image_id
    boot_volume_size_in_gbs = 50
  }
  metadata = {
    ssh_authorized_keys = file(pathexpand(var.ssh_public_key_path))
  }
  lifecycle {
    prevent_destroy = true
  }
}
resource "oci_core_volume" "metrics" {
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_id
  display_name        = "${var.name}-metrics"
  size_in_gbs         = var.metrics_volume_gbs
  vpus_per_gb         = 10
  freeform_tags       = local.tags
  lifecycle {
    prevent_destroy = true
  }
}
resource "oci_core_volume_attachment" "metrics" {
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.monitoring.id
  volume_id       = oci_core_volume.metrics.id
  device          = "/dev/oracleoci/oraclevdb"
  is_read_only    = false
}
