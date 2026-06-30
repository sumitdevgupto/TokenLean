"""Tests for operational scripts (C12-T)."""
import os
import stat
import subprocess
import pytest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
BUILD_LOCAL = SCRIPTS_DIR / "local" / "build-local.sh"


class TestBuildLocalScript:
    def test_build_local_sh_exists(self):
        assert BUILD_LOCAL.exists(), f"build-local.sh not found at {BUILD_LOCAL}"

    def test_build_local_sh_is_not_empty(self):
        content = BUILD_LOCAL.read_text()
        assert len(content.strip()) > 0, "build-local.sh is empty"

    def test_build_local_sh_has_shebang(self):
        first_line = BUILD_LOCAL.read_text().splitlines()[0]
        assert first_line.startswith("#!"), (
            f"build-local.sh missing shebang. First line: {first_line!r}"
        )

    def test_build_local_sh_passes_bash_syntax_check(self):
        """bash -n checks syntax without executing (feeds content via stdin for cross-platform compat)."""
        content = BUILD_LOCAL.read_bytes()
        result = subprocess.run(
            ["bash", "-n"],
            input=content,
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"bash -n syntax check failed:\n{result.stderr.decode(errors='replace')}"
        )

    def test_build_local_sh_references_docker_compose(self):
        content = BUILD_LOCAL.read_text()
        assert "docker-compose" in content, (
            "build-local.sh does not reference docker-compose"
        )

    def test_build_local_sh_references_build_and_up(self):
        content = BUILD_LOCAL.read_text()
        assert "build" in content, "build-local.sh does not call build"
        assert "up" in content, "build-local.sh does not call up"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="File permission bits not meaningful on Windows",
    )
    def test_build_local_sh_is_executable(self):
        mode = BUILD_LOCAL.stat().st_mode
        is_exec = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        assert is_exec, "build-local.sh is not executable (chmod +x missing)"
