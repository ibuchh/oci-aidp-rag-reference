variable "oci_profile" { type = string }
variable "region" { type = string }
variable "compartment_id" { type = string }
variable "vcn_id" { type = string }
variable "private_route_table_id" { type = string }
variable "dhcp_options_id" { type = string }
variable "inference_private_ip" { type = string }
variable "name" {
  type    = string
  default = "vllm-aidp-poc"
}
variable "workspace_cidr" {
  type    = string
  default = "10.160.36.0/24"
}
variable "vcn_dns_ip" { type = string }
