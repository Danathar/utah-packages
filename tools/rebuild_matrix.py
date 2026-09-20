#!/usr/bin/env python3
"""Emit the rebuild matrix for .github/workflows/rebuild-rpms.yml.

The thin, untestable half of the decision: read the environment the workflow
provides, fetch the published repository, and write GITHUB_OUTPUT. Every rule
about what to build lives in tools/rebuild_plan.py, where it is under test.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rebuild_plan import (
    build_dependents,
    cacheable,
    stale_from_primary,
    provides_from_primary,
    changed_entries,
    dependents_from_primary,
    merge_dependents,
    overflow,
    plan_report,
    policy_causes,
    providers_from_primary,
    published_from_primary,
    render_plan,
    selection,
    stage_outputs,
)

ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_REPO_TIMEOUT = 120
INVENTORY = "config/upstream-sources.json"
PLAN_REPORT = Path("work") / "reports" / "build-plan.json"


def changed_paths(base_sha: str) -> list[str]:
    """Every path touched since the base commit.

    Empty when there is no range to read -- a scheduled run has no `before` and
    no pull request base -- which is why the published comparison must stand on
    its own rather than leaning on this.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha or "") or set(base_sha) == {"0"}:
        return []
    return subprocess.check_output(
        ["git", "diff", "--name-only", f"{base_sha}..HEAD"], text=True
    ).splitlines()


def changed_recipes(base_sha: str, paths: list[str]) -> set[str]:
    """Recipes touched since the base commit."""
    changed = {
        match.group(1)
        for path in paths
        if (match := re.match(r"^packages/([^/]+)/", path))
    }
    return changed | changed_inventory(base_sha, paths)


def changed_inventory(base_sha: str, paths: list[str]) -> set[str]:
    """Names whose entry in the source inventory changed since the base.

    Reads the old config out of git rather than trusting the diff text, so a
    reformat or a moved entry does not read as a change to every package.
    """
    if INVENTORY not in paths:
        return set()
    try:
        before = json.loads(
            subprocess.check_output(["git", "show", f"{base_sha}:{INVENTORY}"], text=True)
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        # The inventory did not exist or does not parse at the base commit.
        # Nothing can be proven unchanged, so prove nothing and let the
        # published comparison decide on its own.
        return set()
    after = json.loads((ROOT / INVENTORY).read_text())
    return changed_entries(before, after)


def fetch_primary(base_url: str) -> bytes:
    """The decompressed primary.xml of the repository at base_url.

    base_url is normally the file:// path of the repository `prepare`
    extracted from the published factory image, so the listing read here is
    byte-for-byte the one every build root will have enabled. A failure here
    is not fatal to the caller: an empty result means nothing can be proven
    published, so everything rebuilds. Slower, never wrong.
    """
    if not base_url:
        return b""
    if not base_url.endswith("/"):
        base_url += "/"
    repomd = (
        urllib.request.urlopen(base_url + "repodata/repomd.xml", timeout=60)
        .read()
        .decode()
    )
    href = re.search(r'<location href="([^"]*primary[^"]*)"', repomd).group(1)
    raw = urllib.request.urlopen(base_url + href, timeout=PUBLISHED_REPO_TIMEOUT).read()
    if href.endswith(".zst"):
        import zstandard

        primary = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    else:
        primary = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return primary


def hummingbird_baseurl(repo_file: Path) -> str:
    """The baseurl of config/hummingbird.repo, with a trailing slash."""
    match = re.search(r"^baseurl=(\S+)", repo_file.read_text(), re.MULTILINE)
    if match is None:
        raise ValueError(f"{repo_file} has no baseurl")
    return match.group(1).rstrip("/") + "/"


def fetch_published(base_url: str) -> dict[str, tuple[str, str]]:
    """What the published repository already carries, by source package."""
    return published_from_primary(fetch_primary(base_url))


def write_report(report: dict) -> None:
    """Leave the plan where a human and the next job can both read it."""
    path = ROOT / PLAN_REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    markdown = render_plan(report)
    path.with_suffix(".md").write_text(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(markdown + "\n")


def main() -> int:
    config = json.loads((ROOT / INVENTORY).read_text())
    factory_repo = os.environ.get("FACTORY_REPO", "")

    base_sha = os.environ.get("BASE_SHA", "")
    paths = changed_paths(base_sha)
    changed = changed_recipes(base_sha, paths)
    print(f"changed package recipes: {', '.join(sorted(changed)) or 'none'}")

    # A change to the build root or to the source tooling changes what every
    # recipe would build, and none of it is visible in a published NEVR, so
    # the listing stops being a witness for anything.
    causes = policy_causes(paths)
    full = os.environ.get("FULL") == "1" or bool(causes)
    if causes:
        print(f"buildroot or source policy changed, rebuilding all: {', '.join(causes)}")

    published: dict[str, tuple[str, str]] = {}
    dependents: dict[str, set[str]] = {}
    stale: dict[str, set[str]] = {}
    if not full:
        primary = b""
        try:
            primary = fetch_primary(factory_repo)
            published = published_from_primary(primary)
            if primary:
                # Runtime edges from what is published, build-time edges from
                # the recipes on disk, resolved through the same listing. One
                # graph: either kind of edge is a reason to rebuild.
                dependents = merge_dependents(
                    dependents_from_primary(primary),
                    build_dependents(
                        ROOT,
                        [entry["name"] for entry in config["packages"]],
                        providers_from_primary(primary),
                    ),
                )
            print(f"published repo has {len(published)} source packages")
        except Exception as error:  # noqa: BLE001 - availability, not correctness
            print(
                f"WARNING: could not read published repo, rebuilding all: {error}",
                file=sys.stderr,
            )
        # A published package whose binaries require something that neither
        # the published repository nor Hummingbird provides is stale: it was
        # built against a build root that has since moved. Without the
        # Hummingbird listing that judgement cannot be made, so it is not
        # made -- the run then trusts the recipe match alone, as before.
        if primary:
            try:
                external = provides_from_primary(
                    fetch_primary(hummingbird_baseurl(ROOT / "config" / "hummingbird.repo"))
                )
                stale = stale_from_primary(primary, external)
            except Exception as error:  # noqa: BLE001 - availability, not correctness
                print(
                    "WARNING: could not read the Hummingbird repository, "
                    f"so stale published builds cannot be detected: {error}",
                    file=sys.stderr,
                )
    if not factory_repo:
        print(
            "WARNING: no factory repository for the build root; "
            "nothing may be skipped as already published",
            file=sys.stderr,
        )

    selected = selection(
        config,
        ROOT,
        published=published,
        changed=changed,
        full=full,
        factory_repo=factory_repo,
        dependents=dependents,
        stale=set(stale),
        causes=causes,
    )
    build = [entry for entry, _ in selected]
    building = {entry["name"] for entry in build}
    for entry in config["packages"]:
        name = entry["name"]
        if name not in building:
            print(f"skip {name}: already published")
    for entry, reason in selected:
        name = entry["name"]
        if name in stale:
            missing = ", ".join(sorted(stale[name])[:3])
            print(f"rebuild {name}: published build requires {missing}, which nothing provides")
        else:
            print(f"rebuild {name}: {reason}")

    cacheable_names = cacheable(build, changed, set(stale))
    # Written before the overflow check, because a run that dies on a stage
    # with no job is exactly one where the plan is worth reading.
    write_report(
        plan_report(
            config, selected, full=full, causes=causes, cacheable_names=cacheable_names
        )
    )

    if late := overflow(build):
        raise SystemExit(
            "no job exists for stage 11 or later; "
            f"reduce the stage of: {', '.join(late)}"
        )

    outputs = stage_outputs(build)
    outputs["cacheable"] = json.dumps(cacheable_names)
    for stage in range(11):
        chunks = json.loads(outputs[f"stage{stage}_chunks"])
        if len(chunks) > 1:
            names = json.loads(outputs[f"stage{stage}"])
            print(f"stage {stage}: {len(names)} packages in {len(chunks)} chunks")

    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")
    print(f"will build {len(build)} of {len(config['packages'])} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
