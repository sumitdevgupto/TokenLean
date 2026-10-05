"""Unit tests for scripts/audit_licenses.py — the licence rule in THIRD_PARTY_LICENSES.md, as a
check: every pin of every lockfile is permissively licensed, or MPL-2.0 (an unmodified
transitive dependency). Offline: PyPI and the installed distributions are faked."""
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = ROOT / "scripts" / "audit_licenses.py"


def _load_script():
    """Import audit_licenses.py as a module (a standalone script, not a package member)."""
    spec = importlib.util.spec_from_file_location("audit_licenses", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def al():
    return _load_script()


def ev(expression="", license="", classifiers=()):
    """Licence evidence as the script reads it from metadata."""
    return {"expression": expression, "license": license,
            "classifiers": [f"License :: {c}" for c in classifiers]}


@pytest.mark.parametrize("evidence, verdict", [
    # An SPDX expression: every licence it names must be allowed.
    (ev("MIT"), "permissive"),
    (ev("Apache-2.0 AND BSD-2-Clause"), "permissive"),                       # prometheus-client
    (ev("Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause "
        "AND BSL-1.0 AND MIT"), "permissive"),                                 # torch 2.14.0
    (ev("Apache-2.0 AND CNRI-Python"), "permissive"),                          # regex
    (ev("MPL-2.0 AND (Apache-2.0 OR MIT)"), "mpl"),                            # orjson
    (ev("GPL-3.0-only"), "other"),
    (ev("LGPL-2.1-or-later"), "other"),
    (ev("AGPL-3.0-only"), "other"),
    (ev("SSPL-1.0"), "other"),
    (ev("BUSL-1.1"), "other"),
    (ev("LicenseRef-Proprietary"), "other"),
    (ev("MIT OR GPL-2.0-only"), "other"),          # a disallowed alternative takes an override
    # No expression: the License :: classifiers.
    (ev(classifiers=["OSI Approved :: MIT License"]), "permissive"),
    (ev(classifiers=["OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)"]), "mpl"),  # certifi
    (ev(classifiers=["OSI Approved :: GNU General Public License v3 (GPLv3)"]), "other"),
    (ev(license="Apache License", classifiers=["Other/Proprietary License"]), "other"),  # fastembed 0.8.0
    (ev(license="Dual License", classifiers=["OSI Approved :: BSD License",
                                             "OSI Approved :: Apache Software License"]), "permissive"),
    (ev(classifiers=["OSI Approved"]), "unknown"),  # says nothing about WHICH licence
    # Nothing else: the License field (an expression, or the first line of a licence text).
    (ev(license="MPL-2.0 AND MIT"), "mpl"),                                    # tqdm
    (ev(license="MIT License"), "permissive"),
    (ev(license="GNU GENERAL PUBLIC LICENSE"), "other"),
    (ev(license="Dual: BSD or GPL-2.0"), "other"),          # free text naming a disallowed one
    (ev(license="MIT AND Commons-Clause"), "other"),        # an expression in the License field
    (ev(license="UNKNOWN"), "unknown"),
    (ev(), "unknown"),
])
def test_classify(al, evidence, verdict):
    assert al.classify(evidence) == verdict


def test_an_override_fills_a_package_that_publishes_nothing(al):
    verdict, source = al.resolve("fsspec", "2026.7.0", ev())
    assert verdict == "permissive" and source.startswith("override")


def test_a_name_override_never_hides_published_metadata(al):
    # Were a later fsspec published as GPL, the name-only override must not mask it.
    assert al.resolve("fsspec", "2099.1.0", ev("GPL-3.0-only"))[0] == "other"


def test_a_version_override_corrects_wrong_metadata_for_that_release_only(al):
    wrong = ev(license="Apache License", classifiers=["Other/Proprietary License"])
    assert al.resolve("fastembed", "0.8.0", wrong)[0] == "permissive"
    assert al.resolve("fastembed", "0.9.0", wrong)[0] == "other"


def test_a_pin_outside_the_rule_fails_the_audit(al):
    assert al.audit([al.Row("left-pad", "1.0", "other", "metadata", "GPL-3.0-only")]) == 1


def test_a_licence_that_cannot_be_determined_fails_the_audit(al):
    assert al.audit([al.Row("mystery", "1.0", "unknown", "lookup failed", "")]) == 1


def test_permissive_and_mpl_pins_pass(al):
    assert al.audit([al.Row("certifi", "1", "mpl", "metadata", "MPL-2.0"),
                     al.Row("six", "1", "permissive", "metadata", "MIT")]) == 0


def test_a_pending_removal_is_reported_but_does_not_fail(al, capsys, monkeypatch):
    monkeypatch.setattr(al, "PENDING_REMOVAL", {"old-dep": "no licence; dropped at the next recompile"})
    assert al.audit([al.Row("old-dep", "1.0", "unknown", "metadata", "")]) == 0
    assert "old-dep" in capsys.readouterr().out
    # ...and only for the package it names.
    assert al.audit([al.Row("new-dep", "1.0", "unknown", "metadata", "")]) == 1


def test_every_pending_removal_is_still_marked_for_the_next_recompile(al):
    """An exception must not outlive its reason: when a package leaves requirements.in, its
    PENDING_REMOVAL entry must go too."""
    lines = {}
    for line in (ROOT / "src" / "proxy" / "requirements.in").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            lines[al.norm(re.split(r"[<>=!~;\[\s#]", line.strip(), maxsplit=1)[0])] = line
    for name in al.PENDING_REMOVAL:
        assert name in lines and "drop at the next recompile" in lines[name], name


def test_the_audit_covers_every_lockfile_the_compile_script_writes(al):
    script = (ROOT / "scripts" / "compile-requirements.sh").read_text(encoding="utf-8")
    image_dirs = re.search(r"for dir in ([^;]+); do", script).group(1).split()
    written = {ROOT / "src" / "proxy" / "requirements.txt", ROOT / "tests" / "requirements-test.txt"}
    written |= {ROOT / "src" / d / "requirements.txt" for d in image_dirs}
    assert set(al.REQ_FILES) == written


def test_parse_pins_reads_exact_pins_and_skips_the_rest(al):
    text = ("#\n#    pip-compile --output-file=requirements.txt requirements.in\n#\n"
            "uvicorn[standard]==0.52.1\n    # via -r requirements.in\n"
            "Jinja2==3.1.6 ; python_version >= '3.8'\n-r other.txt\n\n"
            "# The following packages are considered to be unsafe in a requirements file:\n# torch\n")
    assert al.parse_pins(text) == {"uvicorn": "0.52.1", "jinja2": "3.1.6"}


def test_main_audits_each_pin_once_against_its_release(al, tmp_path, monkeypatch):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("alpha==1.0\nbeta==2.0\n", encoding="utf-8")
    second.write_text("alpha==1.0\n", encoding="utf-8")
    monkeypatch.setattr(al, "REQ_FILES", [first, second])
    monkeypatch.setattr(al, "installed_distributions", lambda: {})
    asked = []

    def fake_fetch(name, version):
        asked.append((name, version))
        return {"license_expression": "MIT" if name == "alpha" else "GPL-3.0-only",
                "license": "", "classifiers": []}

    monkeypatch.setattr(al, "fetch", fake_fetch)
    assert al.main([]) == 1                      # beta is GPL
    assert sorted(asked) == [("alpha", "1.0"), ("beta", "2.0")]


class _Meta(dict):
    """importlib.metadata's message object: .get and .get_all."""
    def get_all(self, key):
        return []


def _installed(version, expression):
    dist = type("Dist", (), {})()
    dist.version, dist.metadata = version, _Meta({"License-Expression": expression})
    return dist


@pytest.fixture
def one_pin(al, tmp_path, monkeypatch):
    lockfile = tmp_path / "a.txt"
    lockfile.write_text("alpha==1.0\n", encoding="utf-8")
    monkeypatch.setattr(al, "REQ_FILES", [lockfile])
    return monkeypatch


def test_the_installed_pinned_version_is_read_locally(al, one_pin):
    one_pin.setattr(al, "installed_distributions", lambda: {"alpha": _installed("1.0", "MIT")})
    one_pin.setattr(al, "fetch", lambda name, version: pytest.fail("fetched an installed release"))
    assert al.main([]) == 0


def test_an_installed_different_version_is_not_taken_for_the_pin(al, one_pin):
    # alpha 0.9 is installed under GPL; the pin is 1.0, published as MIT.
    one_pin.setattr(al, "installed_distributions", lambda: {"alpha": _installed("0.9", "GPL-3.0-only")})
    one_pin.setattr(al, "fetch", lambda name, version: {"license_expression": "MIT"})
    assert al.main([]) == 0


def test_offline_a_pin_that_is_not_installed_is_undetermined(al, one_pin):
    one_pin.setattr(al, "installed_distributions", lambda: {})
    assert al.main(["--offline"]) == 1
