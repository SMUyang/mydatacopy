# OSS Download CLI Design

## Goal

Add a cross-platform, read-only/list-and-download CLI that wraps Alibaba Cloud `ossutil 2.x` for reliable OSS inspection and downloads with checkpoint-based resumability, concurrency, filtering, and safe AccessKey configuration.

## Scope

Supported operations:

- `oss-download configure`: interactively configure an ossutil profile; secrets are hidden during input.
- `oss-download ls`: list objects under an OSS source without changing remote data; output is streamed from ossutil.
- `oss-download download`: download one object or a recursive OSS prefix to a local directory.
- `oss-download --help`.

Not supported: upload, delete, OSS-to-OSS copy, ACL changes, metadata changes, or arbitrary shell execution.

## Architecture

`oss_download.py` is a standard-library-only Python CLI. It validates arguments and local prerequisites, builds an `ossutil ls` or `ossutil cp` command, and executes the official binary without reimplementing OSS operations. `ossutil` owns listing, multipart transfer, retry, checkpoint, and CRC behavior.

The same Python entry point is exposed through the `largefile-copy` repository's new `oss-download` console script and a no-install launcher. `pathlib`, `platform`, and `getpass` keep behavior portable across Linux, macOS, and Windows.

## Credentials

`configure` delegates to the installed `ossutil config` wizard using a user-selected config path/profile. One-shot credential input uses hidden `getpass` prompts or environment variables; AccessKey Secret is never accepted as a normal command-line value, printed, or written to the tool log.

## List contract

Required arguments:

- `--source`: `oss://bucket/prefix` or single object.

Supported options map directly to ossutil's listing capabilities: recursive listing, repeatable include/exclude filters, `json`/`yaml`/`xml`/`text` output format, output query, all object versions, config file, and profile. The wrapper passes only explicitly requested options, places the source last, streams ossutil output through inherited standard streams, and preserves ossutil's exit code. No destination is accepted or created.

The list command is read-only: its constructed argv starts with `[ossutil, "ls"]` and cannot contain `cp`, `rm`, `put`, or any other mutating operation. A metadata log is written only when `--log` is explicit; log content is passed through the same secret-redaction helper used by downloads.

## Download contract

Required arguments:

- `--source`: `oss://bucket/prefix` or single object.
- `--destination`: local file or directory; cloud URLs are rejected.

Options map directly to ossutil's documented download capabilities: recursive, force, update, jobs, positive-integer parallelism, checkpoint directory, big-file threshold, part size, include/exclude filters, and dry run. The wrapper keeps the `--maxdownspeed` CLI name but maps it to ossutil 2.x `--bandwidth-limit`, passes only explicitly requested options, and preserves ossutil's exit code.

Defaults favor safe resumability: recursive download is explicit, a checkpoint directory is derived beside the destination when omitted, `--update` is opt-in, no delete operation is ever issued, and existing local files are not overwritten unless ossutil's documented behavior is explicitly selected.

## Logging and verification

The wrapper records timestamped command metadata without secrets, starts and ends, and the ossutil exit status. It validates that the source uses `oss://`, validates a download destination when applicable, and checks that `ossutil` is available before either operation. Tests use a fake ossutil executable to verify list/download command construction without network access, plus real temporary directories for credential redaction and argument validation.
