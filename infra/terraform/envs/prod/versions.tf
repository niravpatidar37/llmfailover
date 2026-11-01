terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.9.0"
    }
  }

  # Remote state in GCS. Bucket and prefix come from -backend-config (docs/RUNBOOK.md), e.g.
  #   terraform init -backend-config="bucket=<PROJECT>-tfstate" -backend-config="prefix=llm-gateway/prod"
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
