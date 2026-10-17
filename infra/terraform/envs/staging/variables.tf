variable "project_id" {
  description = "GCP project id (set in terraform.tfvars or TF_VAR_project_id)."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
}

variable "image" {
  description = "Gateway image by digest (REGION-docker.pkg.dev/PROJECT/REPO/gateway@sha256:...)."
  type        = string
}

variable "deploy_service_account_email" {
  description = "GitHub Actions deploy service account (Workload Identity Federation)."
  type        = string
}

variable "min_instances" {
  description = "Minimum API instances."
  type        = number
}

variable "max_instances" {
  description = "Maximum API instances."
  type        = number
}

variable "redis_tier" {
  description = "Memorystore tier."
  type        = string
}

variable "public_invoker" {
  description = "Allow unauthenticated invocation (tenant keys authenticate at the app layer)."
  type        = bool
}

variable "project_spend_cap_usd" {
  description = "Monthly paid-LLM spend cap enforced by the gateway."
  type        = number
}

variable "deletion_protection" {
  description = "Protect stateful resources from destroy."
  type        = bool
}

variable "kill_switch" {
  description = "Disable all provider calls (LLM_KILL_SWITCH=1)."
  type        = bool
  default     = false
}

variable "enable_anthropic" {
  description = "Inject ANTHROPIC_API_KEY (secret must have a version first)."
  type        = bool
  default     = false
}

variable "enable_hf" {
  description = "Inject HF_TOKEN and HF_ENABLED=1 (account must be funded)."
  type        = bool
  default     = false
}
