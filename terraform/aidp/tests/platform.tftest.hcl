mock_provider "oci" {}
variables {
  oci_profile     = "OFFLINE_TEST"
  region          = "eu-frankfurt-1"
  compartment_id  = "ocid1.compartment.oc1..test"
  lakehouse_id    = "ocid1.autonomousdatabase.oc1.eu-frankfurt-1.test"
  admin_secret_id = "ocid1.vaultsecret.oc1.eu-frankfurt-1.test"
}
run "ai_features_use_lakehouse_and_vault_references" {
  command = plan
  assert {
    condition     = oci_ai_data_platform_ai_data_platform.poc.is_enable_ai_feature && oci_ai_data_platform_ai_data_platform.poc.vector_db_id == var.lakehouse_id && oci_ai_data_platform_ai_data_platform.poc.vector_db_admin_secret_id == var.admin_secret_id
    error_message = "AIDP must reference its dedicated Lakehouse and Vault ADMIN secret."
  }
}
