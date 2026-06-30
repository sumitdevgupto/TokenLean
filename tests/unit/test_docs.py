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
    "developer-onboarding.md",
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

    @pytest.mark.parametrize("group", ["G19", "G20", "G21", "G22", "G23", "G24", "G25", "G27", "G28"])
    def test_readme_mentions_group(self, group):
        content = _read(README_PATH)
        assert group in content, f"README should document {group}"

    def test_readme_states_g26_reserved(self):
        content = _read(README_PATH).lower()
        assert "g26" in content and "reserved" in content, (
            "README should note G26 is the reserved slot"
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
