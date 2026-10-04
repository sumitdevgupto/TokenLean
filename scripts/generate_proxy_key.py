"""Generate proxy API keys with tenant metadata for local development.

Usage:
    python scripts/generate_proxy_key.py --tenant NOVA-STG-01 --tier enterprise
    python scripts/generate_proxy_key.py --tenant SHOP-STG-01 --tier free

Outputs:
    - Appends to config/local-keys.json (format: {hash: {tenant_id, tier}})
    - Prints export statement for the plaintext key
"""
from __future__ import annotations  # PEP 563: lazy annotations so tuple[...]/list[...]
                                    # subscripts don't evaluate at import on Python 3.7/3.8
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def generate_key(tenant_id: str, tier: str = "free", admin: bool = False) -> tuple[str, str, dict]:
    """Generate a new proxy key and its metadata.

    Returns:
        Tuple of (plaintext_key, key_hash, metadata_dict)
    """
    # Generate a random 48-char hex string for the key suffix
    random_suffix = os.urandom(24).hex()
    key = f"tok-{tenant_id}-{random_suffix}"
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    metadata = {
        "tenant_id": tenant_id,
        "tier": tier,
        "created": datetime.now().isoformat()
    }
    # Admin/impersonation scope — may assume another tenant via X-Tenant-ID and
    # call the cross-tenant admin/GDPR endpoints. Only for operator/benchmark keys.
    if admin:
        metadata["admin"] = True
    return key, key_hash, metadata


def main():
    parser = argparse.ArgumentParser(
        description="Generate a proxy API key with tenant metadata"
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help="Tenant ID (e.g., NOVA-STG-01, SHOP-STG-01, BUIL-STG-01)"
    )
    parser.add_argument(
        "--tier",
        default="free",
        choices=["free", "enterprise"],
        help="Pricing tier for this tenant (default: free)"
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Grant the admin/impersonation scope (X-Tenant-ID + cross-tenant admin endpoints). "
             "Use only for operator/benchmark/DS16 keys."
    )
    parser.add_argument(
        "--output-dir",
        default="config",
        help="Directory containing local-keys.json (default: config)"
    )
    args = parser.parse_args()

    # Generate the key
    key, key_hash, metadata = generate_key(args.tenant, args.tier, admin=args.admin)
    
    # Load the existing store. One that exists but cannot be read is NEVER treated as
    # empty: writing it back would keep only the new key and wipe every other key (the
    # file is also synced to production Postgres). utf-8-sig tolerates the byte-order
    # mark a Windows editor may add.
    keys_file = Path(args.output_dir) / "local-keys.json"
    existing = {}
    if keys_file.exists():
        try:
            existing = json.loads(keys_file.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            print(f"Error: could not read {keys_file} ({e}). Refusing to overwrite it — "
                  "fix or move the file, then retry.", file=sys.stderr)
            sys.exit(1)
        if not isinstance(existing, dict):
            print(f"Error: {keys_file} does not hold a JSON object. Refusing to overwrite it.",
                  file=sys.stderr)
            sys.exit(1)

    # Add new key (format: {hash: {tenant_id, tier}})
    existing[key_hash] = metadata

    # Write through a temp file + rename, so a crash mid-write never leaves a truncated store.
    tmp_file = keys_file.with_name(keys_file.name + ".tmp")
    try:
        keys_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        os.replace(tmp_file, keys_file)
    except OSError as e:
        tmp_file.unlink(missing_ok=True)
        print(f"Error: Could not write keys file: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Output env export for the tenant
    env_var = f"ROI_PROXY_API_KEY_{args.tenant.upper().replace('-', '_')}"
    
    print(f"# Generated key for tenant: {args.tenant} (tier: {args.tier})")
    print(f"# Stored hash in: {keys_file}")
    print()
    print("# Linux/Mac:")
    print(f"export {env_var}={key}")
    print()
    print("# Windows PowerShell:")
    print(f"$env:{env_var} = '{key}'")
    print()
    print("# Windows CMD:")
    print(f"set {env_var}={key}")


if __name__ == "__main__":
    main()
