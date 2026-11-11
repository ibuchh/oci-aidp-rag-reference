output "bastion_id" { value = oci_bastion_bastion.poc.id }
output "vcn_id" { value = oci_core_vcn.poc.id }
output "fss_mount_ip" { value = oci_file_storage_mount_target.models.ip_address }
output "fss_id" { value = oci_file_storage_file_system.models.id }
output "gpu_subnet_id" { value = oci_core_subnet.gpu.id }
output "bastion_subnet_cidr" { value = oci_core_subnet.bastion.cidr_block }
