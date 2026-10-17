module "gateway" {
  source = "../../modules/gateway"

  project_id                   = var.project_id
  region                       = var.region
  env                          = "staging"
  image                        = var.image
  deploy_service_account_email = var.deploy_service_account_email

  min_instances         = var.min_instances
  max_instances         = var.max_instances
  redis_tier            = var.redis_tier
  public_invoker        = var.public_invoker
  project_spend_cap_usd = var.project_spend_cap_usd
  deletion_protection   = var.deletion_protection
  kill_switch           = var.kill_switch
  enable_anthropic      = var.enable_anthropic
  enable_hf             = var.enable_hf
}
