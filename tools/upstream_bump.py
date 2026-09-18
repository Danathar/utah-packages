#!/usr/bin/env python3
"""Propose version bumps for the packages this factory locks to download.gnome.org.

Why this exists
---------------
`renovate.json` already carries a custom manager aimed at
`config/upstream-sources.json`, keyed on `datasource` and `depName`. No entry
carries those fields, so it has never matched anything and nothing here has
ever been bumped automatically -- which is why the inventory still asks for
GNOME 51 betas after 51.0 shipped.

Renovate could not finish the job on its own anyway. A version in this factory
is written in three places that must move together, and Renovate can rewrite
only the first:

  config/upstream-sources.json  version, url, filename, sha512, sha256_url,
                                fallback_urls
  packages/<pkg>/<pkg>.spec     Version:
  packages/<pkg>/sources        SHA512 (<tarball>) = <digest>

A version bump with a stale checksum is rejected by source_pipeline.py -- a
safe failure, but not a working bump. So the checksum has to be computed from
the bytes at bump time, which is what this does.

Scope
-----
Only entries whose Source0 is download.gnome.org. That is 17 of 341; the other
254 resolve through Fedora's lookaside, whose natural feed is dist-git, and
detect-rawhide-updates.yml deliberately only observes there on the stated
policy that "Fedora is a compatibility build root, not a source-update feed."
Widening this tool to those is a separate decision, not an omission.

GNOME publishes an authoritative release index per module at
sources/<module>/cache.json, so the candidate list needs no scraping.

Two spellings
-------------
RPM orders a prerelease below its final with a tilde, so the spec says
`Version: 51~beta` while the tarball is `gnome-shell-51.beta.tar.xz`. Fedora's
%{gnome_tarball_version} macro performs that `~` -> `.` conversion, which is
why bumping `Version:` alone also moves Source0 -- 25 specs here rely on it.
The inventory stores the tarball spelling. Both are derived from one release
string rather than restated, so they cannot disagree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.package_inventory import source_locks

GNOME_SOURCES = "https://download.gnome.org/sources/"
LOOKASIDE = "https://src.fedoraproject.org/repo/pkgs/rpms"

# alpha/beta/rc in any spelling GNOME uses: 51.beta, 51~rc, 1.10.beta.1.
# The trailing context is a lookahead, not part of the match: consuming it made
# rpm_version("1.10.beta.1") return "1.10~beta1", eating the separator before
# the point release. Caught by the round-trip test.
PRERELEASE = re.compile(r"(?:^|[.~-])(alpha|beta|rc)(?=[.~-]|\d|$)", re.IGNORECASE)
# The same marker with its leading separator, for rewriting that separator to
# the tilde RPM needs.
PRERELEASE_SEPARATOR = re.compile(r"[.~-](alpha|beta|rc)(?=[.~-]|\d|$)", re.IGNORECASE)


def is_prerelease(version: str) -> bool:
    """True for a GNOME prerelease such as 51.beta, 51~rc or 1.10.beta.1."""
    return bool(PRERELEASE.search(version))


def version_key(version: str) -> tuple[int, ...]:
    """Sort key for a stable GNOME version.

    Only meaningful for stable versions, which are numeric and dot-separated;
    callers filter prereleases out first. A non-numeric component sorts as -1
    rather than raising, so one malformed entry in cache.json cannot take the
    whole run down.
    """
    return tuple(int(part) if part.isdigit() else -1 for part in version.split("."))


def newest_stable(versions: list[str]) -> str | None:
    """The highest non-prerelease in a cache.json version list, or None.

    "Not alpha/beta/rc" is not the same as "stable" -- see cycle_final.
    """
    stable = [v for v in versions if not is_prerelease(v)]
    return max(stable, key=version_key) if stable else None


def cycle_final(versions: list[str], cycle: str) -> str | None:
    """The highest release within one cycle, ignoring prereleases.

    A bump is only ever proposed for application automatically when it stays
    inside the cycle the lock already names -- 51.beta to 51.0. Crossing a
    cycle cannot be decided from the number alone, because GNOME's numbering
    encodes development series that no arithmetic rule separates from stable
    ones. pango is the proof: its releases run

        1.56.4, 1.57.0, 1.57.1, 1.58.0, 1.58.2, 1.90.0

    where 1.57 is a development series under the traditional odd-minor
    convention and 1.90 is the development series toward 2.0. Both sort above
    the stable 1.58.2, and 90 is even, so neither "highest" nor "even minor"
    picks the right answer. An earlier draft of this tool proposed
    1.58.2 -> 1.90.0 for exactly that reason.

    So cross-cycle candidates are reported for a human and never applied.
    """
    inside = [
        v
        for v in versions
        if not is_prerelease(v) and release_cycle(tarball_version(v)) == cycle
    ]
    return max(inside, key=version_key) if inside else None


def tarball_version(version: str) -> str:
    """The tarball spelling: RPM's prerelease tilde becomes a dot."""
    return version.replace("~", ".")


def rpm_version(version: str) -> str:
    """The Version: spelling: a prerelease sorts below its final with a tilde."""
    if not is_prerelease(version):
        return version
    return PRERELEASE_SEPARATOR.sub(lambda m: "~" + m.group(1), version, count=1)


def major(version: str) -> str:
    """The directory GNOME files a release under: the leading component."""
    return version.split(".")[0]


def release_cycle(version: str) -> str:
    """The development cycle a release belongs to.

    GNOME uses two numbering schemes and the cycle sits in a different place in
    each, so this cannot be "the first component":

      gnome-shell 51.beta, 51.0     cycle 51    -- the app scheme, where the
                                                   leading number is the GNOME
                                                   release
      pango 1.58.2, gtk4 4.23.3     cycle 1.58  -- the library scheme, where
                                                   major.minor is the cycle and
                                                   the last component is the
                                                   point release
      libadwaita 1.10.beta.1        cycle 1.10  -- library scheme, prerelease

    Getting this wrong is not cosmetic. With the cycle read as just the leading
    component, pango's cycle was "1", which made the development release 1.90.0
    look like an in-cycle successor to the stable 1.58.2.
    """
    parts = version.split(".")
    numeric = []
    for part in parts:
        if not part.isdigit():
            break
        numeric.append(part)
    if not numeric:
        return version
    if is_prerelease(version):
        # Everything before the marker is the cycle: 1.10.beta.1 -> 1.10.
        return ".".join(numeric)
    # A three-component release keeps major.minor; a two-component one is the
    # app scheme, where the trailing number is the point release within a cycle.
    return ".".join(numeric[:-1]) if len(numeric) >= 3 else numeric[0]


def gnome_module(entry: dict) -> str | None:
    """The download.gnome.org module a locked entry tracks, if it tracks one."""
    url = entry.get("url", "")
    if not url.startswith(GNOME_SOURCES):
        return None
    return url[len(GNOME_SOURCES):].split("/", 1)[0] or None


def releases(module: str, opener=urllib.request.urlopen) -> list[str]:
    """Every release GNOME lists for a module, from its cache.json index.

    cache.json is a 4-element array whose third element maps module name to an
    ordered version list.
    """
    request = urllib.request.Request(
        f"{GNOME_SOURCES}{module}/cache.json",
        headers={"User-Agent": "utah-packages-bump/1"},
    )
    with opener(request, timeout=60) as response:
        document = json.loads(response.read())
    return list(document[2].get(module, []))


def sha512_of(url: str, opener=urllib.request.urlopen) -> str:
    """The SHA-512 of the bytes at a URL, streamed rather than buffered."""
    request = urllib.request.Request(url, headers={"User-Agent": "utah-packages-bump/1"})
    digest = hashlib.sha512()
    with opener(request, timeout=300) as response:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def planned_entry(entry: dict, release: str, digest: str) -> dict:
    """The locked entry rewritten for a new release.

    Every URL is rebuilt from the release string rather than patched, so a
    field cannot be left behind pointing at the old tarball -- fallback_urls in
    particular embeds both the filename and the digest.
    """
    module = gnome_module(entry)
    tarball = tarball_version(release)
    name = f"{module}-{tarball}.tar.xz"
    base = f"{GNOME_SOURCES}{module}/{major(tarball)}"
    updated = dict(entry)
    updated["version"] = tarball
    updated["url"] = f"{base}/{name}"
    updated["filename"] = name
    updated["sha512"] = digest
    if "sha256_url" in entry:
        updated["sha256_url"] = f"{base}/{module}-{tarball}.sha256sum"
    if entry.get("fallback_urls"):
        updated["fallback_urls"] = [
            f"{LOOKASIDE}/{module}/{name}/sha512/{digest}/{name}"
        ]
    return updated


def rewrite_spec(spec: Path, release: str) -> bool:
    """Point a spec's Version: at a new release. True when it changed."""
    text = spec.read_text()
    wanted = rpm_version(release)
    pattern = re.compile(r"(?m)^(Version:\s*)(\S+)$")
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"{spec}: no Version: line to bump")
    if match.group(2) == wanted:
        return False
    spec.write_text(pattern.sub(lambda m: m.group(1) + wanted, text, count=1))
    return True


def rewrite_sources(manifest: Path, filename: str, digest: str) -> None:
    """Replace a package's Fedora sources manifest with the new tarball pin."""
    manifest.write_text(f"SHA512 ({filename}) = {digest}\n")


def candidates(locks: dict[str, dict], only: str | None = None) -> list[tuple[str, dict, str]]:
    """(name, entry, module) for every GNOME-hosted lock, newest first by name."""
    found = []
    for name, entry in sorted(locks.items()):
        if only and name != only:
            continue
        module = gnome_module(entry)
        if module:
            found.append((name, entry, module))
    return found


def plan(root: Path, only: str | None, opener=urllib.request.urlopen) -> list[dict]:
    """What would change, without changing anything.

    Each proposal carries a `kind`:

      "final"  -- the lock names a prerelease and the same cycle has since
                  produced a release. Safe to apply: the cycle is already the
                  maintainers' choice, and only the prerelease suffix moves.
      "review" -- a newer release exists in a later cycle. Reported so a human
                  sees it, never applied; see cycle_final for why the number
                  cannot decide this.

    A module whose index cannot be read is reported and skipped rather than
    failing the run: one unreachable module must not stop the other sixteen.
    """
    locks = source_locks(root)
    proposals = []
    for name, entry, module in candidates(locks, only):
        try:
            available = releases(module, opener=opener)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as error:
            proposals.append({"name": name, "error": f"{module}: {error}"})
            continue
        current = tarball_version(entry["version"])
        cycle = release_cycle(current)

        within = cycle_final(available, cycle)
        if is_prerelease(entry["version"]) and within is not None:
            proposals.append(
                {
                    "kind": "final",
                    "name": name,
                    "module": module,
                    "current": entry["version"],
                    "latest": within,
                }
            )
            continue
        if within is not None and version_key(within) > version_key(current):
            proposals.append(
                {
                    "kind": "final",
                    "name": name,
                    "module": module,
                    "current": entry["version"],
                    "latest": within,
                }
            )
            continue

        newest = newest_stable(available)
        if newest is not None and version_key(newest) > version_key(current):
            proposals.append(
                {
                    "kind": "review",
                    "name": name,
                    "module": module,
                    "current": entry["version"],
                    "latest": newest,
                }
            )
    return proposals


def apply(root: Path, proposal: dict, opener=urllib.request.urlopen) -> dict:
    """Move one package to a new release across all three files."""
    name, release = proposal["name"], proposal["latest"]
    config = root / "config" / "upstream-sources.json"
    document = json.loads(config.read_text())
    index = next(i for i, e in enumerate(document["packages"]) if e["name"] == name)
    entry = document["packages"][index]

    module = gnome_module(entry)
    tarball = tarball_version(release)
    url = f"{GNOME_SOURCES}{module}/{major(tarball)}/{module}-{tarball}.tar.xz"
    digest = sha512_of(url, opener=opener)

    updated = planned_entry(entry, release, digest)
    document["packages"][index] = updated
    config.write_text(json.dumps(document, indent=2) + "\n")

    package = root / "packages" / name
    spec = package / f"{name}.spec"
    if spec.is_file():
        rewrite_spec(spec, release)
    manifest = package / "sources"
    if manifest.is_file():
        rewrite_sources(manifest, updated["filename"], digest)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--package", help="consider only this package")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite the inventory, spec and sources manifest (default: report only)",
    )
    args = parser.parse_args()

    proposals = plan(args.root, args.package)
    failures = [p for p in proposals if "error" in p]
    finals = [p for p in proposals if p.get("kind") == "final"]
    review = [p for p in proposals if p.get("kind") == "review"]

    for failure in failures:
        print(f"skipped {failure['name']}: {failure['error']}", file=sys.stderr)

    for bump in finals:
        print(f"{bump['name']}: {bump['current']} -> {bump['latest']}")
        if args.apply:
            apply(args.root, bump)

    for item in review:
        # Deliberately not applied, and deliberately not silent: a later cycle
        # may be a development series (pango 1.90 toward 2.0), which no rule
        # here can tell from a stable one.
        print(
            f"needs review  {item['name']}: {item['current']} -> {item['latest']} "
            f"(crosses a release cycle)",
            file=sys.stderr,
        )

    if not finals:
        print("no in-cycle release is newer than what the inventory locks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
