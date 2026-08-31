"""Docs must not drift from `pipeline.py`.

The pipeline order is documented in three places that a human maintains by hand — the
README's mermaid flowchart, `docs/request-flow-diagram.md`, and `AGENTS.md` — and both
docs already declare `pipeline.py` the source of truth. Nothing enforced that, so the
hand-maintained copies drifted: when G31 and G32 landed, several surfaces kept listing
the old group set, and `test_docs.py`'s hand-written `G19…G30` range simply stopped being
extended. (A sibling test asserting "G26 is the reserved slot" also outlived G26 shipping
and kept passing on an unrelated occurrence of the word "reserved".)

So these tests **derive** what to expect by parsing `pipeline.py` rather than restating
it. Adding a group to the pipeline and forgetting a doc now fails here, which is the only
way two views of one pipeline stay safe to keep.

Deliberately structural, not stylistic: they assert a group is PRESENT and that the
response-side ORDER matches, never how a sentence is worded.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
PIPELINE = ROOT / "src" / "proxy" / "middleware" / "pipeline.py"
README = ROOT / "README.md"
FLOW_DOC = ROOT / "docs" / "request-flow-diagram.md"
AGENTS = ROOT / "AGENTS.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _read_or_skip(p: Path) -> str:
    """Read a doc, or skip when it is not part of this tree.

    `AGENTS.md` is COMMERCIAL (gitignored), so it is absent from the OSS tree the
    open-core gate builds with `git archive HEAD`. A public test that hard-requires it
    is a barricade violation — it makes the OSS checkout fail on a file it is not
    entitled to have. Skipping keeps the guard fully effective in the full tree, where
    the file exists and drift actually happens.
    """
    if not p.exists():
        pytest.skip(f"{p.name} is not present in this tree (OSS checkout) — nothing to check")
    return p.read_text(encoding="utf-8")


# ── derive the truth from pipeline.py ─────────────────────────────────────────

def _registered_groups() -> set:
    """Every `self.gNN = GNNSomething()` in OptimisationPipeline.__init__."""
    return {f"G{m}" for m in re.findall(r"^\s+self\.g(\d{2})\s*=\s*G\d+", _read(PIPELINE), re.M)}


def _response_chain() -> list:
    """Group ids in response-chain order, from the `("GNN-name", lambda …)` list."""
    src = _read(PIPELINE)
    start = src.index("async def process_response")
    body = src[start:src.index("# Stage 5b", start)]
    return [f"G{n}" for n in re.findall(r'\("G(\d{2})[-\w]*",\s*lambda', body)]


def _mentions(text: str, group: str) -> bool:
    """True if `text` documents `group` in either spelling.

    `pipeline.py` zero-pads its attributes (`self.g02`) while the docs write the short
    form (`**G2**`) for single digits - both are current and neither is wrong, so the
    guard normalises instead of forcing a cosmetic rewrite across three files.
    """
    n = int(group[1:])
    # The trailing lookahead is what stops `G2` matching `G20`/`G22`/`G25`.
    return re.search(f"G0*{n}(?![0-9])", text) is not None


def _readme_mermaid() -> str:
    m = re.search(r"```mermaid\n(.*?)```", _read(README), re.S)
    assert m, "README no longer contains a mermaid block — update this test with it"
    return m.group(1)


# ── the guards ────────────────────────────────────────────────────────────────

class TestPipelineIsDiscoverable:
    """If these fail the parsing above broke, and every test below is meaningless."""

    def test_groups_found(self):
        groups = _registered_groups()
        assert len(groups) >= 30, f"only parsed {len(groups)} groups from pipeline.py: {sorted(groups)}"
        assert {"G29", "G30", "G31", "G32"} <= groups

    def test_response_chain_found(self):
        chain = _response_chain()
        assert len(chain) >= 6, f"parsed too few response stages: {chain}"
        assert chain[0] == "G29", f"expected G29 first in the response chain, got {chain}"


class TestDocsCoverEveryGroup:
    """Every group wired into the pipeline must appear in each doc surface."""

    @pytest.mark.parametrize("doc", ["README.md", "docs/request-flow-diagram.md", "AGENTS.md"])
    def test_every_registered_group_is_documented(self, doc):
        content = _read_or_skip(ROOT / doc)
        missing = sorted(g for g in _registered_groups() if not _mentions(content, g))
        assert not missing, (
            f"{doc} does not mention {missing}. A group wired into pipeline.py but absent "
            f"from the docs is exactly the drift this test exists to stop."
        )


class TestResponseChainOrderMatchesDocs:
    """The response-side ORDER is load-bearing (G32 before the auto-executing groups),
    so the docs must not show a stale sequence — a diagram that contradicts the code is
    read as the contract."""

    def _assert_order(self, text: str, where: str):
        chain = _response_chain()
        positions = {}
        for g in chain:
            idx = text.find(g)
            assert idx != -1, f"{where} is missing {g} from the response chain"
            positions[g] = idx
        ordered = sorted(chain, key=lambda g: positions[g])
        assert ordered == chain, (
            f"{where} lists the response chain as {ordered} but pipeline.py runs {chain}"
        )

    def test_readme_mermaid_response_node_order(self):
        node = _readme_mermaid()
        start = node.index("Response-side")
        self._assert_order(node[start:], "README mermaid Resp2 node")

    def test_flow_doc_canonical_order_line(self):
        content = _read(FLOW_DOC)
        line = next((l for l in content.splitlines()
                     if l.startswith("`G29(resp)") and "G5(store)" in l), None)
        assert line, "docs/request-flow-diagram.md lost its canonical response-order line"
        self._assert_order(line, "request-flow-diagram canonical response order")

    def test_agents_md_response_pipeline_line(self):
        content = _read_or_skip(AGENTS)
        line = next((l for l in content.splitlines()
                     if "G29(resp)" in l and "G18 Observe" in l), None)
        assert line, "AGENTS.md lost its response-pipeline line"
        self._assert_order(line, "AGENTS.md response pipeline")


class TestSafetyGroupsDocumentedAsNonBypassable:
    """A reader must be able to tell the trust & safety groups from the savings ones —
    they are excluded from the published savings headline, so conflating them misstates
    the number."""

    @pytest.mark.parametrize("group", ["G29", "G30", "G31", "G32"])
    def test_safety_group_named_in_readme_group_table(self, group):
        rows = [l for l in _read(README).splitlines() if l.startswith(f"| **{group}** |")]
        assert len(rows) == 1, f"README group table should have exactly one {group} row"
        assert "trust & safety" in rows[0].lower(), (
            f"README's {group} row must mark it as trust & safety, not a savings technique"
        )
        # Savings column must be "—": a safety control contributes no token savings.
        assert "| — |" in rows[0], f"{group} must show no savings figure"
