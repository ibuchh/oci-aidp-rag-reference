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
variable "home_region" { type = string }
variable "compartment_id" { type = string }
variable "name" { type = string }
variable "inference_secret_id" { type = string }
variable "runtime_principal_type" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_-]*$", var.runtime_principal_type))
    error_message = "Use the verified runtime principal type."
  }
}
variable "runtime_principal_id" {
  type = string
  validation {
    condition     = startswith(var.runtime_principal_id, "ocid1.")
    error_message = "Use the exact verified runtime principal OCID."
  }
}
provider "oci" {
  region              = var.home_region
  config_file_profile = var.oci_profile
}
resource "oci_identity_policy" "runtime_secret" {
  compartment_id = var.compartment_id
  name           = "${var.name}-runtime-inference-secret"
  description    = "Only the verified AI Compute runtime may read this POC inference secret"
  statements = [
    "Allow any-user to read secret-bundles in compartment id ${var.compartment_id} where all {request.principal.type='${var.runtime_principal_type}', request.principal.id='${var.runtime_principal_id}', target.secret.id='${var.inference_secret_id}'}"
  ]
  freeform_tags = { Project = var.name, Component = "Runtime-IAM" }
}
output "policy_id" { value = oci_identity_policy.runtime_secret.id }
