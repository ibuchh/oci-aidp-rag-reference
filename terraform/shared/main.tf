locals {
  tags = { Project = var.name, Purpose = "Standalone-vLLM-POC" }
}

resource "oci_core_vcn" "poc" {
  compartment_id = var.compartment_id
  cidr_blocks    = [var.vcn_cidr]
  display_name   = var.name
  dns_label      = "vllmpoc"
  freeform_tags  = local.tags
}

resource "oci_core_nat_gateway" "poc" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.poc.id
  display_name   = "${var.name}-nat"
}
data "oci_core_services" "oracle" {
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}
resource "oci_core_service_gateway" "poc" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.poc.id
  display_name   = "${var.name}-services"
  services {
    service_id = data.oci_core_services.oracle.services[0].id
  }
}
resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.poc.id
  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_nat_gateway.poc.id
  }
  route_rules {
    destination       = data.oci_core_services.oracle.services[0].cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.poc.id
  }
}
resource "oci_core_security_list" "private" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.poc.id
  display_name   = "${var.name}-private"
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
  ingress_security_rules {
    source   = cidrsubnet(var.vcn_cidr, 8, 32)
    protocol = "6"
    tcp_options {
      min = 22
      max = 22
    }
  }
  # Inference is initially reachable only through a Bastion port-forward.
  ingress_security_rules {
    source   = cidrsubnet(var.vcn_cidr, 8, 32)
    protocol = "6"
    tcp_options {
      min = 443
      max = 443
    }
  }
}
resource "oci_core_subnet" "gpu" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.poc.id
  cidr_block                 = cidrsubnet(var.vcn_cidr, 8, 33)
  display_name               = "${var.name}-vllm"
  dns_label                  = "vllm"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.private.id]
}
resource "oci_core_subnet" "bastion" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.poc.id
  cidr_block                 = cidrsubnet(var.vcn_cidr, 8, 32)
  display_name               = "${var.name}-bastion"
  dns_label                  = "bastion"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.private.id]
}
resource "oci_core_subnet" "storage" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.poc.id
  cidr_block                 = cidrsubnet(var.vcn_cidr, 8, 34)
  display_name               = "${var.name}-storage"
  dns_label                  = "storage"
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.storage.id]
}
resource "oci_core_security_list" "storage" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.poc.id
  display_name   = "${var.name}-storage"
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
  dynamic "ingress_security_rules" {
    for_each = toset(["6", "17"])
    content {
      source   = cidrsubnet(var.vcn_cidr, 8, 33)
      protocol = ingress_security_rules.value
      # NFSv3/RPC ports are restricted by source to the vllm subnet.
    }
  }
}
resource "oci_bastion_bastion" "poc" {
  bastion_type                 = "STANDARD"
  compartment_id               = var.compartment_id
  target_subnet_id             = oci_core_subnet.bastion.id
  client_cidr_block_allow_list = [var.admin_cidr]
  name                         = var.bastion_name
  max_session_ttl_in_seconds   = 10800
}
resource "oci_file_storage_file_system" "models" {
  lifecycle {
    prevent_destroy = true
  }
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_id
  display_name        = "${var.name}-models"
  freeform_tags       = local.tags
}
resource "oci_file_storage_mount_target" "models" {
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_id
  subnet_id           = oci_core_subnet.storage.id
  display_name        = "${var.name}-models"
}
resource "oci_file_storage_export" "models" {
  export_set_id  = oci_file_storage_mount_target.models.export_set_id
  file_system_id = oci_file_storage_file_system.models.id
  path           = "/models"
  export_options {
    source          = cidrsubnet(var.vcn_cidr, 8, 33)
    access          = "READ_WRITE"
    identity_squash = "NONE"
  }
}
