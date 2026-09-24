#!/usr/bin/env python3
"""The atomic-publish gate for the Utah factory.

ghcr.io/.../utah-packages:latest is the only digest consumers read. It may move
only when the whole selected package set is coherent: every selected rebuild
wave and the precedence check succeed, and the composed Hummingbird-only consumer
transaction resolves against the repository being published. Any selected package
failure, precedence failure, or unresolved transaction leaves the published
digest unchanged; the failed run's successful RPMs are retained as Actions
artifacts for diagnosis but never reach the consumer tag.

This module encodes that gate in two forms that must agree:

* ``publish_allowed`` is a pure decision function over the job outcomes. The
  regression test drives it through every failure mode.
* ``assert_gate_enforced`` reads ``.github/workflows/rebuild-rpms.yml`` and
  checks the publish job's own ``if:`` and step order encode the same gate, so
  the workflow and the decision function cannot drift apart.

The wave inventory is discovered from the workflow rather than restated here,
because growth in this factory means adding a wave. A check that only walks a
hardcoded list notices a wave that disappears from the gate and never notices
one that appears in the workflow without a gate clause -- the direction that
would let a failed wave publish.

The workflow's native ``if:`` is the gate that actually runs; this module proves
it holds and keeps it honest.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REBUILD_WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "rebuild-rpms.yml"
)

# Every build wave the publish job waits on, in wave order. This restates what
# rebuild-rpms.yml declares, so that ``publish_allowed`` can be exercised
# without reading the workflow; ``assert_gate_enforced`` proves the two agree.
STAGES = tuple(f"rebuild{stage}" for stage in range(11))

STAGE_JOB = re.compile(r"^rebuild(\d+)$")


def rebuild_stages(workflow: dict) -> tuple[str, ...]:
    """The rebuild waves ``rebuild-rpms.yml`` actually declares, in wave order."""
    jobs = workflow.get("jobs") or {}
    matched = [(int(m.group(1)), name) for name in jobs if (m := STAGE_JOB.match(name))]
    return tuple(name for _, name in sorted(matched))


def publish_allowed(
    *,
    stages: list[str],
    precedence: str,
    transaction_resolved: bool,
    is_fork_pull_request: bool,
) -> bool:
    """Whether the consumer OCI tag may move for this run.

    ``stages`` is the sequence of rebuild-wave results, ``precedence`` the
    precedence-check result, ``transaction_resolved`` whether the Hummingbird-only
    consumer transaction validated, and ``is_fork_pull_request`` whether a fork
    pull request -- whose read-only token cannot push -- is publishing.
    """
    if is_fork_pull_request:
        return False
    if not transaction_resolved:
        return False
    if precedence != "success":
        return False
    # A wave with no packages is skipped, which is fine; a failed wave is not.
    return all(result in ("success", "skipped") for result in stages)


def _normalized(gate: str) -> str:
    return re.sub(r"\s+", " ", gate).strip()


def assert_gate_enforced(workflow: dict) -> None:
    """The publish job must encode exactly the gate ``publish_allowed`` models."""
    try:
        publish = workflow["jobs"]["publish"]
    except (KeyError, TypeError) as error:
        raise AssertionError("rebuild-rpms.yml has no publish job") from error

    gate = _normalized(str(publish.get("if", "")))

    stages = rebuild_stages(workflow)
    if not stages:
        raise AssertionError("rebuild-rpms.yml declares no rebuild waves")
    if stages != STAGES:
        raise AssertionError(
            "rebuild-rpms.yml declares waves "
            f"{list(stages)}, but publish_gate.STAGES says {list(STAGES)}; "
            "update STAGES and the publish job's if: together"
        )

    needs = publish.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    missing = [stage for stage in stages if stage not in needs]
    if missing:
        raise AssertionError(
            f"publish job must depend on every rebuild wave; missing {missing}"
        )

    # Each rebuild wave may pass or be empty, but a failed wave must not publish.
    for stage in stages:
        clause = (
            f"needs.{stage}.result == 'success' || "
            f"needs.{stage}.result == 'skipped'"
        )
        if clause not in gate:
            raise AssertionError(
                f"publish gate must allow {stage} only on success or skip"
            )
        if f"needs.{stage}.result == 'failure'" in gate:
            raise AssertionError(f"publish gate must never publish when {stage} fails")

    # A precedence failure must never publish.
    if "needs.precedence.result == 'success'" not in gate:
        raise AssertionError("publish gate must require precedence to succeed")

    # Pull requests do not trigger this expensive workflow. If that ever
    # changes, the publish gate must again explicitly exclude fork PRs, whose
    # read-only token cannot push.
    triggers = workflow.get("on", workflow.get(True, {}))
    if (
        "pull_request" in triggers
        and "pull_request.head.repo.full_name" not in gate
    ):
        raise AssertionError("publish gate must exclude fork pull requests")

    _assert_transaction_before_publish(publish)


def _assert_transaction_before_publish(publish: dict) -> None:
    steps = publish.get("steps", [])
    names = [str(step.get("name", "")) for step in steps]
    try:
        validate = next(
            i for i, name in enumerate(names)
            if "Hummingbird-only consumer transaction" in name
        )
    except StopIteration:
        raise AssertionError(
            "publish job must validate the Hummingbird-only consumer transaction"
        ) from None
    try:
        publish_step = next(
            i for i, name in enumerate(names)
            if "Publish the repository as an OCI image" in name
        )
    except StopIteration:
        raise AssertionError(
            "publish job must publish the repository as an OCI image"
        ) from None
    if not validate < publish_step:
        raise AssertionError(
            "the Hummingbird-only transaction must validate before the image publishes"
        )
    # The validation runs whenever the publish job runs and its non-zero exit
    # fails the job before the image step; it must not be skippable.
    if "if" in steps[validate]:
        raise AssertionError("the transaction validation must not be skippable")


def load_workflow(path: Path = REBUILD_WORKFLOW) -> dict:
    with path.open() as handle:
        return yaml.safe_load(handle)


def main() -> int:
    workflow = load_workflow()
    assert_gate_enforced(workflow)
    print("publish gate enforced: every rebuild wave and precedence must pass, "
          "the Hummingbird-only transaction validates before the image publishes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
