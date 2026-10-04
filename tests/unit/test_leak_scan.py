"""Unit tests for scripts/ci/leak_scan.py — a content scan for high-confidence credential
formats, run by CI over tracked files and before a push over the staged diff.

Every fake credential below is assembled from fragments at runtime, so this file never holds a
string the scanner would flag (CI scans it too)."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = ROOT / "scripts" / "ci" / "leak_scan.py"


def _load_script():
    """Import leak_scan.py as a module (it's a standalone script, not a package member)."""
    spec = importlib.util.spec_from_file_location("leak_scan", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ss():
    return _load_script()


FAKES = {
    "private-key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "aws-access-key": "AKIA" + "Z" * 16,
    "google-api-key": "AIza" + "Z" * 35,
    "google-oauth-secret": "GOCSPX-" + "z" * 28,
    "github-token": "ghp" + "_" + "Z" * 36,
    "slack-token": "xoxb" + "-" + "1" * 12,
    "openai-key": "sk-" + "Z" * 20 + "T3Blbk" + "FJ" + "Z" * 20,
    "anthropic-key": "sk-ant-" + "api03-" + "Z" * 90,
    "stripe-live-key": "sk_" + "live_" + "Z" * 24,
    "sendgrid-key": "SG." + "Z" * 22 + "." + "Z" * 43,
    "tokenlean-proxy-key": "tok-acme-" + "a" * 48,
}
NEAR_MISSES = [
    "AKIA" + "z" * 16,                    # lower case is not an AWS key id
    "sk-" + "Z" * 40,                     # no OpenAI marker
    "tok-acme-" + "a" * 47,               # one hex digit short
    "tok-acme-" + "g" * 48,               # not hex
    "-----BEGIN " + "PUBLIC" + " KEY-----",
    "SG." + "Z" * 22,
]


class TestScanText:
    @pytest.mark.parametrize("kind", sorted(FAKES))
    def test_finds_each_kind_on_the_right_line(self, ss, kind):
        text = "first line\nvalue = '" + FAKES[kind] + "'\nlast line\n"
        assert ss.scan_text("conf.txt", text) == [("conf.txt", 2, kind)]

    @pytest.mark.parametrize("text", NEAR_MISSES)
    def test_lookalikes_are_not_flagged(self, ss, text):
        assert ss.scan_text("conf.txt", text) == []

    def test_an_allow_marker_skips_its_line(self, ss):
        text = "key = '" + FAKES["aws-access-key"] + "'  # leak-scan: allow\n"
        assert ss.scan_text("fixture.py", text) == []


def _diff(*lines):
    return "\n".join(lines) + "\n"


class TestAddedLines:
    def test_numbers_added_lines_by_the_new_file(self, ss):
        diff = _diff(
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -3,0 +4,2 @@ def f():",
            "+    a = 1",
            "+    b = 2",
            "@@ -10 +12 @@",
            "-    old = 0",
            "+    new = 0",
            "\\ No newline at end of file",
        )
        assert ss.added_lines(diff) == {"app.py": [(4, "    a = 1"), (5, "    b = 2"), (12, "    new = 0")]}

    def test_a_new_file_and_a_quoted_path(self, ss):
        diff = _diff(
            "diff --git a/new.txt b/new.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/new.txt",
            "@@ -0,0 +1 @@",
            "+hello",
            'diff --git "a/odd\\"name.txt" "b/odd\\"name.txt"',
            '--- "a/odd\\"name.txt"',
            '+++ "b/odd\\"name.txt"',
            "@@ -1 +1 @@",
            "-x",
            "+y",
        )
        assert ss.added_lines(diff) == {"new.txt": [(1, "hello")], 'odd"name.txt': [(1, "y")]}


def _git(cwd, *args, env=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "old.txt").write_text("keep\n" + FAKES["slack-token"] + "\n", encoding="utf-8")
    _git(tmp_path, "add", "old.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


class TestStaged:
    def test_reports_only_added_lines(self, ss, repo):
        # The committed slack token is old news; the new AWS key on line 3 is what gets pushed.
        (repo / "old.txt").write_text("keep\n" + FAKES["slack-token"] + "\nk = "
                                      + FAKES["aws-access-key"] + "\n", encoding="utf-8")
        _git(repo, "add", "old.txt")
        assert ss.staged_findings(repo) == [("old.txt", 3, "aws-access-key")]

    def test_unstaged_changes_are_not_scanned(self, ss, repo):
        (repo / "old.txt").write_text("k = " + FAKES["aws-access-key"] + "\n", encoding="utf-8")
        assert ss.staged_findings(repo) == []

    def test_a_second_git_dir_over_the_same_tree(self, ss, repo):
        # How the push script scans the commercial repo: --git-dir over the same working tree.
        _git(repo, "--git-dir=.git-other", "--work-tree=.", "init", "-q")
        (repo / "other.txt").write_text(FAKES["github-token"] + "\n", encoding="utf-8")
        _git(repo, "--git-dir=.git-other", "--work-tree=.", "add", "other.txt")
        assert ss.staged_findings(repo, git_dir=".git-other") == [("other.txt", 1, "github-token")]
        assert ss.staged_findings(repo) == []


class TestTracked:
    def test_scans_tracked_files_only_and_skips_binary(self, ss, repo):
        (repo / "untracked.txt").write_text(FAKES["aws-access-key"] + "\n", encoding="utf-8")
        (repo / "blob.bin").write_bytes(b"\0\1" + FAKES["aws-access-key"].encode())
        _git(repo, "add", "blob.bin")
        assert ss.tracked_findings(repo) == [("old.txt", 2, "slack-token")]

    def test_the_public_repo_holds_no_credential(self, ss):
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
                                capture_output=True)
        if inside.returncode:
            pytest.skip("not a git checkout (an exported tree)")
        assert ss.tracked_findings(ROOT) == []


class TestCli:
    def test_findings_exit_1_and_never_print_the_value(self, ss, repo, capsys):
        assert ss.main(["--tracked"], root=repo) == 1
        out = capsys.readouterr().out
        assert "old.txt:2: slack-token" in out
        assert FAKES["slack-token"] not in out

    def test_clean_exits_0(self, ss, repo, capsys):
        assert ss.main(["--staged"], root=repo) == 0
        assert capsys.readouterr().out == ""

    def test_a_git_failure_exits_2(self, ss, repo, monkeypatch):
        monkeypatch.setenv("GIT_DIR", str(repo / "no-such-repo"))
        assert ss.main(["--tracked"], root=repo) == 2

    def test_a_mode_is_required(self, ss, repo):
        with pytest.raises(SystemExit) as exc:
            ss.main([], root=repo)
        assert exc.value.code == 2
