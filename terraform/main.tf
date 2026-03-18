# terraform/main.tf — Oracle Cloud Always-Free ARM instance for QuantBot
# ════════════════════════════════════════════════════════════════════
# Free tier used:
#   VM.Standard.A1.Flex  — 4 OCPU, 24 GB RAM  (ARM, always free)
#   Block Volume 50 GB   (always free)
#   VCN + subnet + IGW   (always free)
#
# Prerequisites:
#   1. Create Oracle Cloud account → https://cloud.oracle.com/free
#   2. Install OCI CLI: brew install oci  (or pip install oci-cli)
#   3. Run: oci setup config  (generates ~/.oci/config)
#   4. Install Terraform: brew install terraform
#   5. Fill in terraform/terraform.tfvars (see variables.tf)
#   6. Run: cd terraform && terraform init && terraform apply
# ════════════════════════════════════════════════════════════════════

terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3"
}

provider "oci" {
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key_path = var.private_key_path
  region           = var.region
}

# ── Data sources ──────────────────────────────────────────────────
data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

# Ubuntu 22.04 ARM (aarch64) — latest minimal image
data "oci_core_images" "ubuntu_arm" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "22.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# ── Network ───────────────────────────────────────────────────────
resource "oci_core_vcn" "quantbot_vcn" {
  compartment_id = var.compartment_ocid
  cidr_block     = "10.0.0.0/16"
  display_name   = "quantbot-vcn"
  dns_label      = "quantbot"
}

resource "oci_core_internet_gateway" "igw" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.quantbot_vcn.id
  display_name   = "quantbot-igw"
  enabled        = true
}

resource "oci_core_route_table" "public_rt" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.quantbot_vcn.id
  display_name   = "quantbot-public-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_internet_gateway.igw.id
  }
}

resource "oci_core_security_list" "quantbot_sl" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.quantbot_vcn.id
  display_name   = "quantbot-security-list"

  # Allow all outbound (needed to reach Binance API + Telegram)
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
    stateless   = false
  }

  # SSH — your IP only
  ingress_security_rules {
    protocol  = "6"   # TCP
    source    = var.my_ip_cidr
    stateless = false
    tcp_options { min = 22; max = 22 }
  }

  # HTTP (Let's Encrypt challenge + redirect to HTTPS)
  ingress_security_rules {
    protocol  = "6"
    source    = "0.0.0.0/0"
    stateless = false
    tcp_options { min = 80; max = 80 }
  }

  # Dashboard — port 8888 (IP-only, no SSL required)
  # Lock this down to your IP only for security
  ingress_security_rules {
    protocol  = "6"
    source    = var.my_ip_cidr
    stateless = false
    tcp_options { min = 8888; max = 8888 }
  }
}

resource "oci_core_subnet" "public_subnet" {
  compartment_id    = var.compartment_ocid
  vcn_id            = oci_core_vcn.quantbot_vcn.id
  cidr_block        = "10.0.1.0/24"
  display_name      = "quantbot-public-subnet"
  dns_label         = "public"
  route_table_id    = oci_core_route_table.public_rt.id
  security_list_ids = [oci_core_security_list.quantbot_sl.id]
  prohibit_public_ip_on_vnic = false
}

# ── Compute — VM.Standard.A1.Flex (ARM, always free) ─────────────
resource "oci_core_instance" "quantbot_vm" {
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  display_name        = "quantbot-server"
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = 2      # 2 of 4 free OCPUs
    memory_in_gbs = 12     # 12 of 24 free GB
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu_arm.images[0].id
    boot_volume_size_in_gbs = 50   # free tier allows 50 GB
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.public_subnet.id
    assign_public_ip = true
    display_name     = "quantbot-vnic"
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key

    # Cloud-init: installs Docker, clones repo, starts services
    user_data = base64encode(<<-EOT
      #!/bin/bash
      set -e
      export DEBIAN_FRONTEND=noninteractive

      # Update
      apt-get update && apt-get upgrade -y

      # Docker
      curl -fsSL https://get.docker.com | sh
      usermod -aG docker ubuntu
      systemctl enable docker

      # Docker Compose v2
      apt-get install -y docker-compose-plugin
      ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

      # Git
      apt-get install -y git

      # Clone repo
      sudo -u ubuntu git clone ${var.repo_url} /home/ubuntu/quantbot
      chown -R ubuntu:ubuntu /home/ubuntu/quantbot

      # .env — written from Terraform variable (populated from GitHub secret)
      echo '${var.env_file_contents}' > /home/ubuntu/quantbot/.env
      chmod 600 /home/ubuntu/quantbot/.env
      chown ubuntu:ubuntu /home/ubuntu/quantbot/.env

      # nginx certs directory
      mkdir -p /home/ubuntu/quantbot/nginx/certs

      # Start all services
      cd /home/ubuntu/quantbot
      sudo -u ubuntu docker-compose up -d --build

      echo "QuantBot deployed at $(date)" >> /home/ubuntu/deploy.log
    EOT
    )
  }

  freeform_tags = { "project" = "quantbot" }
}

# ── Block volume removed ──────────────────────────────────────────
# Docker named volume 'quantbot_data' stores all state files on the
# boot volume (50 GB). A separate block volume is not needed and
# would consume free-tier quota without being used.
