variable "project_id" {
  description = "GCP project id."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Memorystore, Artifact Registry and the subnet."
  type        = string
}

variable "env" {
  description = "Deployment environment (staging or prod). Passed to the app as ENV."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.env)
    error_message = "env must be staging or prod."
  }
}

variable "name" {
  description = "Base name for resources."
  type        = string
  default     = "llm-gateway"
}

variable "image" {
  description = "Initial container image BY DIGEST (REGION-docker.pkg.dev/PROJECT/REPO/gateway@sha256:...). CD updates it afterwards; Terraform ignores later image changes."
  type        = string
  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "image must be pinned by digest (@sha256:...), never by tag."
  }
}

variable "deploy_service_account_email" {
  description = "Service account used by GitHub Actions through Workload Identity Federation (created in the one-time bootstrap)."
  type        = string
}

# ---- scaling and sizing --------------------------------------------------------------------------------
variable "min_instances" {
  description = "Minimum API instances (0 = scale to zero)."
  type        = number
}

variable "max_instances" {
  description = "Maximum API instances. Values > 1 rely on the Redis-shared breaker (tests/test_multi_instance.py)."
  type        = number
  validation {
    condition     = var.max_instances >= 1 && var.max_instances <= 100
    error_message = "max_instances must be between 1 and 100."
  }
}

variable "concurrency" {
  description = "Max concurrent requests per API instance (requests mostly wait on provider I/O)."
  type        = number
  default     = 40
}

variable "api_cpu" {
  description = "vCPU limit for the gateway container."
  type        = string
  default     = "1"
}

variable "api_memory" {
  description = "Memory limit for the gateway container."
  type        = string
  default     = "1Gi"
}

variable "request_timeout_s" {
  description = "Cloud Run request timeout in seconds."
  type        = number
  default     = 60
}

variable "worker_instances" {
  description = "Instances in the deferrable-queue worker pool (worker pools use manual scaling)."
  type        = number
  default     = 1
}

variable "ingress" {
  description = "Cloud Run ingress setting for the API service."
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
  validation {
    condition     = contains(["INGRESS_TRAFFIC_ALL", "INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"], var.ingress)
    error_message = "ingress must be a Cloud Run v2 ingress value."
  }
}

variable "public_invoker" {
  description = "Allow unauthenticated invocation (callers then authenticate with tenant API keys at the app layer, as OpenAI-compatible clients expect). false = Cloud Run IAM required."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the service, worker pool, job and Redis from terraform destroy."
  type        = bool
  default     = true
}

# ---- Redis ---------------------------------------------------------------------------------------------
variable "redis_tier" {
  description = "Memorystore tier: BASIC (no replica) or STANDARD_HA (replica + automatic failover)."
  type        = string
  validation {
    condition     = contains(["BASIC", "STANDARD_HA"], var.redis_tier)
    error_message = "redis_tier must be BASIC or STANDARD_HA."
  }
}

variable "redis_memory_gb" {
  description = "Memorystore capacity in GB."
  type        = number
  default     = 1
}

variable "redis_version" {
  description = "Memorystore Redis version."
  type        = string
  default     = "REDIS_7_2"
}

variable "subnet_cidr" {
  description = "Subnet for Cloud Run Direct VPC egress (/26 or larger recommended)."
  type        = string
  default     = "10.10.0.0/24"
}

# ---- app -----------------------------------------------------------------------------------------------
variable "project_spend_cap_usd" {
  description = "Monthly hard cap on paid LLM spend, enforced atomically in Redis by the gateway."
  type        = number
}

variable "kill_switch" {
  description = "LLM_KILL_SWITCH: true disables every provider call (a new revision is rolled out). The faster path is PUT /admin/kill-switch."
  type        = bool
  default     = false
}

variable "enable_anthropic" {
  description = "Inject ANTHROPIC_API_KEY. Only set true AFTER the secret has a version and the model id in config/gateway.prod.yaml has been verified."
  type        = bool
  default     = false
}

variable "enable_hf" {
  description = "Inject HF_TOKEN and set HF_ENABLED=1. Only once the HF account is funded (it returned HTTP 402)."
  type        = bool
  default     = false
}

variable "gmp_sidecar_image" {
  description = "Google Cloud Managed Service for Prometheus sidecar for Cloud Run (documented version)."
  type        = string
  default     = "us-docker.pkg.dev/cloud-ops-agents-artifacts/cloud-run-gmp-sidecar/cloud-run-gmp-sidecar:1.2.0"
}

variable "labels" {
  description = "Extra labels for every labelled resource."
  type        = map(string)
  default     = {}
}
