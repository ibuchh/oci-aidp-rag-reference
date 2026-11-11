terraform {
  required_version = ">= 1.7.0, < 2.0.0"
  required_providers {
    oci = { source = "oracle/oci", version = "= 9.12.1" }
  }
}
variable "oci_profile" { type = string }
variable "home_region" { type = string }
variable "compartment_id" { type = string }
variable "name" { type = string }
variable "aidp_id" { type = string }
variable "inference_secret_id" { type = string }
variable "kms_key_id" { type = string }
provider "oci" {
  region              = var.home_region
  config_file_profile = var.oci_profile
}
resource "oci_identity_policy" "inference_credential" {
  compartment_id = var.compartment_id
  name           = "${var.name}-aidp-inference-credential"
  description    = "AIDP Credential Store access to this POC inference secret only"
  statements = [
    "Allow any-user to read secret-bundles in compartment id ${var.compartment_id} where all {request.principal.type='aidataplatform', request.principal.id='${var.aidp_id}', target.secret.id='${var.inference_secret_id}'}",
    "Allow any-user to {SECRET_READ, SECRET_UPDATE} in compartment id ${var.compartment_id} where all {request.principal.type='aidataplatform', request.principal.id='${var.aidp_id}', target.secret.id='${var.inference_secret_id}'}",
    "Allow any-user to use keys in compartment id ${var.compartment_id} where all {request.principal.type='aidataplatform', request.principal.id='${var.aidp_id}', target.key.id='${var.kms_key_id}'}"
  ]
  freeform_tags = { Project = var.name, Component = "AIDP-Credential-IAM" }
}
output "policy_id" { value = oci_identity_policy.inference_credential.id }
