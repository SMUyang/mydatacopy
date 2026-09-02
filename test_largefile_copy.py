from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from largefile_copy import checker_process_scope


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "largefile_copy.py"


class LargefileCopyCliTests(unittest.TestCase):
    def run_cli(self, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )

    def paths(self, temporary: Path) -> tuple[Path, Path, Path]:
        source = temporary / "source"
        destination = temporary / "destination"
        checkpoint = temporary / "run.checkpoint.jsonl"
        source.mkdir()
        return source, destination, checkpoint

    def test_source_and_destination_are_required_and_help_lists_new_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            help_result = self.run_cli("--help", cwd=temporary)
            missing_result = self.run_cli(cwd=temporary)

        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--source", help_result.stdout)
        self.assertIn("--destination", help_result.stdout)
        self.assertEqual(missing_result.returncode, 2)

    def test_repairs_missing_file_and_accepts_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination, checkpoint = self.paths(Path(directory))
            (source / "nested").mkdir()
            (source / "nested" / "large.bin").write_bytes(b"source-content")

            result = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                "--workers", "2",
                cwd=source.parent,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / "nested" / "large.bin").read_bytes(), b"source-content")
            self.assertIn("workers=2", result.stdout)
    def test_checker_process_scope_recognizes_supported_invocations_only(self) -> None:
        script_name = "largefile_copy.py"
        supported = (
            ["python3", "/opt/tools/largefile_copy.py", "--source", "/source"],
            ["largefile-copy", "--source", "/source"],
            ["python3", "-m", "largefile_copy", "--source", "/source"],
        )
        unsupported = (
            ["bash", "/opt/tools/largefile-copy", "--source", "/source"],
            ["timeout", "60", "largefile-copy", "--source", "/source"],
            ["python3", "-c", "print(1)", "/tmp/largefile_copy.py"],
            ["python3", "--source", "/tmp/largefile_copy.py"],
        )

        for argv in supported:
            self.assertEqual(checker_process_scope(argv, script_name), (True, False))
        for argv in unsupported:
            self.assertEqual(checker_process_scope(argv, script_name), (False, False))


    def test_repairs_mismatched_file_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination, checkpoint = self.paths(Path(directory))
            (source / "file.bin").write_bytes(b"source-content")
            destination.mkdir()
            (destination / "file.bin").write_bytes(b"stale-content")

            first = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                cwd=source.parent,
            )
            second = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                cwd=source.parent,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual((destination / "file.bin").read_bytes(), b"source-content")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("resumed=1", second.stdout)

    def test_check_only_reports_mismatch_without_modifying_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination, checkpoint = self.paths(Path(directory))
            (source / "file.bin").write_bytes(b"source-content")
            destination.mkdir()
            target = destination / "file.bin"
            target.write_bytes(b"stale-content")

            result = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                "--check-only",
                cwd=source.parent,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(target.read_bytes(), b"stale-content")
            self.assertIn("MISMATCH", result.stdout)

    def test_workers_process_two_pending_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination, checkpoint = self.paths(Path(directory))
            (source / "first.bin").write_bytes(b"first")
            (source / "second.bin").write_bytes(b"second")

            result = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                "--workers", "2",
                cwd=source.parent,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("workers=2", result.stdout)
            self.assertIn("pending=2", result.stdout)
            self.assertEqual((destination / "first.bin").read_bytes(), b"first")
            self.assertEqual((destination / "second.bin").read_bytes(), b"second")

    def test_check_only_reports_missing_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination, checkpoint = self.paths(Path(directory))
            (source / "missing.bin").write_bytes(b"content")

            result = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                "--check-only",
                cwd=source.parent,
            )

            self.assertEqual(result.returncode, 1)
            self.assertFalse(destination.exists())
            self.assertIn("MISSING", result.stdout)

    def test_checkpoint_resume_skips_unchanged_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination, checkpoint = self.paths(Path(directory))
            (source / "file.bin").write_bytes(b"content")
            first = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                cwd=source.parent,
            )
            second = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                cwd=source.parent,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("resumed=1", second.stdout)
            self.assertIn("pending=0", second.stdout)

    def test_exclude_dir_is_opt_in_and_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination, checkpoint = self.paths(Path(directory))
            (source / "keep").mkdir()
            (source / "skip").mkdir()
            (source / "normally-included").mkdir()
            (source / "keep" / "file.bin").write_bytes(b"keep")
            (source / "skip" / "file.bin").write_bytes(b"skip")
            (source / "normally-included" / "file.bin").write_bytes(b"included-by-default")

            result = self.run_cli(
                "--source", str(source),
                "--destination", str(destination),
                "--checkpoint", str(checkpoint),
                "--exclude-dir", "skip",
                "--exclude-dir", "other",
                cwd=source.parent,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((destination / "keep" / "file.bin").exists())
            self.assertTrue((destination / "normally-included" / "file.bin").exists())
            self.assertFalse((destination / "skip").exists())


if __name__ == "__main__":
    unittest.main()
