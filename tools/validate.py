#!/usr/bin/env python3
"""Validate package-factory configuration."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# Any registry reference written as repo:tag@sha256:..., the shape Renovate's
# build-root-image manager tracks and the only shape a lock entry may take.
PINNED_IMAGE = re.compile(r"(?P<base>[a-z0-9.\-/]+:[^\s@\"']+)@(?P<digest>sha256:[a-f0-9]{64})")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.package_inventory import inventory


def workflow_image_pins(workflows: Path) -> dict[str, set[tuple[str, str]]]:
    """Map repo:tag to the (workflow, digest) pairs that pin it.

    A buildroot is named twice -- once in the lock, once in the workflow that
    runs it -- so the two copies can drift. This reads the workflow side.
    """
    pins: dict[str, set[tuple[str, str]]] = {}
    if not workflows.is_dir():
        return pins
    for path in sorted(list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml"))):
        for match in PINNED_IMAGE.finditer(path.read_text()):
            pins.setdefault(match.group("base"), set()).add((path.name, match.group("digest")))
    return pins


def check_buildroot_drift(data: dict, workflows: Path) -> None:
    """Fail when a workflow pins a locked buildroot to a different digest.

    The digest lives in both config/buildroot-lock.json and the workflow that
    pulls it, because Renovate tracks the workflow shape and the manifest needs
    the declarative record. Two copies of one digest drift, so they are
    compared here rather than trusted to stay equal.
    """
    pins = workflow_image_pins(workflows)
    for name, buildroot in data["buildroots"].items():
        image = buildroot["image"]
        base, _, digest = image.partition("@")
        for workflow, found in sorted(pins.get(base, set())):
            if found != digest:
                raise SystemExit(
                    f"buildroot drift: {name} is locked to {base}@{digest} but "
                    f"{workflow} pins {base}@{found}"
                )


def validate_buildroots(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"missing buildroot lock: {path}")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise SystemExit(f"invalid buildroot lock: {path}")
    if data.get("schema") != 1 or not isinstance(data.get("buildroots"), dict):
        raise SystemExit(f"invalid buildroot lock: {path}")
    if not data["buildroots"]:
        raise SystemExit(f"invalid buildroot lock: no buildroots defined in {path}")
    for name, buildroot in data["buildroots"].items():
        image = buildroot.get("image") if isinstance(buildroot, dict) else None
        if not isinstance(image, str) or "@sha256:" not in image:
            raise SystemExit(f"buildroot {name} image must be digest-pinned")
        packages = buildroot.get("packages", [])
        if not isinstance(packages, list):
            raise SystemExit(f"buildroot {name} packages must be a list")
        for package in packages:
            if not isinstance(package, dict) or not package.get("nevra"):
                raise SystemExit(f"buildroot {name} package locks must carry NEVRA entries")

    check_buildroot_drift(data, path.resolve().parent.parent / ".github" / "workflows")


def check_provenance(path: Path, data: dict) -> None:
    required = {"package", "branch", "remote", "commit", "tree", "imported_at"}
    if set(data) != required:
        raise SystemExit(f"invalid upstream provenance: {path}")
    if data["branch"] not in ("rawhide", "upstream"):
        raise SystemExit(f"only rawhide or upstream imports are supported: {path}")
    if data["branch"] == "upstream":
        # Direct-upstream recipes (e.g. liblc3plus, libfreeaptx,
        # pipewire-libs-extra) are imported from the project's own release
        # repository rather than Fedora dist-git. They carry a remote and
        # imported_at but no dist-git commit/tree; the verified source lock
        # lives in config/upstream-sources.json instead.
        for key in ("commit", "tree"):
            if data[key]:
                raise SystemExit(f"upstream import must not carry {key}: {path}")
        if not data.get("remote"):
            raise SystemExit(f"upstream import must name its upstream remote: {path}")
    else:
        # Fedora dist-git imports pin the exact rawhide snapshot.
        for key in ("commit", "tree"):
            if not data.get(key):
                raise SystemExit(f"rawhide import must carry {key}: {path}")
            if not FULL_SHA.fullmatch(data[key]):
                raise SystemExit(f"rawhide import must carry a full {key} SHA: {path}")


def main(root: Path = Path(".")) -> int:
    packages_dir = root / "packages"
    if not packages_dir.is_dir():
        return 0
    for directory in sorted(packages_dir.iterdir()):
        if not directory.is_dir():
            continue
        path = directory / ".hummingbird-upstream.json"
        if not path.is_file():
            raise SystemExit(f"missing upstream provenance: {path}")
        data = json.loads(path.read_text())
        check_provenance(path, data)
    # Before the recipe tally, so a package that is merely missing a Packit
    # entry cannot hide a buildroot that drifted from its lock.
    validate_buildroots(root / "config" / "buildroot-lock.json")
    records = inventory(root)
    missing_locks = sorted(record.name for record in records if not record.source_locked)
    missing_packit = sorted(record.name for record in records if not record.packit_configured)
    missing_provenance = sorted(record.name for record in records if record.provenance is None)
    if missing_locks or missing_packit or missing_provenance:
        if missing_locks:
            print(f"packages missing source locks: {', '.join(missing_locks)}")
        if missing_packit:
            print(f"packages missing Packit config: {', '.join(missing_packit)}")
        if missing_provenance:
            print(f"packages missing recipe provenance: {', '.join(missing_provenance)}")
        return 1
    # The split is the point of the count: a direct-upstream recipe carries a
    # different provenance form from a dist-git import, and both are mandatory.
    forms: dict[str, int] = {}
    for record in records:
        forms[record.provenance_branch] = forms.get(record.provenance_branch, 0) + 1
    summary = ", ".join(f"{count} {form}" for form, count in sorted(forms.items()))
    print(f"validated {len(records)} source RPMs ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
