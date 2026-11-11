terraform {
  required_version = ">= 1.7.0, < 2.0.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 9.12.1"
    }
  }
}
variable "oci_profile" { type = string }
variable "region" { type = string }
variable "compartment_id" { type = string }
variable "subnet_id" { type = string }
variable "nsg_id" { type = string }
variable "admin_secret_id" { type = string }
variable "name" {
  type    = string
  default = "vllm-aidp-poc"
}
variable "db_name" {
  type    = string
  default = "AIDPPOC"
}
variable "ecpus" {
  type    = number
  default = 2
  validation {
    condition     = var.ecpus >= 2 && floor(var.ecpus) == var.ecpus
    error_message = "Lakehouse requires at least 2 whole ECPUs."
  }
}
variable "storage_tbs" {
  type    = number
  default = 1
  validation {
    condition     = var.storage_tbs >= 1 && floor(var.storage_tbs) == var.storage_tbs
    error_message = "Lakehouse requires at least 1 TB in whole TB increments."
  }
}
provider "oci" {
  region              = var.region
  config_file_profile = var.oci_profile
}
resource "oci_database_autonomous_database" "lakehouse" {
  compartment_id                      = var.compartment_id
  display_name                        = "${var.name}-lakehouse"
  db_name                             = var.db_name
  db_workload                         = "LH"
  db_version                          = "26ai"
  compute_model                       = "ECPU"
  compute_count                       = var.ecpus
  data_storage_size_in_tbs            = var.storage_tbs
  license_model                       = "LICENSE_INCLUDED"
  is_free_tier                        = false
  is_auto_scaling_enabled             = false
  is_auto_scaling_for_storage_enabled = false
  subnet_id                           = var.subnet_id
  nsg_ids                             = [var.nsg_id]
  private_endpoint_label              = "aidppocdb"
  is_mtls_connection_required         = true
  secret_id                           = var.admin_secret_id
  freeform_tags                       = { Project = var.name, Component = "AIDP", Data = "Synthetic" }
  lifecycle { prevent_destroy = true }
  timeouts { create = "90m" }
}
output "lakehouse_id" { value = oci_database_autonomous_database.lakehouse.id }
output "state" { value = oci_database_autonomous_database.lakehouse.state }
