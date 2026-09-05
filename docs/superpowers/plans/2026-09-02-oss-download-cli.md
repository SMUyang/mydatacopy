# OSS Download CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development to implement this plan task-by-task.

**Goal:** Add a cross-platform, read-only-capable `oss-download` CLI that safely configures AccessKey credentials, lists OSS objects, and delegates resumable large-file transfers to Alibaba Cloud ossutil 2.x.

**Architecture:** A standard-library Python module validates OSS/local paths and constructs either an `ossutil ls` read-only subprocess command or an `ossutil cp` download command. An interactive `configure` subcommand delegates credential storage to the official ossutil wizard; download and list modes never accept a Secret as a normal command-line argument. A console-script entry point and no-install launcher provide the same CLI on Linux, macOS, and Windows.

**Tech Stack:** Python 3.10+, argparse, pathlib, getpass, subprocess, unittest, setuptools, Alibaba Cloud ossutil 2.x.

---

### Task 1: OSS list/download command and credential surface

**Files:**
- Modify: `oss_download.py`
- Modify: `oss-download`
- Modify: `pyproject.toml`
- Test: `test_oss_download.py`

- [ ] **Step 1: Write failing tests**

Cover required `--source`/`--destination` for download, `oss://` source validation, local-only destination validation, hidden credential input through `getpass`, secret redaction from logs, and construction of v2 `ossutil cp` with `-r`, `-j`, positive-integer `--parallel`, `--checkpoint-dir`, `--include`, `--exclude`, and `--bandwidth-limit`. Add list coverage for the required source, recursive/filter/output/all-versions/config/profile options, accepted output formats, read-only argv boundaries, non-OSS rejection, ossutil exit-code passthrough, and module/launcher help visibility.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m unittest -v test_oss_download.py
```

Expected: import/entry-point failures because `oss_download.py` and the `oss-download` command do not exist.

- [ ] **Step 3: Implement the CLI**

Implement:

```text
oss-download configure [--config-file PATH] [--profile NAME]
oss-download ls --source oss://bucket/prefix [options]
oss-download download --source oss://bucket/prefix --destination PATH [options]
```

Use `argparse` subparsers. `download` must reject non-OSS sources and cloud destinations, reject missing destination, and check `shutil.which("ossutil")` unless `--ossutil PATH` is supplied. `ls` must require an OSS source, accept repeatable include/exclude filters, `json`/`yaml`/`xml`/`text` output formats, output query, all versions, config/profile, executable, and metadata log options, and must construct only `[ossutil, "ls", options..., source]`. Both operations preserve ossutil's exit code; list output is inherited and therefore forwarded in real time. Use `getpass.getpass` for an optional one-shot credential path on download; never include a Secret in a subprocess argv or log line. Build the official v2 `ossutil cp` invocation, map `--maxdownspeed` to `--bandwidth-limit`, accept only a positive integer for `--parallel`, and pass explicit download options only.

`configure` must invoke `ossutil config` interactively and pass a user-supplied config file path through the documented config option without printing credential values.

The launcher must be portable:

```sh
#!/bin/sh
exec python3 "$(dirname "$0")/oss_download.py" "$@"
```

- [ ] **Step 4: Run focused tests**

Run the same unittest file. Expected: all CLI validation, command construction, and redaction tests pass.

- [ ] **Step 5: Commit**

```bash
git add oss_download.py oss-download pyproject.toml test_oss_download.py
git commit -m "feat: add oss download cli"
```

### Task 2: Cross-platform documentation and package validation

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update usage documentation**

Document Linux, macOS, and Windows PowerShell examples for `configure`, read-only `ls`, and `download`; hidden AccessKey Secret behavior; list output formats, filters, all versions, and output queries; recursive and single-object downloads; checkpoint resume; concurrency tuning with positive-integer `--parallel`; filters; speed limiting via `--maxdownspeed`/`--bandwidth-limit`; dry-run; log files; local-only destinations; and the download-only/list-only safety boundary.

- [ ] **Step 2: Validate entry points without network access**

Run:

```bash
python3 oss_download.py --help
./oss-download --help
python3 -m unittest -v test_oss_download.py
```

Expected: both help surfaces show identical configure/download/list command visibility, ls options, and download options; the focused tests cover command construction and safety boundaries.

- [ ] **Step 3: Commit**

```bash
git add README.md pyproject.toml
 git commit -m "docs: document cross-platform oss downloads"
```

### Task 3: Release verification

- [ ] **Step 1: Inspect repository status and diff**

```bash
git status --short --branch
git diff --check HEAD~2..HEAD
```

Expected: clean whitespace and only OSS downloader files changed.

- [ ] **Step 2: Push the release**

```bash
git push origin main
```

- [ ] **Step 3: Verify remote ref**

```bash
git ls-remote --heads origin main
```

Expected: `main` resolves to the final commit.
