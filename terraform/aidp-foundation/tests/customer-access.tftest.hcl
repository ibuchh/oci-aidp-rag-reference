mock_provider "oci" {}
variables {
  oci_profile            = "OFFLINE_TEST"
  region                 = "eu-frankfurt-1"
  compartment_id         = "ocid1.compartment.oc1..test"
  vcn_id                 = "ocid1.vcn.oc1.eu-frankfurt-1.test"
  private_route_table_id = "ocid1.routetable.oc1.eu-frankfurt-1.test"
  dhcp_options_id        = "ocid1.dhcpoptions.oc1.eu-frankfurt-1.test"
  inference_private_ip   = "10.140.33.19"
  vcn_dns_ip             = "169.254.169.254"
  gpu_https_nsg_id       = "ocid1.networksecuritygroup.oc1.eu-frankfurt-1.fixture"
}
run "customer_private_inference_access" {
  command = plan
  assert {
    condition     = length(oci_core_network_security_group_security_rule.aidp_inference_https) == 1 && oci_core_network_security_group_security_rule.aidp_inference_https[0].source == var.workspace_cidr && oci_core_network_security_group_security_rule.aidp_inference_https[0].tcp_options[0].destination_port_range[0].min == 443
    error_message = "Customer inference access must be limited to the runtime subnet and HTTPS."
  }
}
