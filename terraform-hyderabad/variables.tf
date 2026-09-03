# terraform/variables.tf

variable "tenancy_ocid" {
  description = "Oracle Cloud tenancy OCID (from OCI console → Profile → Tenancy)"
  type        = string
}

variable "user_ocid" {
  description = "Your OCI user OCID (from OCI console → Profile → User Settings)"
  type        = string
}

variable "fingerprint" {
  description = "API key fingerprint (from OCI console → User Settings → API Keys)"
  type        = string
}

variable "private_key_path" {
  description = "Path to your OCI API private key (e.g. ~/.oci/oci_api_key.pem)"
  type        = string
  default     = "~/.oci/oci_api_key.pem"
}

variable "region" {
  description = "OCI region — your home region is ap-hyderabad-1 (India South)"
  type        = string
  default     = "ap-hyderabad-1"
}

variable "compartment_ocid" {
  description = "Compartment OCID — use your root tenancy OCID for free tier"
  type        = string
}

variable "ssh_public_key" {
  description = "Your SSH public key content (cat ~/.ssh/id_rsa.pub)"
  type        = string
}

variable "my_ip_cidr" {
  description = "Your home/office IP in CIDR format for SSH access (e.g. 1.2.3.4/32)"
  type        = string
}

variable "repo_url" {
  description = "GitHub repo URL (e.g. https://github.com/yourname/quantbot.git)"
  type        = string
}

variable "vm_image_ocid" {
  description = "Ubuntu 22.04 ARM image OCID for your region. Find it in OCI Console → Compute → Instances → Create Instance → Change Image → Ubuntu 22.04 → click (i) icon → copy OCID"
  type        = string
}

variable "env_file_contents" {
  description = "Full contents of .env file — set as TF_VAR_env_file_contents in CI"
  type        = string
  sensitive   = true
  default     = ""
}
