from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orderwitness import OrderWitnessError, audit, collect, ddmin  # noqa: E402


def write_project(directory: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    return directory


POSITIVE = {
    "test_a_clean.py": """
        import os

        def test_clean():
            assert os.environ.get("OWW_LEAK") != "1"
    """,
    "test_b_dirty.py": """
        import os

        def test_dirty():
            os.environ["OWW_LEAK"] = "1"
    """,
}


class DdminTests(unittest.TestCase):
    def test_shrinks_to_polluter_victim_pair(self):
        # The predicate only needs P somewhere before V; everything else is noise.
        order = ["A", "P", "B", "C", "V", "D", "E"]

        def fails(seq):
            return "P" in seq and "V" in seq and seq.index("P") < seq.index("V")

        minimal, calls = ddmin(order, fails)
        self.assertEqual(minimal, ["P", "V"])
        self.assertGreater(calls, 0)

    def test_keeps_original_when_predicate_is_false(self):
        minimal, calls = ddmin(["A", "B"], lambda seq: False)
        self.assertEqual(minimal, ["A", "B"])
        self.assertEqual(calls, 1)

    def test_result_is_one_minimal(self):
        order = ["X", "P", "Y", "V", "Z"]

        def fails(seq):
            return "P" in seq and "V" in seq and seq.index("P") < seq.index("V")

        minimal, _ = ddmin(order, fails)
        self.assertTrue(fails(minimal))
        for index in range(len(minimal)):
            self.assertFalse(fails(minimal[:index] + minimal[index + 1:]))


class CollectTests(unittest.TestCase):
    def test_collects_in_file_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(Path(tmp), POSITIVE)
            ids = collect(Path(tmp))
            self.assertEqual(
                ids,
                ["test_a_clean.py::test_clean", "test_b_dirty.py::test_dirty"],
            )

    def test_collection_error_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(Path(tmp), {"test_broken.py": "def test(:\n    pass\n"})
            with self.assertRaises(OrderWitnessError):
                collect(Path(tmp))


class ParametrizedIdTests(unittest.TestCase):
    def test_ids_containing_spaces_survive_collection(self):
        project = {
            "test_param.py": """
                import pytest

                @pytest.mark.parametrize(
                    "value,expected",
                    [("a=1 b=2", 1), ("x y", 2)],
                    ids=["space id", "a=1 b=2"],
                )
                def test_param(value, expected):
                    assert expected in (1, 2)
            """
        }
        with tempfile.TemporaryDirectory() as tmp:
            write_project(Path(tmp), project)
            ids = collect(Path(tmp))
            self.assertEqual(len(ids), 2)
            self.assertTrue(any("space id" in node for node in ids), ids)
            self.assertTrue(any("a=1 b=2" in node for node in ids), ids)
            report = audit(Path(tmp), shuffles=2, seed=0)
            self.assertEqual(report.baseline, "pass")


class AuditTests(unittest.TestCase):
    def test_finds_verified_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(Path(tmp), POSITIVE)
            report = audit(Path(tmp), shuffles=4, seed=0)
            self.assertIsNotNone(report.witness)
            witness = report.witness
            self.assertEqual(witness["kind"], "pair")
            self.assertTrue(witness["pair_verified"])
            self.assertTrue(witness["polluter"].endswith("test_dirty"))
            self.assertTrue(witness["victim"].endswith("test_clean"))
            self.assertGreaterEqual(report.runs, 5)

    def test_independent_suite_has_no_witness(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(
                Path(tmp),
                {
                    "test_one.py": "def test_one():\n    assert 1 + 1 == 2\n",
                    "test_two.py": "def test_two():\n    assert len('ab') == 2\n",
                    "test_three.py": "def test_three():\n    assert sorted([2, 1]) == [1, 2]\n",
                },
            )
            report = audit(Path(tmp), shuffles=4, seed=7)
            self.assertIsNone(report.witness)
            self.assertEqual(report.failing_seeds, [])

    def test_red_baseline_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(
                Path(tmp),
                {
                    "test_red.py": "def test_red():\n    assert False, 'always red'\n",
                    "test_ok.py": "def test_ok():\n    assert True\n",
                },
            )
            with self.assertRaises(OrderWitnessError):
                audit(Path(tmp), shuffles=2, seed=0)


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-m", "orderwitness", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

    def test_cli_reports_pair_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(Path(tmp), POSITIVE)
            proc = self.run_cli("--root", tmp, "--shuffles", "4", "--seed", "0")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["witness"]["kind"], "pair")
            self.assertIn("test_b_dirty.py::test_dirty", payload["witness"]["repro"])
            self.assertIn("test_a_clean.py::test_clean", payload["witness"]["repro"])

    def test_cli_negative_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(Path(tmp), {"test_ok.py": "def test_ok():\n    assert True\n"})
            proc = self.run_cli("--root", tmp, "--shuffles", "2")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertIsNone(payload["witness"])

    def test_cli_refuses_red_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(
                Path(tmp),
                {"test_red.py": "def test_red():\n    assert False, 'always red'\n"},
            )
            proc = self.run_cli("--root", tmp, "--shuffles", "2")
            self.assertEqual(proc.returncode, 2)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertIn("baseline", payload["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)