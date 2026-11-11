mock_provider "oci" {}
variables {
  oci_profile     = "OFFLINE_TEST"
  region          = "eu-frankfurt-1"
  compartment_id  = "ocid1.compartment.oc1..test"
  subnet_id       = "ocid1.subnet.oc1.eu-frankfurt-1.test"
  nsg_id          = "ocid1.networksecuritygroup.oc1.eu-frankfurt-1.test"
  admin_secret_id = "ocid1.vaultsecret.oc1.eu-frankfurt-1.test"
}
run "lakehouse_is_private_and_uses_secret_reference" {
  command = plan
  assert {
    condition     = oci_database_autonomous_database.lakehouse.db_version == "26ai" && oci_database_autonomous_database.lakehouse.db_workload == "LH"
    error_message = "AI features require the 26ai Lakehouse proposal."
  }
  assert {
    condition     = oci_database_autonomous_database.lakehouse.subnet_id == var.subnet_id && oci_database_autonomous_database.lakehouse.secret_id == var.admin_secret_id && !oci_database_autonomous_database.lakehouse.is_auto_scaling_enabled
    error_message = "Private endpoint, password reference and bounded sizing must be preserved."
  }
}
