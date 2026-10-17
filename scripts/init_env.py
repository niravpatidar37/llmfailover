"""Create the gitignored local .env with random admin tokens and a Grafana admin password.

Never prints the generated values. Re-running keeps existing values.
"""

from __future__ import annotations

import secrets
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"
KEYS = ["GATEWAY_ADMIN_TOKEN", "CHAOS_ADMIN_TOKEN", "GRAFANA_ADMIN_PASSWORD"]


def main() -> None:
    existing: dict[str, str] = {}
    if ENV.exists():
        for line in ENV.read_text("utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    for k in KEYS:
        existing.setdefault(k, secrets.token_urlsafe(24))
    existing.setdefault("PORTFOLIO_ENV_FILE", "F:/.secrets/portfolio.env")
    ENV.write_text("".join(f"{k}={v}\n" for k, v in existing.items()), encoding="utf-8")
    print(f"wrote {ENV} with keys: {', '.join(sorted(existing))}")


if __name__ == "__main__":
    main()
