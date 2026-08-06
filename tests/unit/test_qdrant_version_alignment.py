"""
Guard: every qdrant-client pin must stay in lockstep with the Qdrant server.

qdrant-client refuses a client/server gap of more than one minor version
("Major versions should match and minor version difference must not exceed 1")
and warns loudly at connect time. The proxy, the doc/finetune pipelines and the
pitch-test-plan harness all talk to the SAME server and the pipelines seed the
very collections G07 reads back, so a single drifting pin is enough to reintroduce
the skew. This test fails the moment one of them moves on its own.

Regression: 2026-08-06 — the pipelines sat on `>=1.18.0` (uncapped) while the
proxy was pinned `>=1.12,<1.13`, GCP shipped server v1.9.0 and local dev v1.12.6.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

# Every requirements file that pins qdrant-client. All must carry the SAME spec.
# Split by tree: the OSS gate builds via `git archive HEAD`, so anything gitignored
# is simply absent there and must be checked only when it is actually present.
CLIENT_PIN_FILES = [
    "src/proxy/requirements.txt",
    "src/doc-pipeline/requirements.txt",
    "src/finetune-pipeline/requirements.txt",
]

# Gitignored (commercial repo) — present in a full working tree, absent in the OSS tree.
OPTIONAL_CLIENT_PIN_FILES = [
    "pitch-test-plan/requirements-proxy.txt",
    "pitch-test-plan/requirements.txt",
]

# Every place the Qdrant SERVER image version is declared.
SERVER_IMAGE_FILES = [
    "docker-compose.yml",
    "infra/variables.tf",
]

_CLIENT_RE = re.compile(r"^qdrant-client(?P<spec>[^#\s]+)", re.MULTILINE)
_SERVER_RE = re.compile(r"qdrant/qdrant:v(?P<major>\d+)\.(?P<minor>\d+)")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.is_file(), f"expected {rel} to exist"
    return path.read_text(encoding="utf-8")


def _client_specs() -> dict:
    """Pins from the OSS tree, plus the commercial ones when the tree has them."""
    specs = {}
    for rel in CLIENT_PIN_FILES:
        match = _CLIENT_RE.search(_read(rel))
        assert match, f"{rel} no longer pins qdrant-client"
        specs[rel] = match.group("spec").strip()

    for rel in OPTIONAL_CLIENT_PIN_FILES:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue  # OSS tree — gitignored file legitimately absent
        match = _CLIENT_RE.search(path.read_text(encoding="utf-8"))
        assert match, f"{rel} no longer pins qdrant-client"
        specs[rel] = match.group("spec").strip()
    return specs


def _server_versions() -> dict:
    versions = {}
    for rel in SERVER_IMAGE_FILES:
        match = _SERVER_RE.search(_read(rel))
        assert match, f"{rel} no longer declares a qdrant/qdrant image"
        versions[rel] = (int(match.group("major")), int(match.group("minor")))
    return versions


def test_every_client_pin_is_identical():
    """One drifting requirements file is all it takes to reintroduce the skew."""
    specs = _client_specs()
    unique = set(specs.values())
    assert len(unique) == 1, (
        "qdrant-client pins have diverged across requirements files — they all talk "
        f"to the same server and must match exactly: {specs}"
    )


def test_client_pin_is_capped_not_open_ended():
    """An uncapped `>=` floor lets pip resolve straight past the server's minor."""
    for rel, spec in _client_specs().items():
        assert "<" in spec, (
            f"{rel} pins qdrant-client as {spec!r} with no upper bound — pip will "
            "resolve to latest and drift past the server. Use e.g. '>=1.12,<1.13'."
        )


def test_server_image_versions_agree():
    """Local dev and GCP must not run different Qdrant servers."""
    versions = _server_versions()
    assert len(set(versions.values())) == 1, (
        f"Qdrant server version differs between deploy targets: {versions}"
    )


def test_client_pin_tracks_the_server_minor():
    """qdrant-client hard-fails when the client/server minor gap exceeds 1."""
    spec = next(iter(set(_client_specs().values())))
    floor = re.search(r">=\s*(\d+)\.(\d+)", spec)
    assert floor, f"could not parse a floor out of qdrant-client spec {spec!r}"
    client = (int(floor.group(1)), int(floor.group(2)))

    for rel, server in _server_versions().items():
        assert client[0] == server[0], (
            f"qdrant-client major {client[0]} != server major {server[0]} ({rel})"
        )
        assert abs(client[1] - server[1]) <= 1, (
            f"qdrant-client {client[0]}.{client[1]} is more than one minor from the "
            f"server {server[0]}.{server[1]} declared in {rel} — the client will "
            "refuse to talk to it. Bump the server and every client pin together."
        )
