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
}
run "private_network_and_server_generated_secret" {
  command = plan
  assert {
    condition     = oci_core_subnet.workspace.prohibit_public_ip_on_vnic && oci_core_network_security_group_security_rule.database_out.destination == var.workspace_cidr
    error_message = "AIDP subnet must prohibit public VM IPs and target its same-subnet Lakehouse."
  }
  assert {
    condition     = oci_vault_secret.lakehouse_admin.enable_auto_generation && oci_vault_secret.lakehouse_admin.secret_generation_context[0].generation_template == "DBAAS_DEFAULT_PASSWORD"
    error_message = "ADMIN password must be generated inside Vault."
  }
}
