# terraform/outputs.tf

output "vm_public_ip" {
  description = "Public IP of your QuantBot server"
  value       = oci_core_instance.quantbot_vm.public_ip
}

output "ssh_command" {
  description = "SSH command to connect to your server"
  value       = "ssh ubuntu@${oci_core_instance.quantbot_vm.public_ip}"
}

output "dashboard_url" {
  description = "Dashboard URL (after nginx + SSL are configured)"
  value       = "https://${oci_core_instance.quantbot_vm.public_ip}"
}
