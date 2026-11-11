variable "oci_profile" { default = "DEFAULT" }
variable "region" { default = "eu-frankfurt-1" }
variable "compartment_id" { type = string }
variable "availability_domain" { type = string }
variable "image_id" { type = string }
variable "shape" { default = "VM.GPU.A10.2" }
variable "boot_volume_id" {
  type    = string
  default = null
}
variable "private_ip" {
  type = string
}
variable "ssh_public_key_path" { default = "~/.ssh/id_rsa.pub" }
variable "model_revision" { type = string }
variable "vllm_image" { default = "vllm/vllm-openai:v0.11.0@sha256:014a95f21c9edf6abe0aea6b07353f96baa4ec291c427bb1176dc7c93a85845c" }
variable "name" { default = "vllm-aidp-poc" }
variable "monitoring_nsg_ids" {
  type    = list(string)
  default = []
}
