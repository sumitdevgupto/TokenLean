"""Unit tests for per-tenant GCS bucket naming + reverse-derivation (data-safety).

The bucket name deterministically encodes the tenant; the webhook reverse-derives it via
registry lookup. These tests lock the round-trip, the GCS naming constraints, and the
collision guard so a future refactor that breaks isolation fails here.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import re
import pytest

from tenancy.context import (
    tenant_to_bucket,
    bucket_to_tenant,
    validate_registry_unique,
    TenantContext,
)

_GCS_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_CANONICAL = ["NOVA-STG-01", "SHOP-STG-01", "BUIL-STG-01", "default", "ACME-PRD-01"]


class TestBucketNaming:
    @pytest.mark.parametrize("tenant", _CANONICAL + ["foo_bar", "x", "  spaced  "])
    def test_output_satisfies_gcs_constraints(self, tenant):
        name = tenant_to_bucket(tenant)
        assert 3 <= len(name) <= 63
        assert _GCS_NAME_RE.match(name), f"{name!r} violates GCS bucket naming"
        assert "_" not in name and name == name.lower()

    def test_nova_lowercases_cleanly(self):
        assert tenant_to_bucket("NOVA-STG-01") == "token-opt-docs-nova-stg-01"

    def test_default_is_explicit(self):
        assert tenant_to_bucket("default") == "token-opt-docs-default"
        assert tenant_to_bucket("") == "token-opt-docs-default"

    def test_long_tenant_truncates_slug_not_prefix(self):
        name = tenant_to_bucket("A" * 200)
        assert name.startswith("token-opt-docs-")
        assert len(name) <= 63


class TestReverseDerivation:
    @pytest.mark.parametrize("tenant", _CANONICAL + ["foo_bar"])
    def test_round_trip(self, tenant):
        registry = _CANONICAL + ["foo_bar"]
        assert bucket_to_tenant(tenant_to_bucket(tenant), registry) == tenant

    def test_unknown_bucket_is_none(self):
        assert bucket_to_tenant("token-opt-docs-evil", _CANONICAL) is None

    def test_empty_bucket_is_none(self):
        assert bucket_to_tenant("", _CANONICAL) is None

    def test_returns_raw_registry_id_not_slug(self):
        # foo_bar → bucket token-opt-docs-foo-bar, but reverse gives back the RAW id.
        assert bucket_to_tenant("token-opt-docs-foo-bar", ["foo_bar"]) == "foo_bar"


class TestRegistryUnique:
    def test_canonical_ids_are_unique(self):
        validate_registry_unique(_CANONICAL)  # must not raise

    def test_colliding_ids_raise(self):
        # NOVA_STG and NOVA-STG both lowercase to the same bucket slug.
        with pytest.raises(ValueError):
            validate_registry_unique(["NOVA_STG", "NOVA-STG"])


class TestBucketCollectionAlignment:
    """The write bucket and the read collection must stay derived from the same id."""

    @pytest.mark.parametrize("tenant", _CANONICAL)
    def test_bucket_and_collection_share_tenant(self, tenant):
        bucket = tenant_to_bucket(tenant)
        collection = TenantContext.for_tenant(tenant).qdrant_collection
        assert collection.startswith("rag_")
        # The bucket must reverse-derive to the same tenant the collection was built for.
        assert bucket_to_tenant(bucket, [tenant]) == tenant
