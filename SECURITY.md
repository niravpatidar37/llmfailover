# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub:
[Security → Report a vulnerability](https://github.com/niravpatidar37/llmfailover/security/advisories/new).
Do not open a public issue for a security problem.

Include what you found, how to reproduce it, and the impact you expect. You should get a first reply
within 7 days. This is a portfolio MVP maintained by one person, so there is no bounty and no SLA beyond that.

## Scope

In scope: the gateway (`src/gateway/`), its auth, tenant isolation, idempotency, spend cap, redaction and
prod guards, the Dockerfile, the Terraform in `infra/terraform/` and the workflows in `.github/workflows/`.

Out of scope: the chaos proxy when run outside a local dev machine (it is a dev tool that refuses to start
with `ENV=prod`), and the residual risks already listed in the README's Security model section.

## Supported versions

Only `main` is supported.
