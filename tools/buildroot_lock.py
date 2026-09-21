#!/usr/bin/env python3
"""Resolve and snapshot factory buildroots.

``config/buildroot-lock.json`` is the declarative record of which image a
buildroot is: a digest pin, not a tag. ``snapshot`` writes what the root
actually contained -- every package NEVRA, the digest the run resolved, and the
digest the lock expected -- so a rebuild says which bytes it used rather than
which bytes it meant to use.

The package list comes from ``rpm -qa`` here, or from ``--packages-from`` when
that inventory was printed elsewhere; CI uses the second form, because the
build root is a Fedora container with no interpreter this factory may assume.

The lock is never used as the resolved image: with no ``--image``,
``--digest`` or ``BUILDROOT_DIGEST`` the snapshot records ``image: null`` and
warns, so an empty digest cannot quietly re-assert the lock's provenance.
Those three are the only inputs. The job-wide ``BUILDROOT_IMAGE`` is not read,
because ``tools/validate.py`` forces it to equal the lock's digest and reading
it would re-assert that pin through a second door.

A mismatch between the resolved and locked image warns, matching the workflow:
``quay.io/fedora/fedora:44`` is republished several times a day and failing on
a moved tag is the outage `docs/skills/repeated-mistakes.md` section 7 records.
``--strict`` turns that warning, and any divergence from a non-empty locked
package list, into an error -- for an operator asking whether a root is exactly
what was locked, not for the scheduled rebuild.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_CONFIG = Path("config/buildroot-lock.json")


def load_lock(path: Path) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema") != 1 or not isinstance(data.get("buildroots"), dict):
        raise ValueError(f"invalid buildroot lock: {path}")
    for name, br in data["buildroots"].items():
        image = br.get("image") if isinstance(br, dict) else None
        if not isinstance(image, str) or "@sha256:" not in image:
            raise ValueError(f"buildroot {name} image must be digest-pinned")
        if not isinstance(br.get("packages", []), list):
            raise ValueError(f"buildroot {name} packages must be a list")
    return data


def buildroot(data: dict, name: str) -> dict:
    value = data["buildroots"].get(name)
    if not isinstance(value, dict):
        raise ValueError(f"unknown buildroot: {name}")
    image = value.get("image")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise ValueError(f"buildroot {name} image must be digest-pinned")
    packages = value.get("packages", [])
    if not isinstance(packages, list):
        raise ValueError(f"buildroot {name} packages must be a list")
    return value


QUERY_FORMAT = "%{NAME}\t%|EPOCH?{%{EPOCH}:}|%{VERSION}-%{RELEASE}\t%{ARCH}\n"


def parse_packages(lines: Iterable[str]) -> list[dict[str, str]]:
    """Turn ``rpm -qa --qf QUERY_FORMAT`` output into sorted NEVRA records.

    Taken as text rather than by calling rpm, because the inventory is printed
    inside the build root and read out here: the root is a Fedora container
    with no interpreter this factory is entitled to assume, and installing one
    to take its own inventory would change the set being recorded.
    """
    packages = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        name, evra, arch = parts
        packages.append(
            {
                "name": name,
                "evra": evra,
                "arch": arch,
                "nevra": f"{name}-{evra}.{arch}",
            }
        )
    return sorted(packages, key=lambda item: (item["name"], item["evra"], item["arch"]))


def rpm_packages() -> list[dict[str, str]]:
    output = subprocess.check_output(["rpm", "-qa", "--qf", QUERY_FORMAT], text=True)
    return parse_packages(output.splitlines())


def compare(expected: list[dict], actual: list[dict]) -> list[str]:
    wanted = {item.get("nevra") for item in expected if isinstance(item, dict)}
    got = {item["nevra"] for item in actual}
    missing = sorted(wanted - got)
    extra = sorted(got - wanted)
    errors = []
    if missing:
        errors.append("missing locked buildroot packages: " + ", ".join(missing[:20]))
    if extra:
        errors.append("unexpected buildroot packages: " + ", ".join(extra[:20]))
    return errors


def cmd_image(args: argparse.Namespace) -> int:
    lock = load_lock(args.config)
    print(buildroot(lock, args.name)["image"])
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    lock = load_lock(args.config)
    spec = buildroot(lock, args.name)
    if args.packages_from:
        if not args.packages_from.is_file():
            print(f"missing buildroot inventory: {args.packages_from}", file=sys.stderr)
            return 1
        packages = parse_packages(args.packages_from.read_text().splitlines())
    else:
        packages = rpm_packages()
    locked_image = spec["image"]
    base_ref = locked_image.split("@")[0]
    digest = getattr(args, "digest", None) or os.environ.get("BUILDROOT_DIGEST")
    # Only what the run resolved counts as the image. The lock is never a
    # fallback: it says which bytes the rebuild meant to use, and writing it
    # here would attest a root the packages may not have been built in. With
    # nothing resolved the snapshot records null and says so.
    # No environment fallback either: the workflow exports BUILDROOT_IMAGE for
    # the whole job and validate.py forces it to equal the lock's digest, so
    # reading it here would re-assert the lock's pin through a second door --
    # the same attestation this command exists to avoid making.
    actual_image = getattr(args, "image", None) or (
        f"{base_ref}@{digest}" if digest else None
    )
    payload = {
        "schema": 1,
        "name": args.name,
        "image": actual_image,
        "locked_image": locked_image,
        "captured_at": datetime.now(UTC).isoformat(),
        "packages": packages,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    expected = spec.get("packages", [])
    errors = []
    if actual_image is None:
        msg = (
            f"buildroot image unknown: no --image, --digest or BUILDROOT_DIGEST was given, "
            f"so the snapshot records image null rather than the lock's {locked_image}"
        )
        if args.strict:
            errors.append(msg)
        else:
            print(f"warning: {msg}", file=sys.stderr)
    elif actual_image != locked_image:
        msg = f"buildroot image mismatch: lock specifies {locked_image}, actual running image is {actual_image}"
        if args.strict:
            errors.append(msg)
        else:
            print(f"warning: {msg}", file=sys.stderr)
    if args.strict and expected:
        errors.extend(compare(expected, packages))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"captured {len(packages)} buildroot packages for {args.name}: {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    image = sub.add_parser("image")
    image.add_argument("name")
    image.set_defaults(func=cmd_image)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("name")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument(
        "--packages-from",
        type=Path,
        # argparse %-formats help text, so the rpm query's own % signs must
        # be doubled or the parser refuses the string (eagerly on 3.14+).
        help="read rpm -qa --qf %s output instead of running rpm"
        % repr(QUERY_FORMAT).replace("%", "%%"),
    )
    snapshot.add_argument("--image", help="actual image/digest of the running buildroot")
    snapshot.add_argument("--digest", help="actual digest of the running buildroot")
    snapshot.add_argument("--strict", action="store_true")
    snapshot.set_defaults(func=cmd_snapshot)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
