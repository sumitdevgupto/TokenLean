"""Every middleware module is imported by the proxy.

A middleware module that nothing imports still reads like a finished stage, so a reader, or a
later change that wires it in, trusts code that no request has ever run. Several such
modules were deleted on 2026-10-01 and 2026-10-02 (a memory adapter that mixed tenants, an
agent runtime with hardcoded prices, a pgvector helper with injectable SQL); this keeps new
ones from accumulating unnoticed.
"""
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[3] / "src"


def _sources(src: Path):
    return {
        path: path.read_text(encoding="utf-8", errors="replace")
        for path in src.rglob("*.py")
        if "__pycache__" not in path.parts and not path.name.startswith("test_")
    }


def _unused_modules(src: Path):
    """Names of the modules in ``src/proxy/middleware`` that no other source file imports."""
    sources = _sources(src)
    unused = set()
    for module in sorted((src / "proxy" / "middleware").glob("*.py")):
        name = module.stem
        if name == "__init__":
            continue
        imported = re.compile(
            rf"(middleware\.{name}\b|from \.{name}\b|from (middleware|\.) import[^\n]*\b{name}\b)")
        if not any(imported.search(text) for path, text in sources.items() if path != module):
            unused.add(name)
    return unused


def test_every_middleware_module_is_imported_by_the_proxy():
    unused = _unused_modules(_SRC)
    assert not unused, (
        f"{sorted(unused)}: nothing under src/ imports these middleware modules. Wire them "
        "into the pipeline or delete them.")


class TestTheDetector:
    """The check above is only as good as the imports it recognises, so run it on small trees."""

    @staticmethod
    def _tree(tmp_path, files):
        for relative, text in files.items():
            path = tmp_path / "proxy" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return tmp_path

    def test_a_module_nothing_imports_is_reported(self, tmp_path):
        src = self._tree(tmp_path, {"middleware/lonely.py": "X = 1\n", "main.py": "import os\n"})
        assert _unused_modules(src) == {"lonely"}

    @pytest.mark.parametrize("importer, text", [
        ("main.py", "from middleware.used import Stage\n"),
        ("main.py", "import middleware.used\n"),
        ("main.py", "from middleware import langfuse, used\n"),
        ("middleware/other.py", "from .used import Stage\n"),
        ("middleware/other.py", "from . import used\n"),
    ])
    def test_each_way_of_importing_a_module_counts(self, tmp_path, importer, text):
        src = self._tree(tmp_path, {"middleware/used.py": "X = 1\n", importer: text})
        assert "used" not in _unused_modules(src)

    def test_importing_a_longer_name_does_not_count(self, tmp_path):
        src = self._tree(tmp_path, {
            "middleware/g1.py": "X = 1\n", "middleware/g10.py": "X = 1\n",
            "main.py": "from middleware.g10 import X\n"})
        assert _unused_modules(src) == {"g1"}

    def test_a_module_that_names_itself_is_still_unused(self, tmp_path):
        src = self._tree(tmp_path, {
            "middleware/alone.py": '"""See middleware.alone."""\nfrom middleware.alone import X\n'})
        assert _unused_modules(src) == {"alone"}

    def test_a_test_file_is_not_an_importer(self, tmp_path):
        src = self._tree(tmp_path, {
            "middleware/lonely.py": "X = 1\n", "test_lonely.py": "from middleware.lonely import X\n"})
        assert _unused_modules(src) == {"lonely"}
