#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.buildroot_lock import buildroot, compare, load_lock, main as buildroot_main
from tools.factory_manifest import main as manifest_main, source_verification
from tools.source_pipeline import main as source_pipeline_main


class BuildrootLockTests(unittest.TestCase):
    def setUp(self) -> None:
        # cmd_snapshot still reads BUILDROOT_DIGEST when no --digest is given,
        # and the prepare job exports one for the whole job, so strip it to see
        # only what each test passes. BUILDROOT_IMAGE and ACTUAL_BUILDROOT_IMAGE
        # are deliberately left alone: they are no longer read at all, and
        # test_cmd_snapshot_ignores_the_workflows_buildroot_image_variable
        # exports one to prove it.
        isolated = {
            key: value
            for key, value in os.environ.items()
            if key != "BUILDROOT_DIGEST"
        }
        patcher = patch.dict(os.environ, isolated, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_compare_detects_missing_and_unexpected_packages(self) -> None:
        expected = [
            {"nevra": "glibc-2.41-1.fc44.x86_64"},
            {"nevra": "openssl-libs-3.5.7-1.hum1.x86_64"},
        ]
        actual = [
            {"nevra": "glibc-2.41-1.fc44.x86_64"},
            {"nevra": "bash-5.2-1.fc44.x86_64"},
        ]
        errors = compare(expected, actual)
        self.assertTrue(any("missing locked buildroot packages" in e for e in errors))
        self.assertTrue(any("unexpected buildroot packages" in e for e in errors))

    def test_load_lock_requires_digest_pins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "bad": {"image": "quay.io/fedora/fedora:44", "packages": []}
                    },
                })
            )
            with self.assertRaises(ValueError):
                load_lock(lock)

    def test_buildroot_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": "quay.io/fedora/fedora:44@sha256:" + "a" * 64,
                            "packages": [],
                        }
                    },
                })
            )
            data = load_lock(lock)
            spec = buildroot(data, "fedora-44")
            self.assertEqual(spec["image"], "quay.io/fedora/fedora:44@sha256:" + "a" * 64)
            with self.assertRaises(ValueError):
                buildroot(data, "nonexistent")

    def test_cmd_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": "quay.io/fedora/fedora:44@sha256:" + "b" * 64,
                            "packages": [],
                        }
                    },
                })
            )
            with patch("sys.stdout", new=io.StringIO()) as fake_out:
                rc = buildroot_main(["--config", str(lock), "image", "fedora-44"])
                self.assertEqual(rc, 0)
                self.assertIn("quay.io/fedora/fedora:44@sha256:" + "b" * 64, fake_out.getvalue())

    def test_cmd_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": "quay.io/fedora/fedora:44@sha256:" + "c" * 64,
                            "packages": [{"nevra": "pkg-1.0-1.x86_64"}],
                        }
                    },
                })
            )
            out = Path(directory) / "snapshot.json"
            fake_pkgs = [{"name": "pkg", "evra": "1.0-1", "arch": "x86_64", "nevra": "pkg-1.0-1.x86_64"}]
            with patch("tools.buildroot_lock.rpm_packages", return_value=fake_pkgs):
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--digest", "sha256:" + "c" * 64,
                    "--output", str(out),
                    "--strict",
                ])
                self.assertEqual(rc, 0)
                data = json.loads(out.read_text())
                self.assertEqual(data["schema"], 1)
                self.assertEqual(data["name"], "fedora-44")
                self.assertEqual(data["packages"], fake_pkgs)

    def test_cmd_snapshot_records_null_image_when_no_digest_was_resolved(self) -> None:
        # An empty BUILDROOT_DIGEST used to fall through to the lock's own
        # digest, so the snapshot attested exactly the provenance it exists to
        # check. Now nothing resolved means image null, with a warning; the
        # lock stays in locked_image where it belongs.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "lock.json"
            locked_img = "quay.io/fedora/fedora:44@sha256:" + "c" * 64
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {"fedora-44": {"image": locked_img, "packages": []}},
                })
            )
            inventory = root / "buildroot.rpms"
            inventory.write_text("glibc\t2.41-1.fc44\tx86_64\n")
            out = root / "snapshot.json"
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--packages-from", str(inventory),
                    "--digest", "",
                    "--output", str(out),
                ])
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text())
            self.assertIsNone(data["image"])
            self.assertEqual(data["locked_image"], locked_img)
            self.assertIn("buildroot image unknown", stderr.getvalue())
            self.assertNotIn(locked_img, json.dumps(data["image"]))

    def test_cmd_snapshot_ignores_the_workflows_buildroot_image_variable(self) -> None:
        # The prepare job exports BUILDROOT_IMAGE for the whole job, and
        # validate.py forces that variable to equal the lock's digest. Reading
        # it as the resolved image would re-assert the lock's own pin through a
        # second door, so it is not read: running inside that environment with
        # nothing resolved still records null.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "lock.json"
            locked_img = "quay.io/fedora/fedora:44@sha256:" + "c" * 64
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {"fedora-44": {"image": locked_img, "packages": []}},
                })
            )
            inventory = root / "buildroot.rpms"
            inventory.write_text("glibc\t2.41-1.fc44\tx86_64\n")
            out = root / "snapshot.json"
            stderr = io.StringIO()
            env = {
                "BUILDROOT_IMAGE": locked_img,
                "ACTUAL_BUILDROOT_IMAGE": "quay.io/fedora/fedora:44@sha256:" + "d" * 64,
            }
            with patch.dict(os.environ, env), patch("sys.stderr", stderr):
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--packages-from", str(inventory),
                    "--output", str(out),
                ])
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text())
            self.assertIsNone(data["image"])
            self.assertEqual(data["locked_image"], locked_img)
            self.assertIn("buildroot image unknown", stderr.getvalue())

    def test_cmd_snapshot_strict_refuses_an_unknown_image(self) -> None:
        # --strict is the operator asking whether the root is exactly what was
        # locked. With no resolved image that question has no answer, so it
        # fails rather than passing on the lock's own digest.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": "quay.io/fedora/fedora:44@sha256:" + "c" * 64,
                            "packages": [],
                        }
                    },
                })
            )
            inventory = root / "buildroot.rpms"
            inventory.write_text("glibc\t2.41-1.fc44\tx86_64\n")
            out = root / "snapshot.json"
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--packages-from", str(inventory),
                    "--output", str(out),
                    "--strict",
                ])
            self.assertEqual(rc, 1)
            self.assertIn("buildroot image unknown", stderr.getvalue())
            # The snapshot is still written, with the honest null.
            self.assertIsNone(json.loads(out.read_text())["image"])

    def test_cmd_snapshot_strict_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": "quay.io/fedora/fedora:44@sha256:" + "c" * 64,
                            "packages": [{"nevra": "pkg-1.0-1.x86_64"}],
                        }
                    },
                })
            )
            out = Path(directory) / "snapshot.json"
            fake_pkgs = [{"name": "other", "evra": "2.0-1", "arch": "x86_64", "nevra": "other-2.0-1.x86_64"}]
            with patch("tools.buildroot_lock.rpm_packages", return_value=fake_pkgs):
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--output", str(out),
                    "--strict",
                ])
                self.assertEqual(rc, 1)

    def test_cmd_snapshot_records_actual_image_and_strict_image_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.json"
            locked_img = "quay.io/fedora/fedora:44@sha256:" + "c" * 64
            actual_img = "quay.io/fedora/fedora:44@sha256:" + "d" * 64
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": locked_img,
                            "packages": [],
                        }
                    },
                })
            )
            out = Path(directory) / "snapshot.json"
            with patch("tools.buildroot_lock.rpm_packages", return_value=[]):
                # Non-strict allows actual image differing from locked image and records both
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--image", actual_img,
                    "--output", str(out),
                ])
                self.assertEqual(rc, 0)
                data = json.loads(out.read_text())
                self.assertEqual(data["image"], actual_img)
                self.assertEqual(data["locked_image"], locked_img)

                # Strict rejects when actual image != locked image
                rc_strict = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--image", actual_img,
                    "--output", str(out),
                    "--strict",
                ])
                self.assertEqual(rc_strict, 1)


    def test_cmd_snapshot_reads_an_inventory_printed_by_the_build_root(self) -> None:
        # CI runs rpm inside the root and assembles the snapshot out here, so
        # the root needs no interpreter. This is that path end to end.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "lock.json"
            locked_img = "quay.io/fedora/fedora:44@sha256:" + "c" * 64
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {"fedora-44": {"image": locked_img, "packages": []}},
                })
            )
            inventory = root / "buildroot.rpms"
            inventory.write_text(
                "openssl-libs\t1:3.5.7-1.hum1\tx86_64\n"
                "glibc\t2.41-1.fc44\tx86_64\n"
                "\n"
                "truncated\tline\n"
            )
            out = root / "snapshot.json"
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--packages-from", str(inventory),
                    "--digest", "sha256:" + "c" * 64,
                    "--output", str(out),
                ])
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text())
            # Sorted by name, epoch kept in the NEVRA, malformed line dropped.
            self.assertEqual(
                [package["nevra"] for package in data["packages"]],
                ["glibc-2.41-1.fc44.x86_64", "openssl-libs-1:3.5.7-1.hum1.x86_64"],
            )
            # Dropped, but not silently: the snapshot is an attestation input,
            # so an inventory it could not fully read has to say so.
            self.assertIn("under-reports the root's contents", stderr.getvalue())
            self.assertIn("'truncated\\tline'", stderr.getvalue())
            # The digest the run resolved, rebuilt into a full reference.
            self.assertEqual(data["image"], locked_img)
            self.assertEqual(data["locked_image"], locked_img)

    def test_cmd_snapshot_strict_refuses_an_inventory_it_could_not_fully_parse(
        self,
    ) -> None:
        # --strict asks "is this root exactly what was locked?". A capture with
        # unparsable lines cannot answer that: the missing packages might be
        # the divergence. Non-strict still warns and proceeds -- the scheduled
        # rebuild records what it saw -- but strict must refuse.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "lock.json"
            locked_img = "quay.io/fedora/fedora:44@sha256:" + "c" * 64
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": locked_img,
                            "packages": [{"nevra": "glibc-2.41-1.fc44.x86_64"}],
                        }
                    },
                })
            )
            inventory = root / "buildroot.rpms"
            inventory.write_text("glibc\t2.41-1.fc44\tx86_64\ntruncated\tline\n")
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                rc = buildroot_main([
                    "--config", str(lock),
                    "snapshot", "fedora-44",
                    "--packages-from", str(inventory),
                    "--digest", "sha256:" + "c" * 64,
                    "--output", str(root / "snapshot.json"),
                    "--strict",
                ])
            self.assertEqual(rc, 1)
            self.assertIn("buildroot inventory incomplete", stderr.getvalue())

    def test_compare_reports_a_lock_entry_without_a_nevra_instead_of_crashing(
        self,
    ) -> None:
        # load_lock only checks that `packages` is a list; the per-entry nevra
        # requirement lives in tools/validate.py. Running this module
        # standalone against a hand-edited lock therefore reaches compare()
        # with None in the expected set, where it used to die inside
        # ", ".join(...) with a TypeError. The gate failed either way, but a
        # traceback does not tell an operator which entry to fix.
        errors = compare(
            [{"nevra": "glibc-2.41-1.fc44.x86_64"}, {"name": "openssl-libs"}, {}],
            [{"nevra": "glibc-2.41-1.fc44.x86_64"}],
        )
        self.assertTrue(any("malformed lock" in e for e in errors))
        self.assertTrue(any("2 locked package entries" in e for e in errors))
        # The malformed entries must not be reported as missing packages --
        # they are a lock defect, not a divergence in the build root.
        self.assertFalse(any("missing locked buildroot packages" in e for e in errors))

    def test_cmd_snapshot_fails_when_the_inventory_is_absent(self) -> None:
        # A build root that printed nothing must not produce an empty snapshot
        # that reads as a root with no packages in it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": "quay.io/fedora/fedora:44@sha256:" + "c" * 64,
                            "packages": [],
                        }
                    },
                })
            )
            rc = buildroot_main([
                "--config", str(lock),
                "snapshot", "fedora-44",
                "--packages-from", str(root / "absent.rpms"),
                "--output", str(root / "snapshot.json"),
            ])
            self.assertEqual(rc, 1)
            self.assertFalse((root / "snapshot.json").exists())


class FactoryManifestTests(unittest.TestCase):
    def test_manifest_aggregates_sources_buildroots_and_oci(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "reports").mkdir(parents=True)
            (repo / "x86_64").mkdir(parents=True)
            (repo / "x86_64" / "demo-1.0-1.hum1.bfin.x86_64.rpm").write_bytes(b"rpm")
            (repo / "reports" / "demo.json").write_text(
                json.dumps({
                    "package": "demo",
                    "result": "accepted",
                    "verification": "sha512",
                    "checksum_only": True,
                })
            )
            (repo / "reports" / "buildroot-demo.json").write_text(
                json.dumps({"schema": 1, "name": "fedora-44", "packages": [{"nevra": "glibc-2.41.x86_64"}]})
            )
            # Unrelated json published beside repo should be ignored
            (repo / "unrelated.json").write_text(json.dumps({"arbitrary": "data"}))
            lock = root / "lock.json"
            lock.write_text(
                json.dumps({
                    "schema": 1,
                    "buildroots": {
                        "fedora-44": {
                            "image": "quay.io/fedora/fedora:44@sha256:abc",
                            "packages": [],
                        }
                    },
                })
            )
            out = root / "manifest.json"
            rc = manifest_main([
                "--repository", str(repo),
                "--build-list", '["demo"]',
                "--buildroot-lock", str(lock),
                "--output", str(out),
                "--oci-ref", "ghcr.io/demo/utah-packages:latest",
                "--oci-digest", "sha256:123456",
            ])
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text())
            self.assertEqual(data["schema"], 1)
            self.assertEqual(data["requested_packages"], ["demo"])
            self.assertEqual(data["packages"], ["x86_64/demo-1.0-1.hum1.bfin.x86_64.rpm"])
            self.assertEqual(data["oci"]["digest"], "sha256:123456")
            self.assertEqual(len(data["sources"]), 1)
            self.assertEqual(len(data["buildroots"]), 1)
            self.assertEqual(data["source_verification"]["counts"], {"sha512": 1})
            self.assertEqual(data["source_verification"]["checksum_only"], ["demo"])


    def test_source_verification_names_the_checksum_only_exceptions(self) -> None:
        # "Verified" and "checksummed" are different claims. The summary counts
        # both and names the packages upstream gave no signature for, so the
        # exception list is readable without walking every source report.
        summary = source_verification([
            {"package": "signed", "result": "accepted", "verification": "signature", "checksum_only": False},
            {"package": "hashed", "result": "accepted", "verification": "sha512", "checksum_only": True},
            {"package": "manifest", "result": "accepted", "verification": "sha256-manifest", "checksum_only": True},
            {"package": "rejected", "result": "rejected", "verification": "sha512", "checksum_only": True},
        ])
        self.assertEqual(
            summary["counts"], {"sha256-manifest": 1, "sha512": 1, "signature": 1}
        )
        self.assertEqual(summary["checksum_only"], ["hashed", "manifest"])

    def test_source_verification_treats_an_unlabelled_report_as_unverified(self) -> None:
        # A report written before this field existed, or by a path that forgot
        # it, must not read as a signature it never had.
        summary = source_verification([{"package": "old", "result": "accepted"}])
        self.assertEqual(summary["counts"], {"unknown": 1})
        self.assertEqual(summary["checksum_only"], ["old"])


class SourcePipelineSignatureTests(unittest.TestCase):
    def test_records_signature_verification_and_checksum_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages_dir = root / "packages" / "demo"
            packages_dir.mkdir(parents=True)
            config = root / "sources.json"
            content = b"sample content for testing"
            import hashlib
            sha512_val = hashlib.sha512(content).hexdigest()

            # Case 1: with signature_url
            config.write_text(
                json.dumps({
                    "packages": [{
                        "name": "demo",
                        "url": "https://example.com/demo.tar.gz",
                        "sha512": sha512_val,
                        "signature_url": "https://example.com/demo.tar.gz.asc",
                    }]
                })
            )
            reports_dir = root / "reports"
            with patch("tools.source_pipeline.fetch_with_fallbacks", return_value="https://example.com/demo.tar.gz"), \
                 patch("tools.source_pipeline.digest", side_effect=[sha512_val]), \
                 patch("tools.source_pipeline.verify_signature"), \
                 patch("tools.source_pipeline.bundled_sources", return_value=[]), \
                 patch("sys.argv", ["source_pipeline", "demo", "--config", str(config), "--output", str(root / "sources"), "--report-dir", str(reports_dir)]):
                # We need candidate file to exist for digest & replace
                target_dir = root / "sources" / "demo"
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "demo.tar.gz.candidate").write_bytes(content)
                rc = source_pipeline_main()
                self.assertEqual(rc, 0)
                report = json.loads((reports_dir / "demo.json").read_text())
                self.assertEqual(report["verification"], "signature")
                self.assertFalse(report["checksum_only"])

            # Case 2: checksum only (no signature_url)
            config.write_text(
                json.dumps({
                    "packages": [{
                        "name": "demo",
                        "url": "https://example.com/demo.tar.gz",
                        "sha512": sha512_val,
                    }]
                })
            )
            with patch("tools.source_pipeline.fetch_with_fallbacks", return_value="https://example.com/demo.tar.gz"), \
                 patch("tools.source_pipeline.digest", side_effect=[sha512_val]), \
                 patch("tools.source_pipeline.verify_signature"), \
                 patch("tools.source_pipeline.bundled_sources", return_value=[]), \
                 patch("sys.argv", ["source_pipeline", "demo", "--config", str(config), "--output", str(root / "sources"), "--report-dir", str(reports_dir)]):
                (target_dir / "demo.tar.gz.candidate").write_bytes(content)
                rc = source_pipeline_main()
                self.assertEqual(rc, 0)
                report = json.loads((reports_dir / "demo.json").read_text())
                self.assertEqual(report["verification"], "sha512")
                self.assertTrue(report["checksum_only"])


if __name__ == "__main__":
    unittest.main()
