#!/usr/bin/env python3
"""Coverage for tools/check_suppressed_tests.py, run by tools/validate.py.

The gate exists because one `%global tests_nonfatal 1` would have closed
issue #132 by shipping a PipeWire with a hanging test in it. These tests fix
both halves of that: a new definition fails, and the one inherited exception
cannot outlive the recipe that earned it.
"""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import check_suppressed_tests
from tools.check_suppressed_tests import INHERITED, main, offenders, stale


# The shape every Fedora audio %check uses. The guard reads tests_nonfatal;
# it never defines it, so it must not be mistaken for one.
GUARDED_CHECK = (
    "%check\n"
    "%meson_test || TESTS_ERROR=$?\n"
    'if [ "${TESTS_ERROR}" != "" ]; then\n'
    'echo "test failed"\n'
    "%{!?tests_nonfatal:exit $TESTS_ERROR}\n"
    "fi\n"
)


class SuppressedTestsTests(unittest.TestCase):
    def recipe(self, root: Path, package: str, body: str) -> Path:
        directory = root / "packages" / package
        directory.mkdir(parents=True)
        path = directory / f"{package}.spec"
        path.write_text(body)
        return path

    def test_guard_alone_is_not_a_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recipe(root, "pipewire", GUARDED_CHECK)
            self.assertEqual(offenders(root), [])

    def test_global_and_define_both_caught(self) -> None:
        for macro in ("%global", "%define"):
            with self.subTest(macro=macro):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.recipe(
                        root,
                        "pipewire",
                        f"{macro} tests_nonfatal 1\n" + GUARDED_CHECK,
                    )
                    found = offenders(root)
                    self.assertEqual(
                        [(package, number) for package, number, _ in found],
                        [("pipewire", 1)],
                    )

    def test_indented_definition_is_caught(self) -> None:
        # Fedora indents these inside %if blocks; leading space must not hide
        # the definition from the gate.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recipe(
                root,
                "pipewire",
                "%ifarch %{ix86}\n  %global tests_nonfatal 1\n%endif\n",
            )
            self.assertEqual([p for p, _, _ in offenders(root)], ["pipewire"])

    def test_inherited_package_is_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recipe(root, "pulseaudio", "%global tests_nonfatal 1\n")
            self.assertEqual(offenders(root), [])
            self.assertEqual(stale(root), [])

    def test_inherited_entry_that_no_longer_applies_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recipe(root, "pulseaudio", GUARDED_CHECK)
            self.assertEqual(
                stale(root), ["pulseaudio: no longer defines tests_nonfatal"]
            )

    def test_an_absent_recipe_is_not_stale(self) -> None:
        # validate.py runs against synthetic trees, so "the allowlisted recipe
        # is not here" must not be an error. Deletion is caught by
        # test_every_inherited_entry_still_exists instead.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "packages").mkdir()
            self.assertEqual(stale(root), [])
            self.assertEqual(main(root), 0)

    def test_every_inherited_entry_still_exists(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for package in INHERITED:
            with self.subTest(package=package):
                self.assertTrue(
                    sorted((root / "packages" / package).glob("*.spec")),
                    f"{package} is allowlisted but has no recipe; drop the entry",
                )

    def test_main_fails_on_a_new_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recipe(root, "pulseaudio", "%global tests_nonfatal 1\n")
            self.recipe(root, "pipewire", "%global tests_nonfatal 1\n")
            self.assertEqual(main(root), 1)

    def test_main_fails_on_a_stale_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recipe(root, "pulseaudio", GUARDED_CHECK)
            self.assertEqual(main(root), 1)

    def test_main_passes_a_clean_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recipe(root, "pipewire", GUARDED_CHECK)
            with mock.patch.dict(check_suppressed_tests.INHERITED, clear=True):
                self.assertEqual(main(root), 0)

    def test_the_real_tree_passes_and_pulseaudio_is_why_the_list_exists(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(main(root), 0)
        self.assertIn("pulseaudio", INHERITED)


if __name__ == "__main__":
    unittest.main()
