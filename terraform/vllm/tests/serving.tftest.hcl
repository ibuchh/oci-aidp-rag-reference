mock_provider "oci" {}
override_data {
  target = data.terraform_remote_state.shared
  values = {
    outputs = {
      gpu_subnet_id       = "ocid1.subnet.oc1.eu-frankfurt-1.fixture"
      fss_mount_ip        = "172.20.34.41"
      bastion_subnet_cidr = "172.20.32.0/24"
    }
  }
}
variables {
  oci_profile         = "OFFLINE_TEST"
  compartment_id      = "ocid1.compartment.oc1..fixture"
  availability_domain = "fixture:EU-FRANKFURT-1-AD-3"
  image_id            = "ocid1.image.oc1.eu-frankfurt-1.fixture"
  private_ip          = "172.20.33.19"
  model_revision      = "b968826d9c46dd6066d109eabc6255188de91218"
  ssh_public_key_path = "tests/fixture.pub"
}
run "private_gpu_bootstrap_uses_actual_network_and_imdsv2" {
  command = plan
  assert {
    condition     = !oci_core_instance.gpu.create_vnic_details[0].assign_public_ip && oci_core_instance.gpu.instance_options[0].are_legacy_imds_endpoints_disabled
    error_message = "The GPU must remain private and enforce tenancy IMDSv2 governance."
  }
  assert {
    condition     = strcontains(base64decode(oci_core_instance.gpu.metadata["user_data"]), "private_ip='172.20.33.19'") && strcontains(base64decode(oci_core_instance.gpu.metadata["user_data"]), "172.20.34.41:/models")
    error_message = "Certificate SAN and FSS bootstrap must use actual configured network values."
  }
}
