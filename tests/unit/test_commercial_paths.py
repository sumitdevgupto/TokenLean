"""Unit tests for scripts/ci/commercial_paths.py — the open-core split's commercial paths,
read from the commercial sections of .gitignore so every check shares one list."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = ROOT / "scripts" / "ci" / "commercial_paths.py"


def _load_script():
    """Import commercial_paths.py as a module (it's a standalone script, not a package
    member) so its functions can be exercised directly."""
    spec = importlib.util.spec_from_file_location("commercial_paths", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cp():
    return _load_script()


GITIGNORE = (
    "# ─── Build ──────────────────\n"
    "__pycache__/\n"
    "\n"
    "# ─── Commercial / release tooling (private) ──────────────────\n"
    "scripts/paid-gate.sh\n"
    "\n"
    "# ─── Commercial layer (private — open-core split) ──────────────────\n"
    "# what this block is for\n"
    "src/proxy/api/paid.py\n"
    "src/proxy/paid_dir/\n"
    "tests/test_paid.py\n"
    "# ─── Editors ──────────────────\n"
    ".idea/\n"
)
COMMERCIAL = ["scripts/paid-gate.sh", "src/proxy/api/paid.py", "src/proxy/paid_dir/",
              "tests/test_paid.py"]


def _real_patterns(cp):
    return cp.commercial_patterns((ROOT / ".gitignore").read_text(encoding="utf-8"))


class TestCommercialPatterns:
    def test_every_commercial_section_and_nothing_else(self, cp):
        assert cp.commercial_patterns(GITIGNORE) == COMMERCIAL

    def test_no_commercial_paths_is_an_error_not_an_empty_list(self, cp):
        # An empty list would let every check pass, so a renamed header must fail loudly.
        with pytest.raises(ValueError):
            cp.commercial_patterns("# ─── Build ───\n__pycache__/\n"
                                   "# ─── Commercial layer ───\n# nothing yet\n")

    def test_the_public_gitignore_still_has_both_commercial_sections(self, cp):
        # Renaming either header would silently drop that section's paths from every check.
        sections = cp.commercial_sections((ROOT / ".gitignore").read_text(encoding="utf-8"))
        assert len(sections) >= 2 and all(sections.values())


DOCKERIGNORE = (
    "# the commercial modules\n"
    "api/paid.py\n"
    "paid_dir/\n"
    "/rooted.py\n"
    "*.pyc\n"
    "**/cache\n"
    "keep/\n"
    "!keep/this.py\n"
    "#notes.txt\n"
    "v?.txt\n"
    "lit\\*.txt\n"
)


class TestDockerignoreExcludes:
    @pytest.mark.parametrize("path, excluded", [
        ("api/paid.py", True),
        ("api/free.py", False),
        ("api/paid.py.bak", False),     # a pattern is the whole path, not a prefix
        ("api/paid_py", False),         # a dot is a dot
        ("paid_dir", True),
        ("paid_dir/sub/x.py", True),    # everything under an excluded directory
        ("rooted.py", True),            # a leading slash means the context root
        ("top.pyc", True),
        ("api/deep.pyc", False),        # Docker anchors patterns at the context root
        ("a/b/cache", True),            # ** crosses directories ...
        ("cache", True),                # ... including none
        ("keep/other.py", True),
        ("keep/this.py", False),        # a later ! pattern lets a path back in
        ("#notes.txt", False),          # a line starting with # is a comment
        ("v1.txt", True),
        ("v/.txt", False),              # ? is one character, never a slash
        ("lit*.txt", True),
        ("litX.txt", False),            # a backslash makes the next character literal
    ])
    def test_follows_docker_matching(self, cp, path, excluded):
        assert cp.dockerignore_excludes(cp.parse_dockerignore(DOCKERIGNORE), path) is excluded


def _context(root, dockerignore):
    (root / "src" / "proxy").mkdir(parents=True)
    (root / "src" / "proxy" / ".dockerignore").write_text(dockerignore, encoding="utf-8")
    return root


class TestNotDockerignored:
    def test_lists_the_commercial_paths_under_the_context_that_reach_the_image(self, cp, tmp_path):
        root = _context(tmp_path, "api/paid.py\n")
        assert cp.not_dockerignored(root, "src/proxy", COMMERCIAL) == ["src/proxy/paid_dir/"]

    def test_checks_the_extra_paths_too(self, cp, tmp_path):
        # How the owner's machine passes the commercial repo's own file list.
        root = _context(tmp_path, "paid_dir/\n")
        extra = ["src/proxy/paid_dir/a.py", "src/proxy/new_paid.py", "docs/x.md"]
        assert cp.not_dockerignored(root, "src/proxy", [], extra) == ["src/proxy/new_paid.py"]

    def test_a_path_in_both_lists_is_listed_once(self, cp, tmp_path):
        root = _context(tmp_path, "")
        assert cp.not_dockerignored(root, "src/proxy", ["src/proxy/x.py"],
                                    ["src/proxy/x.py"]) == ["src/proxy/x.py"]

    def test_the_proxy_image_leaves_out_every_commercial_path(self, cp):
        assert cp.not_dockerignored(ROOT, "src/proxy", _real_patterns(cp)) == []


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    for name in ("src/proxy/main.py", "src/proxy/api/paid.py", "src/proxy/paid_dir/a.py",
                 "other/src/proxy/api/paid.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    return tmp_path


PATTERNS = ["src/proxy/api/paid.py", "src/proxy/paid_dir/"]


class TestTrackedMatching:
    def test_a_tracked_commercial_file_is_listed(self, cp, repo):
        _git(repo, "add", "src/proxy/main.py", "src/proxy/api/paid.py")
        assert cp.tracked_matching(repo, PATTERNS) == ["src/proxy/api/paid.py"]

    def test_a_file_in_a_commercial_directory_is_listed(self, cp, repo):
        _git(repo, "add", "src/proxy/paid_dir/a.py")
        assert cp.tracked_matching(repo, PATTERNS) == ["src/proxy/paid_dir/a.py"]

    def test_core_files_and_lookalikes_elsewhere_are_not(self, cp, repo):
        # A pattern with a slash is anchored at the repo root, as in .gitignore.
        _git(repo, "add", "src/proxy/main.py", "other/src/proxy/api/paid.py")
        assert cp.tracked_matching(repo, PATTERNS) == []

    def test_untracked_commercial_files_are_not(self, cp, repo):
        _git(repo, "add", "src/proxy/main.py")
        assert cp.tracked_matching(repo, PATTERNS) == []

    def test_the_public_repo_tracks_no_commercial_file(self, cp):
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
                                capture_output=True)
        if inside.returncode:
            pytest.skip("not a git checkout (an exported tree)")
        assert cp.tracked_matching(ROOT, _real_patterns(cp)) == []


class TestCli:
    def test_prints_the_patterns_by_default(self, cp, tmp_path, capsys):
        (tmp_path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        assert cp.main([], root=tmp_path) == 0
        assert capsys.readouterr().out.split() == COMMERCIAL

    def test_tracked_exits_1_and_lists_what_it_found(self, cp, repo, capsys):
        (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        _git(repo, "add", "-f", "src/proxy/main.py", "src/proxy/api/paid.py")
        assert cp.main(["--tracked"], root=repo) == 1
        assert capsys.readouterr().out.split() == ["src/proxy/api/paid.py"]

    def test_tracked_exits_0_when_clean(self, cp, repo, capsys):
        (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        _git(repo, "add", "src/proxy/main.py")
        assert cp.main(["--tracked"], root=repo) == 0
        assert capsys.readouterr().out == ""

    def test_dockerignore_exits_1_and_lists_the_leaks(self, cp, tmp_path, capsys):
        root = _context(tmp_path, "paid_dir/\n")
        (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        assert cp.main(["--dockerignore", "src/proxy", "src/proxy/extra.py"], root=root) == 1
        assert capsys.readouterr().out.split() == ["src/proxy/api/paid.py", "src/proxy/extra.py"]

    def test_dockerignore_exits_0_when_everything_is_left_out(self, cp, tmp_path):
        root = _context(tmp_path, "api/paid.py\npaid_dir/\n")
        (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        assert cp.main(["--dockerignore", "src/proxy"], root=root) == 0

    # A check that cannot read its list must fail, not pass: exit 2 is neither 0 nor 1.
    def test_a_gitignore_without_commercial_paths_exits_2(self, cp, tmp_path, capsys):
        (tmp_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        assert cp.main(["--tracked"], root=tmp_path) == 2
        assert "commercial" in capsys.readouterr().err

    def test_a_git_failure_exits_2(self, cp, repo, monkeypatch):
        (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        monkeypatch.setenv("GIT_DIR", str(repo / "no-such-repo"))
        assert cp.main(["--tracked"], root=repo) == 2

    def test_paths_need_dockerignore(self, cp, tmp_path):
        with pytest.raises(SystemExit) as exc:
            cp.main(["src/proxy/x.py"], root=tmp_path)
        assert exc.value.code == 2


def test_without_git_on_path_it_says_so(cp, monkeypatch, tmp_path):
    # git is looked up on the caller's PATH and run by the path found; a missing one is named.
    monkeypatch.setattr(cp.shutil, "which", lambda name: None)
    with pytest.raises(Exception) as raised:
        cp.tracked_matching(tmp_path, ["*.py"])
    assert isinstance(raised.value, FileNotFoundError), repr(raised.value)
    assert "git is not on PATH" in str(raised.value)
