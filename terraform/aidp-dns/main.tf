terraform {
  required_version = ">= 1.7.0, < 2.0.0"
  required_providers {
    oci = { source = "oracle/oci", version = "= 9.12.1" }
  }
}
variable "oci_profile" { type = string }
variable "region" { type = string }
variable "compartment_id" { type = string }
variable "name" { type = string }
variable "view_id" { type = string }
variable "inference_private_ip" { type = string }
# Compatibility alias already covered by the deployed GPU certificate's SAN.
variable "inference_dns_name" { default = "inference.lab" }
provider "oci" {
  region              = var.region
  config_file_profile = var.oci_profile
}
resource "oci_dns_zone" "inference" {
  compartment_id = var.compartment_id
  name           = var.inference_dns_name
  zone_type      = "PRIMARY"
  scope          = "PRIVATE"
  view_id        = var.view_id
  freeform_tags  = { Project = var.name, Component = "AIDP-Private-DNS" }
}
resource "oci_dns_rrset" "inference" {
  zone_name_or_id = oci_dns_zone.inference.id
  domain          = var.inference_dns_name
  rtype           = "A"
  scope           = "PRIVATE"
  view_id         = var.view_id
  items {
    domain = var.inference_dns_name
    rtype  = "A"
    rdata  = var.inference_private_ip
    ttl    = 60
  }
}
output "private_dns_name" { value = var.inference_dns_name }
output "zone_id" { value = oci_dns_zone.inference.id }
