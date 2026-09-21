---
name: supply-chain-provenance
description: >-
  Buildroot locking, live package NEVRA snapshots, recipe provenance
  validation, and the factory manifest published beside the OCI digest.
metadata:
  type: procedure
---

# Supply chain provenance and buildroot locking

A factory rebuild is only reviewable if it can say what it consumed. Three
inputs decide the output — the recipe, the source archive, and the build root —
and each of them used to be described by something mutable. This is what pins
them and what the run publishes about them.

## The buildroot lock

[`config/buildroot-lock.json`](../../config/buildroot-lock.json) is the
declarative record of which image a buildroot is, pinned by digest. It carries
`fedora-44` because that is the only root the rebuild runs; entries nothing
consumes are decoration, so do not add one ahead of its caller.

`tools/buildroot_lock.py`:

- `image <name>` prints the locked reference, for inspecting the lock without
  parsing it.
- `snapshot <name> --output <path>` writes every package NEVRA in the root,
  the digest the run resolved (`image`), and the digest the lock expected
  (`locked_image`). `--packages-from <file>` reads `rpm -qa` output captured
  elsewhere instead of running `rpm` here. The lock is never used as the
  resolved image: with no `--image`, `--digest` or `BUILDROOT_DIGEST` the
  snapshot records `image: null` and warns, so an empty digest cannot quietly
  re-assert the lock's provenance. Those three are the *only* inputs — in
  particular the job-wide `BUILDROOT_IMAGE` is not read, because
  `tools/validate.py` forces it to equal the lock's digest and reading it would
  re-assert that pin through a second door.
- `--strict` turns a digest mismatch, and any divergence from a non-empty
  locked package list, into a failure.
- An inventory line that is not three tab-separated fields is dropped, but
  never silently: `parse_packages` warns on stderr naming the dropped lines,
  and `--strict` refuses the snapshot outright. A truncated or corrupted
  `rpm -qa` capture makes the snapshot under-report the root's contents, and a
  snapshot that under-reports cannot answer the question `--strict` asks — the
  packages it could not read might be the divergence.
- A lock entry with no `nevra` is reported as a malformed lock, not as a
  missing package. `load_lock` only checks that `packages` is a list (the
  per-entry requirement lives in `tools/validate.py`), so running this module
  standalone against a hand-edited lock can reach the comparison with an
  unusable entry; it names the defect instead of raising.

**The rebuild does not pass `--strict`, deliberately.**
`quay.io/fedora/fedora:44` is republished several times a day and each previous
digest is garbage-collected, so failing on a moved tag recreates the outage
[`repeated-mistakes.md`](repeated-mistakes.md) section 7 records — thirty-seven
jobs dead mid-run on a pin that was correct when the run started. The rebuild
records the drift instead; `--strict` is for an operator asking whether a root
is exactly what was locked.

**The digest is written twice** — in the lock and in `BUILDROOT_IMAGE` in
`.github/workflows/rebuild-rpms.yml` — because Renovate tracks the workflow
shape and the manifest needs the declarative record. Two copies of one digest
drift, so:

- `renovate.json`'s `build-root-image` manager covers both files, and moves
  them in one pull request.
- `tools/validate.py` fails `just check` when the workflows do not pin a
  locked buildroot to exactly what the lock names. The comparison is per
  *repository*, not per `repo:tag`, so all three ways the copies part company
  fail: a different digest, a pin retagged to `fedora:45` while the lock says
  `fedora:44`, and a pin deleted outright. A locked buildroot no workflow pins
  at all is drift, not an absence of evidence.

Do not resolve that duplication by deleting either copy without moving what
depends on it: `tests/test_renovate_coverage.py` requires every workflow image
pin to be tracked, and the lock is what the manifest publishes.

## The snapshot runs on the runner

`rpm -qa` runs inside the build root; the JSON is assembled outside it. The
root is a Fedora container that carries no interpreter this factory may assume,
and installing one so the root can inventory itself would change the set being
inventoried. Extend `parse_packages`, not the container.

## Recipe provenance

Every recipe in `packages/<name>/` carries `.hummingbird-upstream.json`, and
`tools/validate.py` rejects the tree without it:

- **`branch: rawhide`** — the Fedora dist-git snapshot, pinned by full
  40-character `commit` and `tree` SHAs. An abbreviated SHA is not a pin: it
  is ambiguous and it is not what `git` resolved at import.
- **`branch: upstream`** — imported from the project's own release repository,
  so it carries a non-empty `remote` and no dist-git `commit`/`tree`. Its
  source lock lives in `config/upstream-sources.json` instead.

Do not hand-edit these files. Re-import; editing provenance makes a recipe
claim an origin it does not have.

## Source verification reporting

`tools/source_pipeline.py` records how each accepted source was verified:

| `verification` | `checksum_only` | Meaning |
| --- | --- | --- |
| `signature` | `false` | Upstream supplied a signature and it verified |
| `sha256-manifest` | `true` | Upstream published checksums, not a signature |
| `sha512` | `true` | Only the locked SHA-512 stands behind the bytes |

A checksum is not a signature. `tools/factory_manifest.py` counts each kind and
names the packages in the checksum-only classes under `source_verification`, so
the exceptions are a list to shorten rather than a property to grep for.

## The manifest

`tools/factory_manifest.py` writes `manifest.json` beside the repository: the
built RPMs, the source reports and their verification summary, the buildroot
snapshots, the lock itself, and the published OCI reference and digest. The
publish job writes it twice — once before the container build, once after, when
the OCI digest exists — and uploads it as the `factory-manifest` artifact.

Reports are classified by shape, not by filename: a buildroot snapshot is
`schema`+`name`+`packages`, a source report carries `package`. Unrelated JSON
published beside the repository is not folded in.
