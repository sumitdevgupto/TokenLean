"""Generate proxy API keys with tenant metadata for local development.

Usage:
    python scripts/generate_proxy_key.py --tenant nova-med --tier pro
    python scripts/generate_proxy_key.py --tenant shop-bot --tier basic
    python scripts/generate_proxy_key.py --tenant build-co --tier enterprise

Outputs:
    - Appends to config/local-keys.json (format: {hash: {tenant_id, tier}})
    - Prints export statement for the plaintext key
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def generate_key(tenant_id: str, tier: str = "basic", admin: bool = False) -> tuple[str, str, dict]:
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
        help="Tenant ID (e.g., nova-med, shop-bot, build-co)"
    )
    parser.add_argument(
        "--tier",
        default="basic",
        choices=["basic", "pro", "enterprise"],
        help="Pricing tier for this tenant (default: basic)"
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
    
    # Load existing keys file or create new one
    keys_file = Path(args.output_dir) / "local-keys.json"
    try:
        if keys_file.exists():
            with open(keys_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        else:
            existing = {}
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not read existing keys file: {e}", file=sys.stderr)
        existing = {}
    
    # Add new key (format: {hash: {tenant_id, tier}})
    existing[key_hash] = metadata
    
    # Write back
    try:
        keys_file.parent.mkdir(parents=True, exist_ok=True)
        with open(keys_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except IOError as e:
        print(f"Error: Could not write keys file: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Output env export for the tenant
    env_var = f"ROI_PROXY_API_KEY_{args.tenant.upper().replace('-', '_')}"
    
    print(f"# Generated key for tenant: {args.tenant} (tier: {args.tier})")
    print(f"# Stored hash in: {keys_file}")
    print()
    print(f"# Linux/Mac:")
    print(f"export {env_var}={key}")
    print()
    print(f"# Windows PowerShell:")
    print(f"$env:{env_var} = '{key}'")
    print()
    print(f"# Windows CMD:")
    print(f"set {env_var}={key}")


if __name__ == "__main__":
    main()
