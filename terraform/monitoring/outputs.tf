output "instance_id" { value = oci_core_instance.monitoring.id }
output "private_ip" { value = oci_core_instance.monitoring.private_ip }
output "public_ip" { value = oci_core_instance.monitoring.public_ip }
output "grafana_url" { value = "https://${replace(oci_core_instance.monitoring.public_ip, ".", "-")}.sslip.io" }
output "metrics_volume_id" { value = oci_core_volume.metrics.id }
output "gpu_metrics_nsg_id" { value = oci_core_network_security_group.metrics.id }
