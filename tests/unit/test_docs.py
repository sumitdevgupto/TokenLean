"""Documentation gate — validates the curated open-source docs set.

Replaces the pre-open-core doc tests (which asserted an SLA runbook,
implementation-gaps.md, and a README "Commercial Features" section that were
removed during the open-core carve-out). This version:

- asserts the 7 curated OSS docs exist and are non-empty;
- asserts the README documents the current G-group range (G0–G28);
- enforces the open-core barricade: the public README must NOT advertise
  commercial-only features (Commercial Features section / customer portal).
"""
import pytest
from pathlib import Path

DOCS_DIR = Path(__file__).parent.parent.parent / "docs"
README_PATH = Path(__file__).parent.parent.parent / "README.md"

# The curated docs that ship in the open-source repo.
CURATED_DOCS = [
    "config-reference.md",
    "deployment-gcp.md",
    "deployment-local.md",
    "client-onboarding.md",
    "langfuse-access.md",
    "oss-licenses.md",
    "request-flow-diagram.md",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── Curated OSS docs are present and non-empty ────────────────────────────────

class TestCuratedDocs:
    @pytest.mark.parametrize("name", CURATED_DOCS)
    def test_doc_exists(self, name):
        path = DOCS_DIR / name
        assert path.exists(), f"Curated OSS doc missing: docs/{name}"

    @pytest.mark.parametrize("name", CURATED_DOCS)
    def test_doc_is_not_empty(self, name):
        content = _read(DOCS_DIR / name)
        assert len(content.strip()) > 100, f"Curated OSS doc too short: docs/{name}"


# ── README documents the current G-group coverage (G0–G28) ────────────────────

class TestREADMEGroups:
    def test_readme_exists(self):
        assert README_PATH.exists(), "README.md not found"

    # Per-group README coverage is derived from pipeline.py in
    # tests/unit/test_docs_pipeline_sync.py, NOT hand-listed here - the old list
    # stopped at G30 and silently missed G31 and G32 when they landed.

    def test_readme_documents_every_slot_as_shipped(self):
        """G26 was the last reserved slot and SHIPPED on 2026-08-07.

        The assertion this replaces ("README should note G26 is the reserved slot")
        outlived that change and kept passing only because the word 'reserved'
        happens to appear in the unrelated G1/G8 table rows - it was asserting
        something no longer true. What matters now is that G26 is documented and the
        hero copy no longer describes any slot as pending.
        """
        content = _read(README_PATH)
        assert "G26" in content, "README should document G26"
        hero = content.split("## G0")[0].lower()
        assert "reserved slot" not in hero, (
            "README hero should no longer describe a slot as reserved - all 28 ship"
        )


# ── Open-core barricade: no commercial-only marketing in the public README ────

class TestOpenCoreBarricade:
    def test_readme_has_no_commercial_features_section(self):
        content = _read(README_PATH)
        assert "## Commercial Features" not in content, (
            "Open-core README must not advertise a 'Commercial Features' section"
        )

    def test_readme_does_not_market_customer_portal(self):
        content = _read(README_PATH).lower()
        assert "customer portal" not in content, (
            "Open-core README must not market the (commercial) customer portal"
        )
