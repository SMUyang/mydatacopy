# OSS Download CLI Design

## Goal

Add a cross-platform, download-only CLI that wraps Alibaba Cloud `ossutil 2.x` for reliable OSS downloads with resumable snapshots, large-file checkpoints, concurrency, filtering, and safe AccessKey configuration.

## Scope

Supported operations:

- `oss-download configure`: interactively configure an ossutil profile; secrets are hidden during input.
- `oss-download download`: download one object or a recursive OSS prefix to a local directory.
- `oss-download --help`.

Not supported: upload, delete, OSS-to-OSS copy, ACL changes, metadata changes, or arbitrary shell execution.

## Architecture

`oss_download.py` is a standard-library-only Python CLI. It validates arguments and local prerequisites, builds an `ossutil cp` command, and executes the official binary without reimplementing OSS transfer logic. `ossutil` owns multipart transfer, retry, snapshot, and checkpoint behavior.

The same Python entry point is exposed through the `largefile-copy` repository's new `oss-download` console script and a no-install launcher. `pathlib`, `platform`, and `getpass` keep behavior portable across Linux, macOS, and Windows.

## Credentials

`configure` delegates to the installed `ossutil config` wizard using a user-selected config path/profile. One-shot credential input uses hidden `getpass` prompts or environment variables; AccessKey Secret is never accepted as a normal command-line value, printed, or written to the tool log.

## Download contract

Required arguments:

- `--source`: `oss://bucket/prefix` or single object.
- `--destination`: local file or directory.

Options map directly to ossutil's documented download capabilities: recursive mode, force, update, jobs, parallel, snapshot path, checkpoint directory, big-file threshold, part size, include/exclude filters, speed limit, and dry run. The wrapper passes only explicitly requested options and preserves ossutil's exit code.

Defaults favor safe resumability: recursive download is explicit, a snapshot directory is derived beside the log/checkpoint when omitted, no delete operation is ever issued, and existing local files are not overwritten unless ossutil's documented download behavior is selected.

## Logging and verification

The wrapper records timestamped command metadata without secrets, starts and ends, and the ossutil exit status. It validates that the source uses `oss://`, the destination is usable, and `ossutil` is available before transfer. Tests use a fake ossutil executable to verify command construction without network access, plus real temporary directories for credential redaction and argument validation.
