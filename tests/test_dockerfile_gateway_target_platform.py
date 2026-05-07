"""Regression guard: Dockerfile.gateway must not pin its builder to BUILDPLATFORM.

Pinning the builder stage to ``--platform=$BUILDPLATFORM`` made cargo emit a
binary in the build host's arch, which then got copied into a runtime image
of a different arch — the resulting image launched under
``qemu-<host-arch>`` and crashed on the missing dynamic linker. v0.5.1
removed that pin; this test fails the CI if anyone reintroduces it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_gateway_builder_does_not_pin_buildplatform() -> None:
    contents = (ROOT / "Dockerfile.gateway").read_text(encoding="utf-8")

    # Strip comment lines so the check only inspects actual Dockerfile
    # instructions — the rationale comment in the file legitimately mentions
    # the forbidden directive.
    code_lines = [
        line
        for line in contents.splitlines()
        if not line.lstrip().startswith("#")
    ]

    # The *runtime* stage may legitimately omit a platform pin (it inherits
    # the buildx target). What we forbid is the builder being pinned to the
    # build host — that decoupled the cargo target from the runtime image.
    for line in code_lines:
        assert "--platform=$BUILDPLATFORM" not in line, (
            "Dockerfile.gateway must not pin a build stage to BUILDPLATFORM. "
            "Doing so produces a binary in the build host's arch, mismatching "
            "the runtime image and crashing under qemu emulation. See v0.5.1 "
            "release notes."
        )

    # Sanity: the builder stage and a runtime stage both still exist; we
    # didn't accidentally collapse them.
    assert "AS builder" in contents
    assert "AS runtime" in contents
