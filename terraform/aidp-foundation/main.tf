provider "oci" {
  region              = var.region
  config_file_profile = var.oci_profile
}

locals {
  tags = { Project = var.name, Component = "AIDP", Data = "Synthetic" }
}

# Existing VCN, NAT and service routes remain owned by terraform/shared.
# No incoming traffic is permitted by these security lists. Workload NSGs
# provide the explicitly scoped flows below.
resource "oci_core_security_list" "workspace" {
  compartment_id = var.compartment_id
  vcn_id         = var.vcn_id
  display_name   = "${var.name}-workspace"
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "6"
    tcp_options {
      min = 443
      max = 443
    }
    description = "HTTPS for OCI services, dependencies and private inference"
  }
  egress_security_rules {
    destination = var.workspace_cidr
    protocol    = "6"
    tcp_options {
      min = 1522
      max = 1522
    }
    description = "AI Compute to its same-subnet private Lakehouse using mTLS"
  }
  dynamic "egress_security_rules" {
    for_each = toset(["6", "17"])
    content {
      destination = "${var.vcn_dns_ip}/32"
      protocol    = egress_security_rules.value
      dynamic "tcp_options" {
        for_each = egress_security_rules.value == "6" ? [1] : []
        content {
          min = 53
          max = 53
        }
      }
      dynamic "udp_options" {
        for_each = egress_security_rules.value == "17" ? [1] : []
        content {
          min = 53
          max = 53
        }
      }
    }
  }
}

resource "oci_core_subnet" "workspace" {
  compartment_id             = var.compartment_id
  vcn_id                     = var.vcn_id
  display_name               = "${var.name}-workspace"
  cidr_block                 = var.workspace_cidr
  dns_label                  = "aidpwork"
  prohibit_public_ip_on_vnic = true
  route_table_id             = var.private_route_table_id
  dhcp_options_id            = var.dhcp_options_id
  security_list_ids          = [oci_core_security_list.workspace.id]
  freeform_tags              = local.tags
}

resource "oci_core_network_security_group" "workspace" {
  compartment_id = var.compartment_id
  vcn_id         = var.vcn_id
  display_name   = "${var.name}-workspace"
  freeform_tags  = local.tags
}

resource "oci_core_network_security_group" "lakehouse" {
  compartment_id = var.compartment_id
  vcn_id         = var.vcn_id
  display_name   = "${var.name}-lakehouse"
  freeform_tags  = local.tags
}

resource "oci_core_network_security_group_security_rule" "database_out" {
  network_security_group_id = oci_core_network_security_group.workspace.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination_type          = "CIDR_BLOCK"
  destination               = var.workspace_cidr
  tcp_options {
    destination_port_range {
      min = 1522
      max = 1522
    }
  }
}

resource "oci_core_network_security_group_security_rule" "database_in" {
  network_security_group_id = oci_core_network_security_group.lakehouse.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = var.workspace_cidr
  tcp_options {
    destination_port_range {
      min = 1522
      max = 1522
    }
  }
}

resource "oci_kms_vault" "poc" {
  compartment_id = var.compartment_id
  display_name   = "${var.name}-secrets"
  vault_type     = "DEFAULT"
  freeform_tags  = local.tags
  lifecycle { prevent_destroy = true }
}

resource "oci_kms_key" "secrets" {
  compartment_id      = var.compartment_id
  display_name        = "${var.name}-secrets"
  management_endpoint = oci_kms_vault.poc.management_endpoint
  protection_mode     = "HSM"
  key_shape {
    algorithm = "AES"
    length    = 32
  }
  freeform_tags = local.tags
  lifecycle { prevent_destroy = true }
}

# Generate the ADMIN password inside Vault. Never retrieve it into Terraform.
resource "oci_vault_secret" "lakehouse_admin" {
  compartment_id         = var.compartment_id
  vault_id               = oci_kms_vault.poc.id
  key_id                 = oci_kms_key.secrets.id
  secret_name            = "${var.name}-lakehouse-admin"
  enable_auto_generation = true
  secret_generation_context {
    generation_type     = "PASSPHRASE"
    generation_template = "DBAAS_DEFAULT_PASSWORD"
    passphrase_length   = 24
  }
  freeform_tags = local.tags
  lifecycle { prevent_destroy = true }
}
