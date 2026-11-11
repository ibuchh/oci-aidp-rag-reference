# Apply using the customer-approved IAM administrator profile.
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
variable "home_region" {
  type    = string
  default = "us-ashburn-1"
}
variable "tenancy_id" { type = string }
variable "compartment_id" { type = string }
variable "lakehouse_admin_secret_id" { type = string }
variable "principal_type" {
  type    = string
  default = "aidataplatform"
  validation {
    condition     = contains(["aidataplatform", "datalake"], var.principal_type)
    error_message = "Use the principal type documented for the actual AIDP instance."
  }
}

provider "oci" {
  region              = var.home_region
  config_file_profile = var.oci_profile
}

locals {
  principal   = "request.principal.type='${var.principal_type}'"
  compartment = "compartment id ${var.compartment_id}"
  tags        = { Project = var.name, Component = "AIDP-IAM" }
}

resource "oci_identity_policy" "identity" {
  compartment_id = var.tenancy_id
  name           = "${var.name}-service-identity"
  description    = "AIDP documented identity inspection, tag use and namespace read"
  statements = [
    "Allow any-user to {AUTHENTICATION_INSPECT, DOMAIN_INSPECT, DOMAIN_READ, DYNAMIC_GROUP_INSPECT, GROUP_INSPECT, GROUP_MEMBERSHIP_INSPECT, USER_INSPECT, USER_READ} in tenancy where ${local.principal}",
    "Allow any-user to {TAG_NAMESPACE_USE} in tenancy where ${local.principal}",
    "Allow any-user to read objectstorage-namespaces in tenancy where all {${local.principal}, request.permission='OBJECTSTORAGE_NAMESPACE_READ'}",
  ]
  freeform_tags = local.tags
}

resource "oci_identity_policy" "service" {
  compartment_id = var.compartment_id
  name           = "${var.name}-service-resources"
  description    = "AIDP service access scoped to the customer POC compartment with bucket ownership conditions"
  statements = [
    "Allow any-user to manage log-groups in ${local.compartment} where ${local.principal}",
    "Allow any-user to read log-content in ${local.compartment} where ${local.principal}",
    "Allow any-user to use metrics in ${local.compartment} where all {${local.principal}, target.metrics.namespace='oracle_aidataplatform'}",
    "Allow any-user to manage buckets in ${local.compartment} where all {${local.principal}, any {request.permission='BUCKET_CREATE', request.permission='BUCKET_INSPECT', request.permission='BUCKET_READ', request.permission='BUCKET_UPDATE'}}",
    "Allow any-user to manage buckets in ${local.compartment} where all {${local.principal}, request.principal.id=target.resource.tag.orcl-aidp.governingAidpId, any {request.permission='BUCKET_DELETE', request.permission='PAR_MANAGE', request.permission='RETENTION_RULE_LOCK', request.permission='RETENTION_RULE_MANAGE'}}",
    "Allow any-user to manage objects in ${local.compartment} where all {${local.principal}, request.principal.id=target.bucket.system-tag.orcl-aidp.governingAidpId}",
    "Allow any-user to manage vnics in ${local.compartment} where ${local.principal}",
    "Allow any-user to use subnets in ${local.compartment} where ${local.principal}",
    "Allow any-user to use network-security-groups in ${local.compartment} where ${local.principal}",
    "Allow any-user to use generative-ai-family in ${local.compartment} where ${local.principal}",
  ]
  freeform_tags = local.tags
}

resource "oci_identity_policy" "lakehouse_secret" {
  compartment_id = var.compartment_id
  name           = "${var.name}-lakehouse-secret"
  description    = "Allow AIDP to read only the dedicated Lakehouse ADMIN secret bundle"
  statements = [
    "Allow any-user to read secret-bundles in ${local.compartment} where all {${local.principal}, target.secret.id='${var.lakehouse_admin_secret_id}'}",
  ]
  freeform_tags = local.tags
}

output "policy_ids" {
  value = {
    identity         = oci_identity_policy.identity.id
    service          = oci_identity_policy.service.id
    lakehouse_secret = oci_identity_policy.lakehouse_secret.id
  }
}

variable "name" { type = string }
