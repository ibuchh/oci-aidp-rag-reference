variable "oci_profile" { default = "DEFAULT" }
variable "region" { default = "eu-frankfurt-1" }
variable "compartment_id" { type = string }
variable "availability_domain" { type = string }
variable "admin_cidr" { type = string }
variable "vcn_cidr" { default = "10.160.0.0/16" }
variable "name" { default = "vllm-aidp-poc" }
variable "bastion_name" { default = "vllmaidppoc" }
