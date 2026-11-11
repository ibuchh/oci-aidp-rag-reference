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
variable "lakehouse_id" { type = string }
variable "admin_secret_id" { type = string }
variable "name" {
  type    = string
  default = "vllm-aidp-poc"
}
provider "oci" {
  region              = var.region
  config_file_profile = var.oci_profile
}
resource "oci_ai_data_platform_ai_data_platform" "poc" {
  compartment_id            = var.compartment_id
  display_name              = var.name
  default_workspace_name    = "${var.name}-workspace"
  is_enable_ai_feature      = true
  vector_db_id              = var.lakehouse_id
  vector_db_admin_secret_id = var.admin_secret_id
  freeform_tags             = { Project = var.name, Component = "AIDP", Data = "Synthetic" }
  lifecycle { prevent_destroy = true }
  timeouts { create = "90m" }
}
output "aidp_id" { value = oci_ai_data_platform_ai_data_platform.poc.id }
output "state" { value = oci_ai_data_platform_ai_data_platform.poc.state }
output "ai_feature_status" { value = oci_ai_data_platform_ai_data_platform.poc.ai_feature_status }
