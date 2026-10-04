"""Every subprocess call under src/ and scripts/ runs its program by an absolute path.

A call like subprocess.run(["git", ...]) has the system search PATH for "git" when it runs
(bandit B607), and a missing tool surfaces as a bare FileNotFoundError. The tools are now looked
up with shutil.which, run by the path found, and a missing one is named. The lookup still uses
the caller's PATH, as before: these are operator and CI scripts, run with the operator's own,
so a fixed path such as /usr/bin/git would only break machines whose tools live elsewhere.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_CALLS = {"run", "call", "check_call", "check_output", "Popen"}


def _bare_name_calls(source: str):
    """Lines of subprocess calls whose program is a literal name with no directory part."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CALLS and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess" and node.args):
            continue
        argv = node.args[0]
        first = argv.elts[0] if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts else argv
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.strip():
            program = first.value.split()[0]
            if not (program.startswith("/") or program[1:3] in (":\\", ":/")):
                found.append(node.lineno)
    return found


@pytest.mark.parametrize("snippet,bare", [
    ('subprocess.run(["git", "status"])', True),
    ('subprocess.check_output(("bash", "-c", "true"))', True),
    ('subprocess.run("git status", shell=True)', True),
    ('subprocess.run([git, "status"])', False),
    ('subprocess.run(["/usr/bin/git", "status"])', False),
    ('subprocess.run([sys.executable, "-m", "pip"])', False),
], ids=["list", "tuple", "string", "resolved", "absolute", "interpreter"])
def test_the_check_tells_a_bare_program_name_from_a_resolved_one(snippet, bare):
    assert bool(_bare_name_calls(snippet)) is bare


def test_the_ci_template_check_names_a_missing_git(monkeypatch):
    import importlib.util
    import shutil
    spec = importlib.util.spec_from_file_location(
        "pr_diff_token_check", ROOT / "scripts" / "ci" / "pr-diff-token-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="git is not on PATH"):
        module.get_changed_templates("main", "HEAD")


def test_no_subprocess_call_runs_a_program_by_bare_name():
    offenders = [f"{path.relative_to(ROOT).as_posix()}:{line}"
                 for d in ("src", "scripts") for path in sorted((ROOT / d).rglob("*.py"))
                 if "node_modules" not in path.parts
                 for line in _bare_name_calls(path.read_text(encoding="utf-8"))]
    assert not offenders, offenders
