variable "gpu_https_nsg_id" {
  description = "Existing GPU-attached NSG read from the customer monitoring outputs"
  type        = string
  default     = null
}
resource "oci_core_network_security_group_security_rule" "aidp_inference_https" {
  count                     = var.gpu_https_nsg_id == null ? 0 : 1
  network_security_group_id = var.gpu_https_nsg_id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = var.workspace_cidr
  stateless                 = false
  description               = "AIDP private inference HTTPS"
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
  lifecycle {
    precondition {
      condition     = try(can(cidrnetmask(var.workspace_cidr)) && tonumber(split("/", var.workspace_cidr)[1]) >= 24, false)
      error_message = "Use a verified private runtime subnet of /24 or smaller."
    }
  }
}
