#!/usr/bin/env python3
"""Fail when a recipe silences its own ``%check`` with ``tests_nonfatal``.

Fedora's audio specs share a ``%check`` shaped like this::

    %meson_test || TESTS_ERROR=$?
    if [ "${TESTS_ERROR}" != "" ]; then
    echo "test failed"
    %{!?tests_nonfatal:exit $TESTS_ERROR}
    fi

so defining ``tests_nonfatal`` anywhere above it turns a failing suite into a
successful build in one line, and the build log still prints ``test failed``.
Nothing else in the factory notices.

That line was the obvious way to close issue #132: ``pipewire``'s
``pw-test-endpoint`` hangs and is killed by its own ``alarm(5)``, it is the
only failure in the whole recipe set, and the recipe cannot simply be dropped
because ``pipewire-libs-extra`` -- which Utah does install -- carries
``Requires: pipewire >= %{version}``. One ``%global`` would have published a
silently broken audio stack.

``AGENTS.md`` and the ``build-failure-triage`` skill both already say never to
skip a test to get a build green. This makes that mechanical, because a prose
rule did not stop the question being asked.

``pulseaudio`` is the one recipe that already defines it, inherited verbatim
from Fedora dist-git rather than added here. It is recorded below so the gate
passes on the tree as imported while still refusing anything new; the entry is
a description of what we inherited, not permission to add more.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ``%global tests_nonfatal 1`` / ``%define tests_nonfatal 1``. Only a
# definition matters: ``%{!?tests_nonfatal:exit $TESTS_ERROR}`` is the guard
# itself and appears in every recipe that carries this %check shape.
DEFINITION = re.compile(r"^\s*%(?:global|define)\s+tests_nonfatal\b")

# package -> why the definition is there. Inherited from Fedora, never ours.
INHERITED = {
    # packages/pulseaudio/pulseaudio.spec %check, Fedora's own FIXMEs: one
    # arch-gated (i686 cpu-remap-test, s390x core-util-test) and one
    # release-gated (`%if 0%{?fedora} > 27`) which is therefore always on for
    # the Fedora 44 build root. PulseAudio's suite is effectively advisory
    # here. Raise it with a human before relying on it as a gate.
    "pulseaudio": "inherited from Fedora dist-git, arch and release gated",
}


def offenders(root: Path) -> list[tuple[str, int, str]]:
    """Return (package, line number, line) for every new ``tests_nonfatal``."""
    found = []
    for spec in sorted((root / "packages").glob("*/*.spec")):
        package = spec.parent.name
        if package in INHERITED:
            continue
        for number, line in enumerate(spec.read_text().splitlines(), start=1):
            if DEFINITION.match(line):
                found.append((package, number, line.strip()))
    return found


def stale(root: Path) -> list[str]:
    """Return allowlisted recipes that are present and no longer suppress.

    An exception nobody can see is worse than no exception: it keeps claiming
    a recipe is compromised long after the import that made it so is gone.

    A package that is absent entirely is **not** stale. ``tools/validate.py``
    runs against synthetic trees in its own tests and against whatever root it
    is handed; complaining that a recipe the caller never had is missing would
    make the gate depend on the tree it is pointed at. A dropped recipe is
    caught by ``tests/test_check_suppressed_tests.py`` instead, which asserts
    the entries still exist in this repository.
    """
    gone = []
    for package in sorted(INHERITED):
        specs = sorted((root / "packages" / package).glob("*.spec"))
        if not specs:
            continue
        if not any(
            DEFINITION.match(line)
            for spec in specs
            for line in spec.read_text().splitlines()
        ):
            gone.append(f"{package}: no longer defines tests_nonfatal")
    return gone


def main(root: Path | None = None) -> int:
    root = root or Path(__file__).resolve().parent.parent
    problems = offenders(root)
    rotted = stale(root)

    if problems:
        print(
            "A recipe may not define tests_nonfatal. It turns a failing %check "
            "into a green build and ships the failure:",
            file=sys.stderr,
        )
        for package, number, line in problems:
            print(f"  packages/{package}: line {number}: {line}", file=sys.stderr)
        print(
            "\nFix the test or the build root instead. See "
            ".agents/skills/build-failure-triage/SKILL.md.",
            file=sys.stderr,
        )
    if rotted:
        print(
            "\ntools/check_suppressed_tests.py INHERITED is stale; drop these "
            "entries:",
            file=sys.stderr,
        )
        for entry in rotted:
            print(f"  {entry}", file=sys.stderr)

    if problems or rotted:
        return 1

    count = len(list((root / "packages").glob("*/*.spec")))
    inherited = ", ".join(sorted(INHERITED))
    print(
        f"checked {count} recipes: no new tests_nonfatal "
        f"(inherited: {inherited})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
