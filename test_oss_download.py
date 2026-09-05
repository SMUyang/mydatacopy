from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import oss_download


ROOT = Path(__file__).resolve().parent


class OssDownloadCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.destination = self.root / "downloads"
        self.fake = self.root / "fake-ossutil"
        self.args_file = self.root / "argv.json"
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "Path = __import__('pathlib').Path\n"
            f"Path({str(self.args_file)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'env': {{k: os.environ.get(k) for k in ('OSS_ACCESS_KEY_ID', 'OSS_ACCESS_KEY_SECRET', 'OSS_REGION')}}}}))\n"
            "raise SystemExit(int(os.environ.get('FAKE_EXIT_CODE', '0')))\n",
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_download_constructs_v2_recursive_parallel_checkpoint_and_filters_command(self) -> None:
        log = self.root / "run.log"
        checkpoint = self.root / "state" / "checkpoints"
        result = oss_download.main(
            [
                "download",
                "--source",
                "oss://bucket/prefix",
                "--destination",
                str(self.destination),
                "--recursive",
                "--force",
                "--update",
                "--jobs",
                "3",
                "--parallel",
                "8",
                "--checkpoint-dir",
                str(checkpoint),
                "--bigfile-threshold",
                "100MB",
                "--part-size",
                "10MB",
                "--include",
                "*.bam",
                "--include",
                "*.bai",
                "--exclude",
                "*.tmp",
                "--maxdownspeed",
                "5MB",
                "--dry-run",
                "--config-file",
                str(self.root / "ossutil.ini"),
                "--profile",
                "work",
                "--ossutil",
                str(self.fake),
                "--log",
                str(log),
            ]
        )
        self.assertEqual(result, 0)
        argv = json.loads(self.args_file.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[:2], ["cp", "-r"])
        self.assertIn("-f", argv)
        self.assertIn("-u", argv)
        self.assertEqual(argv[argv.index("-j") + 1], "3")
        self.assertEqual(argv[argv.index("--parallel") + 1], "8")
        self.assertEqual(argv[argv.index("--checkpoint-dir") + 1], str(checkpoint))
        self.assertNotIn("--snapshot-path", argv)
        self.assertEqual(argv[argv.index("--bigfile-threshold") + 1], "100MB")
        self.assertEqual(argv[argv.index("--part-size") + 1], "10MB")
        self.assertEqual(argv[argv.index("--bandwidth-limit") + 1], "5MB")
        self.assertEqual(argv.count("--include"), 2)
        self.assertEqual(argv.count("--exclude"), 1)
        self.assertIn("--dry-run", argv)
        self.assertEqual(argv[-2:], ["oss://bucket/prefix", str(self.destination)])
        self.assertTrue(checkpoint.is_dir())
        self.assertIn("START", log.read_text(encoding="utf-8"))

    def test_download_requires_source_and_destination(self) -> None:
        with self.assertRaises(SystemExit) as source_error:
            oss_download.main(["download", "--destination", str(self.destination)])
        self.assertEqual(source_error.exception.code, 2)
        with self.assertRaises(SystemExit) as destination_error:
            oss_download.main(["download", "--source", "oss://bucket/object"])
        self.assertEqual(destination_error.exception.code, 2)

    def test_download_rejects_non_oss_source(self) -> None:
        with self.assertRaises(SystemExit) as error:
            oss_download.main(
                [
                    "download",
                    "--source",
                    "https://example.invalid/object",
                    "--destination",
                    str(self.destination),
                    "--ossutil",
                    str(self.fake),
                ]
            )
        self.assertEqual(error.exception.code, 2)

    def test_download_rejects_cloud_destination(self) -> None:
        for destination in ("oss://bucket/object", "s3://bucket/object", "https://example.invalid/object"):
            with self.subTest(destination=destination):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "oss_download.py"),
                        "download",
                        "--source",
                        "oss://bucket/source",
                        "--destination",
                        destination,
                        "--ossutil",
                        str(self.fake),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("local path", result.stderr)

    def test_parallel_requires_positive_integer(self) -> None:
        with self.assertRaises(SystemExit) as error:
            oss_download.main(
                [
                    "download",
                    "--source",
                    "oss://bucket/object",
                    "--destination",
                    str(self.destination),
                    "--parallel",
                    "auto",
                ]
            )
        self.assertEqual(error.exception.code, 2)

    def test_prompted_secret_is_only_in_child_environment_not_argv_or_log(self) -> None:
        log = self.root / "run.log"
        with mock.patch.object(oss_download.getpass, "getpass", return_value="secret-value"), mock.patch(
            "builtins.input", side_effect=["AKID123456", "cn-hangzhou"]
        ):
            result = oss_download.main(
                [
                    "download",
                    "--source",
                    "oss://bucket/object",
                    "--destination",
                    str(self.destination),
                    "--prompt-credentials",
                    "--ossutil",
                    str(self.fake),
                    "--log",
                    str(log),
                ]
            )
        self.assertEqual(result, 0)
        payload = json.loads(self.args_file.read_text(encoding="utf-8"))
        self.assertNotIn("secret-value", payload["argv"])
        self.assertNotIn("AKID123456", " ".join(payload["argv"]))
        self.assertEqual(payload["env"]["OSS_ACCESS_KEY_SECRET"], "secret-value")
        logged = log.read_text(encoding="utf-8")
        self.assertNotIn("secret-value", logged)
        self.assertNotIn("AKID123456", logged)

    def test_configure_passes_config_and_profile_without_reading_secrets(self) -> None:
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(oss_download.subprocess, "run", side_effect=fake_run):
            result = oss_download.main(
                [
                    "configure",
                    "--ossutil",
                    str(self.fake),
                    "--config-file",
                    str(self.root / "config.ini"),
                    "--profile",
                    "personal",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            calls[0][0],
            [
                str(self.fake),
                "config",
                "--config-file",
                str(self.root / "config.ini"),
                "--profile",
                "personal",
            ],
        )
        self.assertNotIn("secret", json.dumps(calls))

    def test_default_checkpoint_path_is_derived_next_to_destination(self) -> None:
        result = oss_download.main(
            [
                "download",
                "--source",
                "oss://bucket/object",
                "--destination",
                str(self.destination),
                "--ossutil",
                str(self.fake),
            ]
        )
        self.assertEqual(result, 0)
        argv = json.loads(self.args_file.read_text(encoding="utf-8"))["argv"]
        state = self.destination.parent / ".oss-download"
        self.assertEqual(argv[argv.index("--checkpoint-dir") + 1], str(state / "checkpoints"))
        self.assertNotIn("--snapshot-path", argv)

    def test_ls_constructs_readonly_recursive_filters_output_versions_config_and_profile_command(self) -> None:
        log = self.root / "ls.log"
        result = oss_download.main(
            [
                "ls",
                "--source",
                "oss://bucket/prefix",
                "--recursive",
                "--include",
                "*.bam",
                "--include",
                "*.bai",
                "--exclude",
                "*.tmp",
                "--output-format",
                "json",
                "--output-query",
                "Contents[?Size > `0`].Key",
                "--all-versions",
                "--config-file",
                str(self.root / "ossutil.ini"),
                "--profile",
                "work",
                "--ossutil",
                str(self.fake),
                "--log",
                str(log),
            ]
        )
        self.assertEqual(result, 0)
        argv = json.loads(self.args_file.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[0], "ls")
        self.assertIn("--recursive", argv)
        self.assertEqual(argv.count("--include"), 2)
        self.assertEqual(argv.count("--exclude"), 1)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--output-query") + 1], "Contents[?Size > `0`].Key")
        self.assertIn("--all-versions", argv)
        self.assertEqual(argv[argv.index("--config-file") + 1], str(self.root / "ossutil.ini"))
        self.assertEqual(argv[argv.index("--profile") + 1], "work")
        self.assertEqual(argv[-1], "oss://bucket/prefix")
        self.assertNotIn("cp", argv)
        self.assertNotIn("rm", argv)
        self.assertNotIn("put", argv)
        self.assertIn("START", log.read_text(encoding="utf-8"))

    def test_ls_requires_source(self) -> None:
        with self.assertRaises(SystemExit) as error:
            oss_download.main(["ls", "--ossutil", str(self.fake)])
        self.assertEqual(error.exception.code, 2)

    def test_ls_rejects_non_oss_source(self) -> None:
        with self.assertRaises(SystemExit) as error:
            oss_download.main(
                [
                    "ls",
                    "--source",
                    "https://example.invalid/object",
                    "--ossutil",
                    str(self.fake),
                ]
            )
        self.assertEqual(error.exception.code, 2)

    def test_ls_preserves_ossutil_exit_code(self) -> None:
        with mock.patch.dict(oss_download.os.environ, {"FAKE_EXIT_CODE": "17"}, clear=False):
            result = oss_download.main(
                [
                    "ls",
                    "--source",
                    "oss://bucket/object",
                    "--ossutil",
                    str(self.fake),
                ]
            )
        self.assertEqual(result, 17)

    def test_ls_output_formats_are_restricted(self) -> None:
        for output_format in ("json", "yaml", "xml", "text"):
            with self.subTest(output_format=output_format):
                result = oss_download.main(
                    [
                        "ls",
                        "--source",
                        "oss://bucket/object",
                        "--output-format",
                        output_format,
                        "--ossutil",
                        str(self.fake),
                    ]
                )
                self.assertEqual(result, 0)

        with self.assertRaises(SystemExit) as error:
            oss_download.main(
                [
                    "ls",
                    "--source",
                    "oss://bucket/object",
                    "--output-format",
                    "csv",
                    "--ossutil",
                    str(self.fake),
                ]
            )
        self.assertEqual(error.exception.code, 2)

    def test_help_works_for_module_launcher_and_ls(self) -> None:
        module = subprocess.run([sys.executable, str(ROOT / "oss_download.py"), "--help"], capture_output=True, text=True)
        launcher = subprocess.run([str(ROOT / "oss-download"), "--help"], capture_output=True, text=True)
        download = subprocess.run([sys.executable, str(ROOT / "oss_download.py"), "download", "--help"], capture_output=True, text=True)
        listing = subprocess.run([sys.executable, str(ROOT / "oss_download.py"), "ls", "--help"], capture_output=True, text=True)
        self.assertEqual(module.returncode, 0)
        self.assertEqual(launcher.returncode, 0)
        self.assertEqual(download.returncode, 0)
        self.assertEqual(listing.returncode, 0)
        self.assertIn("configure", module.stdout)
        self.assertIn("download", module.stdout)
        self.assertIn("ls", module.stdout)
        self.assertIn("ls", launcher.stdout)
        self.assertIn("--source", download.stdout)
        self.assertIn("--output-format", listing.stdout)


if __name__ == "__main__":
    unittest.main()
