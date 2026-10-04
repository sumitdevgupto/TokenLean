"""
Guard: docs/oss-licenses.md lists exactly what src/proxy/requirements.in declares.

The doc's Core and Optional tables are the published licence inventory of the proxy's
direct dependencies, and nothing tied them to requirements.in: six direct dependencies
had gone missing from them and several version specs were stale while the doc still
said it listed every dependency. This test fails when the two files disagree on

  * which requirements exist, or which section one sits in (the doc's Optional table
    is the requirements.in block that starts at the "# Optional" comment),
  * a requirement's version spec or extras, or
  * a licence: the doc's SPDX column against the licence note that opens each
    requirements.in comment (the text before " — ").

It checks that the two records agree, not that a licence is right: that takes the
package's published metadata or its repo, and the doc's "License Verification Sources"
table says which was used where.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
REQUIREMENTS_IN = REPO_ROOT / "src" / "proxy" / "requirements.in"
LICENSES_DOC = REPO_ROOT / "docs" / "oss-licenses.md"

# The doc's two inventory tables, by heading, and the requirements.in section each mirrors.
DOC_TABLES = {"## Core Dependencies": "core", "## Optional Dependencies": "optional"}

_NAME = r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?"
_REQUIREMENT_LINE = re.compile(_NAME + r"\s*(?P<spec>[^#]*?)\s*(?:#\s*(?P<note>.*))?")
_PACKAGE_CELL = re.compile(_NAME)


def _canonical(name: str) -> str:
    """PEP 503 canonical form: case-insensitive, runs of -_. collapse to -."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirement(extras, spec: str) -> str:
    """`[Standard]` + `>= 0.30.0` -> `[standard]>=0.30.0`: what pip reads, case and spaces aside."""
    names = sorted(_canonical(e) for e in (extras or "").strip("[]").split(",") if e.strip())
    return (f"[{','.join(names)}]" if names else "") + re.sub(r"\s+", "", spec)


def _declared() -> dict:
    """{section: [(name, requirement, licence)]} from requirements.in. A comment line that
    starts with "Optional" opens the optional section; a line's licence is its comment's
    text up to the first dash separator."""
    sections = {"core": [], "optional": []}
    section = "core"
    for line in REQUIREMENTS_IN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            if line.lstrip("#").strip().lower().startswith("optional"):
                section = "optional"
            continue
        if not line or line.startswith("-"):
            continue
        m = _REQUIREMENT_LINE.fullmatch(line)
        assert m, f"cannot parse src/proxy/requirements.in line {line!r}"
        licence = re.split(r"\s[\N{EM DASH}\N{EN DASH}-]+\s", m.group("note") or "", maxsplit=1)[0].strip()
        sections[section].append(
            (_canonical(m.group("name")), _requirement(m.group("extras"), m.group("spec")), licence))
    return sections


def _documented() -> dict:
    """{section: [(name, requirement, licence)]} from the doc's Core and Optional tables
    (columns: Package | Version | SPDX License | ...)."""
    sections = {"core": [], "optional": []}
    seen, section = set(), None
    for line in LICENSES_DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            section = next((s for heading, s in DOC_TABLES.items() if line.startswith(heading)), None)
            seen.add(section)
            continue
        if section is None or not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`").strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Package" or not cells[0].strip("-: "):
            continue                                    # header row, |---| separator
        m = _PACKAGE_CELL.fullmatch(cells[0])
        assert m, f"cannot read the package cell {cells[0]!r} in docs/oss-licenses.md"
        sections[section].append(
            (_canonical(m.group("name")), _requirement(m.group("extras"), cells[1]), cells[2]))
    missing = set(DOC_TABLES.values()) - seen
    assert not missing, f"docs/oss-licenses.md has no {sorted(missing)} dependency table"
    return sections


def _by_name(sections: dict) -> dict:
    return {name: (requirement, licence)
            for rows in sections.values() for name, requirement, licence in rows}


def test_doc_lists_every_requirement_in_its_section_and_nothing_else():
    """A dependency added to requirements.in with no doc row (six had been), a row left
    behind after its requirement went, or a row in the wrong table."""
    declared, documented = _declared(), _documented()
    assert declared["core"], "parsed no requirements out of src/proxy/requirements.in"
    out_of_step = {}
    for section in DOC_TABLES.values():
        want = sorted(name for name, _, _ in declared[section])
        have = sorted(name for name, _, _ in documented[section])
        if have != want:
            out_of_step[section] = (
                f"missing {sorted(set(want) - set(have))}, "
                f"not in requirements.in {sorted(set(have) - set(want))}, "
                f"listed twice {sorted({n for n in have if have.count(n) > 1})}"
            )
    assert not out_of_step, (
        f"docs/oss-licenses.md tables are out of step with src/proxy/requirements.in: {out_of_step}"
    )


def test_doc_version_specs_match_requirements_in():
    """The doc said litellm >=1.40.0 for a requirement that reads >=1.95.0,<2.0.0."""
    declared, documented = _by_name(_declared()), _by_name(_documented())
    stale = {
        name: f"doc {documented[name][0]!r}, requirements.in {declared[name][0]!r}"
        for name in sorted(declared.keys() & documented.keys())
        if documented[name][0] != declared[name][0]
    }
    assert not stale, (
        f"docs/oss-licenses.md version specs differ from src/proxy/requirements.in: {stale}"
    )


def test_doc_licences_match_the_requirements_in_notes():
    """Each requirements.in line carries its SPDX licence; the doc publishes the same one."""
    declared, documented = _by_name(_declared()), _by_name(_documented())
    differ = {
        name: f"doc {documented[name][1]!r}, requirements.in {declared[name][1]!r}"
        for name in sorted(declared.keys() & documented.keys())
        if documented[name][1] != declared[name][1]
    }
    assert not differ, (
        f"docs/oss-licenses.md licences differ from the src/proxy/requirements.in notes: "
        f"{differ} — check the package metadata, then correct whichever side is wrong"
    )
