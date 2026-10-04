"""Each sidecar's Python source is built into its image.

A sidecar directory holds what its Dockerfile builds. A Python file that the Dockerfile never
copies still reads like the running service, so a fix made there changes nothing that runs.
"""
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"


def _copied(dockerfile: Path):
    """The file names a Dockerfile's COPY and ADD lines bring into the image."""
    names = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if words and words[0].upper() in ("COPY", "ADD"):
            names.update(Path(word).name for word in words[1:-1])
    return names


def test_every_sidecar_python_file_is_copied_into_its_image():
    dockerfiles = sorted(_SRC.glob("*-sidecar/Dockerfile"))
    assert len(dockerfiles) >= 3                     # the scan found the sidecars
    unbuilt = [f"{dockerfile.parent.name}/{source.name}"
               for dockerfile in dockerfiles
               for source in sorted(dockerfile.parent.glob("*.py"))
               if source.name not in _copied(dockerfile)]
    assert not unbuilt, (
        f"{unbuilt}: these files sit beside a Dockerfile that never copies them, so they do "
        "not run. Build them into the image or delete them.")


def test_the_tika_sidecar_is_past_the_pdf_parser_xxe():
    """CVE-2025-54988 / CVE-2025-66516: an XXE in Tika's PDF parsing, 1.13 to 3.2.1, fixed in
    3.2.2. tika-svc parses documents tenants upload (the doc pipeline's fallback)."""
    import re
    base = next(line.split()[1] for line in
                (_SRC / "tika-sidecar" / "Dockerfile").read_text(encoding="utf-8").splitlines()
                if line.upper().startswith("FROM "))
    version = re.match(r"apache/tika:(\d+)\.(\d+)\.(\d+)", base)
    assert version, f"tika-sidecar is no longer built FROM a versioned apache/tika: {base}"
    assert tuple(int(v) for v in version.groups()) >= (3, 2, 2), base
