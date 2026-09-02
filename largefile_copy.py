#!/usr/bin/env python3
"""Verify and repair a destination directory against a source directory.

The command compares files by relative path, repairs missing or mismatched
destination files from the source, and resumes safely from a JSONL checkpoint.
It uses only the Python standard library and provides TTY progress reporting,
durable event logs, atomic copies, and protections for overlapping shards.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from dataclasses import dataclass, field
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import sys
import tempfile
import threading
import time
from typing import Callable, Collection, Iterator


CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_CHECKPOINT = Path(".largefile-copy.checkpoint.jsonl")
CHECKPOINT_VERSION = 1
RENDER_INTERVAL = 0.5
RATE_WINDOW = 15.0


def _raise_walk_error(error: OSError) -> None:
    raise error


def iter_files(root: Path, excluded_dirs: Collection[str] = ()) -> Iterator[tuple[Path, Path]]:
    """Yield ``(relative_path, absolute_path)`` for files below root."""
    excluded = set(excluded_dirs)
    for directory, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=_raise_walk_error,
    ):
        dirnames[:] = sorted(name for name in dirnames if name not in excluded)
        for name in dirnames + filenames:
            path = Path(directory) / name
            if path.is_symlink() and not path.exists():
                raise OSError(f"broken symbolic link: {path}")
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_file():
                yield path.relative_to(root), path


def collect_files(root: Path, excluded_dirs: Collection[str] = ()) -> dict[Path, Path]:
    return dict(iter_files(root, excluded_dirs))

def md5_file(path: Path, progress: Callable[[int, int], None] | None = None) -> str:
    digest = hashlib.md5()
    total = path.stat().st_size
    done = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return digest.hexdigest()


def file_signature(path: Path) -> dict[str, int]:
    metadata = path.stat()
    return {
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


def copy_atomic(
    source: Path,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Copy source to destination without leaving a partial destination file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = source.stat().st_size
    done = 0
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.md5repair-",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), stat.S_IMODE(source.stat().st_mode))
            with source.open("rb") as source_handle:
                while chunk := source_handle.read(CHUNK_SIZE):
                    temporary.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            temporary.flush()
            try:
                os.fsync(temporary.fileno())
            except PermissionError:
                pass
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def checkpoint_config(args: argparse.Namespace) -> dict[str, object]:
    return {
        "type": "header",
        "version": CHECKPOINT_VERSION,
        "source": str(args.source.resolve()),
        "destination": str(args.destination.resolve()),
        "exclude_dirs": list(args.exclude_dir),
        "subdir": args.subdir.as_posix() if args.subdir else "",
    }

def checkpoint_reusable(
    record: dict[str, object],
    source_path: Path,
    destination_path: Path | None,
) -> bool:
    if record.get("status") not in {"match", "repaired_missing", "repaired_mismatch"}:
        return False
    if destination_path is None or not destination_path.is_file():
        return False
    try:
        return (
            record.get("source_signature") == file_signature(source_path)
            and record.get("destination_signature") == file_signature(destination_path)
        )
    except OSError:
        return False


def process_file(
    relative_path: Path,
    source_path: Path,
    destination_path: Path | None,
    destination_root: Path,
    check_only: bool,
    slot: "WorkerSlot | None" = None,
) -> dict[str, object]:
    def report(phase: str) -> Callable[[int, int], None]:
        if slot is None:
            return lambda done, total: None
        name = relative_path.as_posix()
        def cb(done: int, total: int) -> None:
            slot.file = name
            slot.phase = phase
            slot.done = done
            slot.total = total
        return cb

    if slot is not None:
        slot.file = relative_path.as_posix()
        slot.phase = "md5"
        slot.done = 0
        slot.total = source_path.stat().st_size

    source_signature = file_signature(source_path)
    source_md5 = md5_file(source_path, progress=report("md5"))
    if file_signature(source_path) != source_signature:
        raise OSError("source file changed while reading; retry this file")

    if destination_path is None or not destination_path.is_file():
        if check_only:
            return {
                "type": "result",
                "relative": relative_path.as_posix(),
                "status": "missing",
                "source_md5": source_md5,
            }
        destination = destination_root / relative_path
        copy_atomic(source_path, destination, progress=report("copy"))
        repaired_md5 = md5_file(destination, progress=report("verify"))
        if repaired_md5 != source_md5:
            raise OSError("MD5 mismatch after copy")
        return {
            "type": "file",
            "relative": relative_path.as_posix(),
            "status": "repaired_missing",
            "source_md5": source_md5,
            "destination_md5": None,
            "source_signature": file_signature(source_path),
            "destination_signature": file_signature(destination),
        }

    destination_signature = file_signature(destination_path)
    destination_md5 = md5_file(destination_path, progress=report("md5-destination"))
    if file_signature(destination_path) != destination_signature:
        raise OSError("destination file changed while reading; retry this file")
    if destination_md5 == source_md5:
        return {
            "type": "file",
            "relative": relative_path.as_posix(),
            "status": "match",
            "source_md5": source_md5,
            "destination_md5": destination_md5,
            "source_signature": source_signature,
            "destination_signature": destination_signature,
        }
    if check_only:
        return {
            "type": "result",
            "relative": relative_path.as_posix(),
            "status": "mismatch",
            "source_md5": source_md5,
            "destination_md5": destination_md5,
        }

    copy_atomic(source_path, destination_path, progress=report("copy"))
    repaired_md5 = md5_file(destination_path, progress=report("verify"))
    if repaired_md5 != source_md5:
        raise OSError("MD5 mismatch after copy")
    return {
        "type": "file",
        "relative": relative_path.as_posix(),
        "status": "repaired_mismatch",
        "source_md5": source_md5,
        "destination_md5": destination_md5,
        "source_signature": file_signature(source_path),
        "destination_signature": file_signature(destination_path),
    }


def write_checkpoint_header(path: Path, config: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(config, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_checkpoint(
    path: Path,
    config: dict[str, object],
    restart: bool,
) -> dict[str, dict[str, object]]:
    if restart or not path.exists():
        write_checkpoint_header(path, config)
        return {}

    with path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
    if not lines:
        raise OSError(f"empty checkpoint: {path}; use --restart")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise OSError(f"invalid checkpoint header: {path}") from error
    if not isinstance(header, dict):
        raise OSError(f"invalid checkpoint header: {path}; use --restart")
    normalized_header = dict(header)
    if config["subdir"] == "":
        normalized_header.setdefault("subdir", "")
    if normalized_header != config:
        raise OSError(f"checkpoint settings differ: {path}; use --restart")

    records: dict[str, dict[str, object]] = {}
    for index, line in enumerate(lines[1:], start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1:
                break
            raise OSError(f"corrupt checkpoint record: {path}:{index + 1}") from error
        if isinstance(record, dict) and record.get("type") == "file" and isinstance(record.get("relative"), str):
            records[record["relative"]] = record
    return records


def append_checkpoint(path: Path, record: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def acquire_run_lock(checkpoint: Path):
    lock_path = checkpoint.with_name(f"{checkpoint.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise OSError(f"another checker holds lock: {lock_path}") from error
    return handle


def release_run_lock(handle) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def checker_subdir_parts(argv: list[str]) -> tuple[str, ...] | None:
    value = None
    for index, item in enumerate(argv):
        if item == "--subdir" and index + 1 < len(argv):
            value = argv[index + 1]
            break
        if item.startswith("--subdir="):
            value = item.split("=", 1)[1]
            break
    if not value:
        return None
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return tuple(path.parts)


def checker_process_scope(argv: list[str], script_name: str) -> tuple[bool, bool]:
    if not argv:
        return False, False
    program = Path(argv[0]).name
    if program == "largefile-copy":
        return True, checker_subdir_parts(argv) is not None
    if not program.lower().startswith("python"):
        return False, False
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "largefile_copy":
        return True, checker_subdir_parts(argv) is not None
    if len(argv) >= 2 and Path(argv[1]).name == script_name:
        return True, checker_subdir_parts(argv) is not None
    return False, False


def shards_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return left[: len(right)] == right or right[: len(left)] == left


def conflicting_checker_pids(subdir: Path | None) -> list[int]:
    script_name = Path(__file__).name
    current_parts = tuple(subdir.parts) if subdir is not None else None
    conflicts = []
    for procdir in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(procdir.name)
            if pid == os.getpid():
                continue
            with open(procdir / "cmdline", "rb") as handle:
                argv = [
                    item.decode(errors="replace")
                    for item in handle.read().split(b"\0")
                    if item
                ]
            is_checker, other_sharded = checker_process_scope(argv, script_name)
            if not is_checker:
                continue
            other_parts = checker_subdir_parts(argv) if other_sharded else None
            if current_parts is None or other_parts is None:
                conflicts.append(pid)
            elif shards_overlap(current_parts, other_parts):
                conflicts.append(pid)
        except (OSError, ValueError):
            pass
    return conflicts




# ---------------------------------------------------------------- progress UI


@dataclass
class WorkerSlot:
    file: str = ""
    phase: str = "idle"
    done: int = 0
    total: int = 0


def fmt_bytes(value: float) -> str:
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600:d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def bar(fraction: float, width: int = 18) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


class ProgressTracker:
    """Thread-safe progress state, log writer, and ANSI renderer."""

    def __init__(
        self,
        workers: int,
        total_files: int,
        total_bytes: int,
        done_files: int,
        done_bytes: int,
        log_path: Path | None,
        interactive: bool,
    ) -> None:
        self.slots = [WorkerSlot() for _ in range(workers)]
        self._free_slots: queue.Queue[WorkerSlot] = queue.Queue()
        for slot in self.slots:
            self._free_slots.put(slot)
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = done_files
        self.done_bytes = done_bytes
        self.counts = {
            "match": 0, "repaired_missing": 0, "repaired_mismatch": 0,
            "detected_missing": 0, "detected_mismatch": 0, "extra": 0, "errors": 0,
        }
        self._lock = threading.Lock()
        self._events: deque[tuple[str, str]] = deque(maxlen=4)
        self._rate_samples: deque[tuple[float, float]] = deque()
        self._processed_bytes = 0
        self.start = time.monotonic()
        self.interactive = interactive
        self._lines_rendered = 0
        self._stop = threading.Event()
        self.log_handle = (
            log_path.open("a", encoding="utf-8") if log_path is not None else None
        )

    # -- worker API ----------------------------------------------------
    def acquire_slot(self) -> WorkerSlot:
        slot = self._free_slots.get()
        return slot

    def release_slot(self, slot: WorkerSlot) -> None:
        slot.file = ""
        slot.phase = "idle"
        slot.done = slot.total = 0
        self._free_slots.put(slot)

    def add_bytes(self, delta: float) -> None:
        with self._lock:
            self._processed_bytes += delta

    # -- events ----------------------------------------------------------
    def event(self, tag: str, text: str) -> None:
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        line = f"{stamp} {tag:<12} {text}"
        if self.log_handle is not None:
            self.log_handle.write(line + "\n")
            self.log_handle.flush()
        if self.interactive:
            with self._lock:
                self._events.append((tag, text))
        else:
            print(line)

    def finish(self, status: str, size_bytes: int) -> None:
        with self._lock:
            self.done_files += 1
            self.done_bytes += size_bytes
            if status in self.counts:
                self.counts[status] += 1

    # -- rendering -------------------------------------------------------
    def rate_bps(self) -> float:
        now = time.monotonic()
        with self._lock:
            self._rate_samples.append((now, self._processed_bytes))
            while len(self._rate_samples) > 1 and now - self._rate_samples[0][0] > RATE_WINDOW:
                self._rate_samples.popleft()
            if len(self._rate_samples) < 2:
                return 0.0
            t0, b0 = self._rate_samples[0]
            t1, b1 = self._rate_samples[-1]
        span = t1 - t0
        return (b1 - b0) / span if span > 0 else 0.0

    def render_once(self) -> None:
        elapsed = time.monotonic() - self.start
        rate = self.rate_bps()
        remain_bytes = max(0, self.total_bytes - self.done_bytes)
        remain_files = max(0, self.total_files - self.done_files)
        eta = (remain_bytes / rate) if rate > 1 else None

        pct_files = 100.0 * self.done_files / self.total_files if self.total_files else 100.0
        pct_bytes = 100.0 * self.done_bytes / self.total_bytes if self.total_bytes else 100.0

        out = []
        out.append(
            f"\x1b[1m[{bar(pct_bytes / 100)}] {pct_bytes:5.1f}%\x1b[0m "
            f"data {fmt_bytes(self.done_bytes)}/{fmt_bytes(self.total_bytes)} "
            f"files {self.done_files}/{self.total_files} ({pct_files:.1f}%) "
            f"elapsed {fmt_duration(elapsed)} "
            f"rate {fmt_bytes(rate)}/s "
            f"eta {fmt_duration(eta) if eta else '--'} "
            f"remain {remain_files}f"
        )
        for index, slot in enumerate(self.slots, 1):
            if slot.phase == "idle" or not slot.file:
                out.append(f"  [{index}] idle")
                continue
            fraction = (slot.done / slot.total) if slot.total else 0.0
            name = slot.file if len(slot.file) <= 44 else "…" + slot.file[-43:]
            out.append(
                f"  [{index}] {slot.phase:<8} {name:<44} "
                f"{fmt_bytes(slot.done):>9}/{fmt_bytes(slot.total):<9} "
                f"{bar(fraction, 10)} {fraction * 100:3.0f}%"
            )
        with self._lock:
            events = list(self._events)
        for tag, text in events:
            color = "\x1b[31m" if tag.startswith("ERROR") else "\x1b[32m" if tag.startswith("REPAIRED") else "\x1b[0m"
            out.append(f"  {color}{tag:<12}\x1b[0m {text}")

        block = "\n".join(out)
        if self._lines_rendered:
            sys.stdout.write(f"\x1b[{self._lines_rendered}A\x1b[J")
        sys.stdout.write(block + "\n")
        sys.stdout.flush()
        self._lines_rendered = len(out)

    def render_loop(self) -> None:
        while not self._stop.wait(RENDER_INTERVAL):
            try:
                self.render_once()
            except OSError:
                pass

    def start_rendering(self) -> None:
        if self.interactive:
            self._thread = threading.Thread(target=self.render_loop, daemon=True)
            self._thread.start()

    def stop_rendering(self) -> None:
        self._stop.set()
        if self.interactive:
            try:
                self.render_once()
            except OSError:
                pass
            if self._lines_rendered:
                sys.stdout.write("\n")
                sys.stdout.flush()
        if self.log_handle is not None:
            self.log_handle.close()

    def event_line(self, tag: str, text: str) -> None:
        self.event(tag, text)


# ---------------------------------------------------------------- argument parsing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare source and destination directory trees by relative path, "
            "repair differences, and resume from a durable checkpoint."
        ),
        epilog="exit 0=clean/repaired, 1=--check-only found differences, 2=error",
    )
    parser.add_argument("--source", type=Path, required=True, help="source directory")
    parser.add_argument("--destination", type=Path, required=True, help="destination directory")
    parser.add_argument(
        "--subdir",
        type=Path,
        help="relative subdirectory processed by this process; use disjoint values per process",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        metavar="NAME",
        help="directory name to exclude from both trees; repeat for multiple names",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help=f"JSONL checkpoint (default: {DEFAULT_CHECKPOINT})",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="event log path (default: alongside the checkpoint)",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="ignore and replace the existing checkpoint",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="parallel worker count (default: 4)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="report differences without copying files",
    )
    args = parser.parse_args()
    if args.subdir is not None:
        if (
            args.subdir.is_absolute()
            or not args.subdir.parts
            or any(part in {"", ".", ".."} for part in args.subdir.parts)
        ):
            parser.error("--subdir must be a non-empty relative path without . or ..")
        if any(part in args.exclude_dir for part in args.subdir.parts):
            parser.error("--subdir cannot include an excluded directory")
        args.source = args.source / args.subdir
        args.destination = args.destination / args.subdir
    if args.checkpoint is None:
        if args.subdir is None:
            args.checkpoint = DEFAULT_CHECKPOINT
        else:
            slug = "__".join(args.subdir.parts)
            args.checkpoint = DEFAULT_CHECKPOINT.parent / "checkpoints" / f"{slug}.jsonl"
    if args.log is None:
        args.log = args.checkpoint.with_suffix(".log")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


# ---------------------------------------------------------------- main


def main() -> int:
    args = parse_args()

    if not args.source.is_dir():
        print(f"ERROR missing source directory: {args.source}", file=sys.stderr)
        return 2
    if args.destination.exists() and not args.destination.is_dir():
        print(f"ERROR destination path is not a directory: {args.destination}", file=sys.stderr)
        return 2
    conflicts = conflicting_checker_pids(args.subdir)
    if conflicts:
        print(
            "ERROR another checker process is active: "
            + ", ".join(str(pid) for pid in conflicts),
            file=sys.stderr,
        )
        return 2

    lock_handle = None
    try:
        lock_handle = acquire_run_lock(args.checkpoint)
        source_files = collect_files(args.source, args.exclude_dir)
        destination_files = collect_files(args.destination, args.exclude_dir) if args.destination.is_dir() else {}
        records = load_checkpoint(args.checkpoint, checkpoint_config(args), args.restart)
    except OSError as error:
        if lock_handle is not None:
            release_run_lock(lock_handle)
        print(f"ERROR preparing scan: {error}", file=sys.stderr)
        return 2

    print(
        f"CONFIG       subdir={args.subdir.as_posix() if args.subdir else '/'} "
        f"workers={args.workers} checkpoint={args.checkpoint} log={args.log}"
    )

    total_bytes = 0
    source_sizes: dict[Path, int] = {}
    pending: list[tuple[Path, Path, Path | None]] = []
    resumed = 0
    resumed_bytes = 0
    interactive = sys.stdout.isatty()
    tracker = ProgressTracker(
        workers=args.workers,
        total_files=len(source_files),
        total_bytes=0,
        done_files=0,
        done_bytes=0,
        log_path=args.log,
        interactive=interactive,
    )
    try:
        for relative_path in sorted(source_files):
            source_path = source_files[relative_path]
            size = source_path.stat().st_size
            source_sizes[relative_path] = size
            total_bytes += size
            destination_path = destination_files.get(relative_path)
            record = records.get(relative_path.as_posix())
            if record and checkpoint_reusable(record, source_path, destination_path):
                resumed += 1
                resumed_bytes += size
                continue
            pending.append((relative_path, source_path, destination_path))
    except OSError as error:
        tracker.counts["errors"] += 1
        tracker.counts["resumed"] = resumed
        tracker.event("ERROR", f"planning: {error}")
        tracker.event(
            "SUMMARY",
            f"resumed={resumed} match=0 repaired_missing=0 repaired_mismatch=0 "
            f"detected_missing=0 detected_mismatch=0 extra=0 errors=1",
        )
        tracker.stop_rendering()
        if lock_handle is not None:
            release_run_lock(lock_handle)
        print(f"ERROR planning scan: {error}", file=sys.stderr)
        return 2

    tracker.total_bytes = total_bytes
    tracker.done_files = resumed
    tracker.done_bytes = resumed_bytes
    tracker.event("START", f"subdir={args.subdir.as_posix() if args.subdir else '/'} total={len(source_files)} files "
                           f"{fmt_bytes(total_bytes)} resumed={resumed} pending={len(pending)}")

    def run_one(relative_path: Path, source_path: Path, destination_path: Path | None):
        slot = tracker.acquire_slot()
        file_bytes = 0
        try:
            return process_file(
                relative_path, source_path, destination_path, args.destination,
                args.check_only, slot,
            )
        finally:
            if slot.phase != "idle":
                file_bytes = slot.done
            if file_bytes:
                tracker.add_bytes(file_bytes)
            tracker.release_slot(slot)

    exit_code = 0
    future_to_relative = {}
    tracker.start_rendering()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for relative_path, source_path, destination_path in pending:
                future = executor.submit(run_one, relative_path, source_path, destination_path)
                future_to_relative[future] = relative_path

            for future in as_completed(future_to_relative):
                relative_path = future_to_relative[future]
                size = source_sizes[relative_path]
                try:
                    result = future.result()
                except OSError as error:
                    tracker.counts["errors"] += 1
                    tracker.event("ERROR", f"{relative_path}: {error}")
                    continue

                status = result["status"]
                if status == "match":
                    tracker.finish("match", size)
                elif status == "missing":
                    tracker.counts["detected_missing"] += 1
                    tracker.event("MISSING", f"{relative_path}")
                elif status == "mismatch":
                    tracker.counts["detected_mismatch"] += 1
                    tracker.event("MISMATCH", f"{relative_path}")
                elif status == "repaired_missing":
                    tracker.finish("repaired_missing", size)
                    tracker.event("REPAIRED", f"{relative_path} (missing) {fmt_bytes(size)}")
                elif status == "repaired_mismatch":
                    tracker.finish("repaired_mismatch", size)
                    tracker.event("REPAIRED", f"{relative_path} (mismatch) {fmt_bytes(size)}")

                if result["type"] == "file":
                    try:
                        append_checkpoint(args.checkpoint, result)
                    except OSError as error:
                        tracker.counts["errors"] += 1
                        tracker.event("ERROR", f"checkpoint {relative_path}: {error}")

        for relative_path in sorted(set(destination_files) - set(source_files)):
            tracker.counts["extra"] += 1
            tracker.event("EXTRA", f"{relative_path}")
    except KeyboardInterrupt:
        tracker.event("ABORT", "interrupted by user; checkpoint preserved")
        exit_code = 2

    counts = tracker.counts
    counts["resumed"] = resumed
    summary = (
        f"resumed={counts['resumed']} "
        f"match={counts['match']} "
        f"repaired_missing={counts['repaired_missing']} "
        f"repaired_mismatch={counts['repaired_mismatch']} "
        f"detected_missing={counts['detected_missing']} "
        f"detected_mismatch={counts['detected_mismatch']} "
        f"extra={counts['extra']} "
        f"errors={counts['errors']}"
    )
    tracker.event("SUMMARY", summary)
    tracker.stop_rendering()

    if lock_handle is not None:
        release_run_lock(lock_handle)

    if counts["errors"] or exit_code:
        return 2
    if args.check_only and (counts["detected_missing"] or counts["detected_mismatch"]):
        return 1
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
