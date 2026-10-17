"""Create a tenant API key and print the updated TENANT_KEYS_JSON spec for Secret Manager.

    uv run python scripts/make_tenant_key.py acme --rpm 600 [--spec current.json] > tenant-keys.json

* stdout: the full spec JSON (hashes only) to upload with
  ``gcloud secrets versions add <prefix>-tenant-keys --data-file=tenant-keys.json``.
* stderr: the NEW plaintext key, shown once. Hand it to the tenant over a secure channel; it is not stored.
* ``--spec`` merges into an existing spec, keeping the tenant's previous hashes so the old key keeps working
  during rotation. Drop old hashes with ``--revoke-others`` once the tenant has switched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tenant")
    ap.add_argument("--rpm", type=int, default=600)
    ap.add_argument("--spec", type=Path, help="existing spec JSON to merge into")
    ap.add_argument("--revoke-others", action="store_true", help="keep only the new key for this tenant")
    args = ap.parse_args()
    if not TENANT_RE.match(args.tenant):
        sys.exit("tenant must match " + TENANT_RE.pattern)
    spec = json.loads(args.spec.read_text("utf-8")) if args.spec else {}
    key = "gwk_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    entry = spec.get(args.tenant, {"key_sha256": []})
    hashes = [] if args.revoke_others else list(entry.get("key_sha256", []))
    spec[args.tenant] = {"rpm": args.rpm, "key_sha256": [*hashes, digest]}
    print(json.dumps(spec, indent=2, sort_keys=True))
    print(f"new key for {args.tenant} (shown once, not stored): {key}", file=sys.stderr)


if __name__ == "__main__":
    main()
