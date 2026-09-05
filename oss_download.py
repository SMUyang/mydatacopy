"""Safe, download-only wrapper around Alibaba Cloud ossutil 2.x."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence


DEFAULT_STATE_DIRNAME = ".oss-download"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parallel(value: str) -> str:
    return str(_positive_int(value))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oss-download",
        description="List or download objects from Alibaba Cloud OSS with ossutil 2.x.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser(
        "configure", help="run the interactive ossutil credential configuration wizard"
    )
    configure.add_argument("--config-file", type=Path, help="ossutil configuration file")
    configure.add_argument("--profile", help="ossutil profile name")
    configure.add_argument("--ossutil", default="ossutil", help="ossutil executable")
    configure.set_defaults(handler=_configure)

    download = subparsers.add_parser(
        "download", help="download an OSS object or recursive prefix to a local path"
    )
    download.add_argument("--source", required=True, help="OSS source (oss://bucket/key)")
    download.add_argument("--destination", required=True, help="local destination")
    download.add_argument("--recursive", action="store_true", help="download a prefix recursively")
    download.add_argument("--force", action="store_true", help="overwrite existing local files")
    download.add_argument("--update", action="store_true", help="download only newer source objects")
    download.add_argument("--jobs", type=_positive_int, help="number of ossutil jobs")
    download.add_argument(
        "--parallel", type=_parallel, metavar="N", help="positive parallel transfer count"
    )
    download.add_argument("--checkpoint-dir", type=Path, help="ossutil multipart checkpoint directory")
    download.add_argument("--bigfile-threshold", help="threshold for multipart downloads")
    download.add_argument("--part-size", help="multipart part size")
    download.add_argument("--include", action="append", default=[], help="include filter (repeatable)")
    download.add_argument("--exclude", action="append", default=[], help="exclude filter (repeatable)")
    download.add_argument(
        "--maxdownspeed", help="download speed limit (passed to ossutil as --bandwidth-limit)"
    )
    download.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    download.add_argument("--config-file", type=Path, help="ossutil configuration file")
    download.add_argument("--profile", help="ossutil profile name")
    download.add_argument(
        "--prompt-credentials",
        action="store_true",
        help="prompt for one-shot credentials; values are passed only through the child environment",
    )
    download.add_argument("--ossutil", default="ossutil", help="ossutil executable")
    download.add_argument("--log", type=Path, help="metadata log path")
    download.set_defaults(handler=_download)
    ls = subparsers.add_parser("ls", help="list objects under an OSS path without changing data")
    ls.add_argument("--source", required=True, help="OSS source (oss://bucket/key)")
    ls.add_argument("--recursive", action="store_true", help="list objects recursively")
    ls.add_argument("--include", action="append", default=[], help="include filter (repeatable)")
    ls.add_argument("--exclude", action="append", default=[], help="exclude filter (repeatable)")
    ls.add_argument(
        "--output-format",
        choices=("json", "yaml", "xml", "text"),
        help="ossutil output format",
    )
    ls.add_argument("--output-query", help="JMESPath query for structured output")
    ls.add_argument("--all-versions", action="store_true", help="include all object versions")
    ls.add_argument("--config-file", type=Path, help="ossutil configuration file")
    ls.add_argument("--profile", help="ossutil profile name")
    ls.add_argument("--ossutil", default="ossutil", help="ossutil executable")
    ls.add_argument("--log", type=Path, help="metadata log path")
    ls.set_defaults(handler=_ls)
    return parser


def _configure(args: argparse.Namespace) -> int:
    executable = _resolve_executable(args.ossutil)
    if executable is None:
        print(f"ERROR ossutil executable not found or not executable: {args.ossutil}", file=sys.stderr)
        return 2

    command = [executable, "config"]
    if args.config_file is not None:
        command.extend(["--config-file", str(args.config_file)])
    if args.profile is not None:
        command.extend(["--profile", args.profile])

    try:
        result = subprocess.run(command, check=False)
    except OSError as error:
        print(f"ERROR unable to start ossutil config: {error}", file=sys.stderr)
        return 2
    if result.returncode != 0:
        if args.profile is not None:
            print(
                "ERROR ossutil config failed; this ossutil may not support --profile. "
                "Retry without --profile or use a compatible ossutil 2.x release.",
                file=sys.stderr,
            )
        else:
            print(f"ERROR ossutil config exited with status {result.returncode}", file=sys.stderr)
    return result.returncode


def _resolve_executable(value: str) -> str | None:
    candidate = Path(value)
    if candidate.parent != Path(".") or candidate.is_absolute():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which(value)


def _validate_destination(destination: str, parser: argparse.ArgumentParser) -> None:
    if "://" in destination:
        parser.error("--destination must be a local path, not a cloud URL")


def _validate_source(source: str, parser: argparse.ArgumentParser) -> None:
    remainder = source.removeprefix("oss://") if source.startswith("oss://") else ""
    bucket = remainder.split("/", 1)[0].strip()
    if not bucket:
        parser.error("--source must be an oss://bucket[/prefix] URL")


def _prepare_path(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OSError(f"cannot create {label} {path}: {error}") from error
    if not os.access(path, os.W_OK):
        raise OSError(f"{label} is not writable: {path}")


def _credential_environment(prompt: bool) -> tuple[dict[str, str], str | None, str | None]:
    environment = os.environ.copy()
    if not prompt:
        return environment, None, None

    access_key_id = input("AccessKey ID: ").strip()
    region = input("Region: ").strip()
    access_key_secret = getpass.getpass("AccessKey Secret: ")
    if not access_key_id or not access_key_secret or not region:
        raise ValueError("AccessKey ID, AccessKey Secret, and Region are required")
    environment.update(
        {
            "OSS_ACCESS_KEY_ID": access_key_id,
            "OSS_ACCESS_KEY_SECRET": access_key_secret,
            "OSS_REGION": region,
        }
    )
    return environment, access_key_id, access_key_secret


def _redact(text: str, access_key_id: str | None, access_key_secret: str | None) -> str:
    redacted = text
    for value in (access_key_secret, access_key_id):
        if value:
            redacted = redacted.replace(value, "***")
    return redacted


def _write_log(path: Path, message: str, access_key_id: str | None = None, access_key_secret: str | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_redact(message, access_key_id, access_key_secret) + "\n")
    except OSError:
        # Transfer output and exit status remain authoritative if metadata logging fails.
        pass


def _build_download_command(args: argparse.Namespace, checkpoint: Path, executable: str) -> list[str]:
    command = [executable, "cp"]
    if args.recursive:
        command.append("-r")
    if args.force:
        command.append("-f")
    if args.update:
        command.append("-u")
    if args.jobs is not None:
        command.extend(["-j", str(args.jobs)])
    if args.parallel is not None:
        command.extend(["--parallel", args.parallel])
    command.extend(["--checkpoint-dir", str(checkpoint)])
    for option, value in (("--bigfile-threshold", args.bigfile_threshold), ("--part-size", args.part_size)):
        if value is not None:
            command.extend([option, value])
    if args.maxdownspeed is not None:
        command.extend(["--bandwidth-limit", args.maxdownspeed])
    for pattern in args.include:
        command.extend(["--include", pattern])
    for pattern in args.exclude:
        command.extend(["--exclude", pattern])
    if args.dry_run:
        command.append("--dry-run")
    if args.config_file is not None:
        command.extend(["--config-file", str(args.config_file)])
    if args.profile is not None:
        command.extend(["--profile", args.profile])
    command.extend([args.source, str(args.destination)])
    return command


def _build_ls_command(args: argparse.Namespace, executable: str) -> list[str]:
    command = [executable, "ls"]
    if args.recursive:
        command.append("--recursive")
    for pattern in args.include:
        command.extend(["--include", pattern])
    for pattern in args.exclude:
        command.extend(["--exclude", pattern])
    if args.output_format is not None:
        command.extend(["--output-format", args.output_format])
    if args.output_query is not None:
        command.extend(["--output-query", args.output_query])
    if args.all_versions:
        command.append("--all-versions")
    if args.config_file is not None:
        command.extend(["--config-file", str(args.config_file)])
    if args.profile is not None:
        command.extend(["--profile", args.profile])
    command.append(args.source)
    return command


def _download(args: argparse.Namespace) -> int:
    parser = _build_parser()
    _validate_source(args.source, parser)
    _validate_destination(args.destination, parser)
    args.destination = Path(args.destination)
    executable = _resolve_executable(args.ossutil)
    if executable is None:
        print(f"ERROR ossutil executable not found or not executable: {args.ossutil}", file=sys.stderr)
        return 2

    state_dir = args.destination.parent / DEFAULT_STATE_DIRNAME
    checkpoint = args.checkpoint_dir or state_dir / "checkpoints"
    log_path = args.log or state_dir / "download.log"
    try:
        _prepare_path(args.destination.parent, "destination parent")
        _prepare_path(checkpoint, "checkpoint directory")
        _prepare_path(log_path.parent, "log parent")
        environment, access_key_id, access_key_secret = _credential_environment(args.prompt_credentials)
    except (OSError, ValueError) as error:
        print(f"ERROR preparing download: {error}", file=sys.stderr)
        return 2

    command = _build_download_command(args, checkpoint, executable)
    _write_log(log_path, "START command=" + " ".join(command), access_key_id, access_key_secret)
    try:
        process = subprocess.Popen(command, env=environment)
        returncode = process.wait()
    except OSError as error:
        _write_log(log_path, f"END error={error} exit=2", access_key_id, access_key_secret)
        print(f"ERROR unable to start ossutil: {error}", file=sys.stderr)
        return 2
    _write_log(log_path, f"END exit={returncode}", access_key_id, access_key_secret)
    return returncode


def _ls(args: argparse.Namespace) -> int:
    parser = _build_parser()
    _validate_source(args.source, parser)
    executable = _resolve_executable(args.ossutil)
    if executable is None:
        print(f"ERROR ossutil executable not found or not executable: {args.ossutil}", file=sys.stderr)
        return 2

    command = _build_ls_command(args, executable)
    log_path = args.log
    if log_path is not None:
        _write_log(log_path, "START command=" + " ".join(command))
    try:
        process = subprocess.Popen(command)
        returncode = process.wait()
    except OSError as error:
        if log_path is not None:
            _write_log(log_path, f"END error={error} exit=2")
        print(f"ERROR unable to start ossutil: {error}", file=sys.stderr)
        return 2
    if log_path is not None:
        _write_log(log_path, f"END exit={returncode}")
    return returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
