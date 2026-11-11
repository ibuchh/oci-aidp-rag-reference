mock_provider "oci" {}
variables {
  name                      = "fixture-poc"
  oci_profile               = "OFFLINE_ADMIN_TEST"
  tenancy_id                = "ocid1.tenancy.oc1..test"
  compartment_id            = "ocid1.compartment.oc1..test"
  lakehouse_admin_secret_id = "ocid1.vaultsecret.oc1.eu-frankfurt-1.test"
}
run "service_policy_boundaries" {
  command = plan
  assert {
    condition     = length(oci_identity_policy.identity.statements) == 3 && length(oci_identity_policy.service.statements) == 10 && length(oci_identity_policy.lakehouse_secret.statements) == 1
    error_message = "Review the policy scope when adding permissions."
  }
  assert {
    condition     = strcontains(oci_identity_policy.lakehouse_secret.statements[0], var.lakehouse_admin_secret_id) && oci_identity_policy.service.compartment_id == var.compartment_id
    error_message = "Secret access must name the dedicated secret and resources must be compartment-scoped."
  }
}
