# largefile-copy

无第三方运行时依赖的通用大文件目录树 MD5 校验与修复 CLI。

## 功能

- 必须显式提供 `--source` 和 `--destination`，按相对路径比较两个目录树
- 缺失文件从 source 原子复制到 destination
- MD5 不一致文件从 source 原子覆盖到 destination
- 复制后再次计算 MD5 复核
- JSONL checkpoint 断点续跑
- `--workers N` 并行处理不同文件
- `--subdir DIR` 分片运行；父子分片自动互斥
- 默认不排除任何目录；可重复指定 `--exclude-dir NAME`
- TTY 下显示 ANSI TUI：总进度、worker 状态、速率和 ETA
- 非 TTY 下输出普通事件日志
- `--log` 记录带时间戳的 START/REPAIRED/ERROR/SUMMARY 事件
- `EXTRA` 只报告，不删除

## 直接使用

```bash
./largefile-copy --help
./largefile-copy \
  --source /path/to/source \
  --destination /path/to/destination \
  --checkpoint /path/to/checkpoint.jsonl \
  --log /path/to/checkpoint.log \
  --workers 4
```

也可以直接运行 Python 模块：

```bash
python3 largefile_copy.py --help
```

目标目录不存在时会在复制首个文件时自动创建。默认 checkpoint 为当前目录的 `.largefile-copy.checkpoint.jsonl`；指定 `--subdir` 且未显式指定 checkpoint 时，使用 `checkpoints/` 下按分片命名的 checkpoint。`--log` 默认与 checkpoint 同名但扩展名为 `.log`。

## 排除目录

默认扫描 source 和 destination 下的全部目录。只在明确需要时指定排除项，可重复使用参数：

```bash
./largefile-copy \
  --source /path/to/source \
  --destination /path/to/destination \
  --exclude-dir cache \
  --exclude-dir temporary
```

## 分片并行

不同目录可以分别启动，每个分片拥有独立 checkpoint 和 lock：

```bash
./largefile-copy --source /source --destination /target \
  --subdir outputs --workers 2

./largefile-copy --source /source --destination /target \
  --subdir reports --workers 2
```

不要同时运行重叠分片，例如 `outputs` 与 `outputs/final`。全量运行也会与任意分片运行互斥。

## 断点续跑

同一命令重新运行会自动复用 checkpoint。当前 source/destination 文件签名未变化的完成项会跳过；变化、缺失或目标文件消失的项会重新检查。

从头运行某个分片：

```bash
./largefile-copy --source /source --destination /target \
  --subdir outputs --restart
```

## 退出码

- `0`：校验完成，或修复成功
- `1`：`--check-only` 发现缺失或 MD5 不一致
- `2`：目录、锁、I/O、复制或 checkpoint 错误

## 安装为命令

可选地在本地环境安装：

```bash
python3 -m pip install --user /Users/hyan/md5-copy-cli
```

安装后使用：

```bash
largefile-copy --help
```

CLI 使用纯 Python 标准库，不需要额外运行时依赖。

## OSS download（ossutil 2.x）

`oss-download` 是跨 Linux、macOS 和 Windows 的仅下载封装，底层调用官方 Alibaba Cloud `ossutil` 2.x。它只执行下载方向的 `ossutil cp`，不上传、不删除、不执行 OSS-to-OSS copy，也不会自动启用 destructive 选项。

### 配置凭据

先使用官方交互式配置向导保存 ossutil 配置/profile。输入 Secret 时由 ossutil 隐藏输入；本项目不会读取或打印 Secret：

```bash
# Linux/macOS
./oss-download configure --config-file /path/to/ossutil.ini --profile work

# Windows PowerShell
python .\oss_download.py configure --config-file C:\path\ossutil.ini --profile work
```

如果当前 ossutil 版本不接受 `--profile`，命令会给出提示，可去掉该选项重试。不要把 AccessKey Secret 写进命令行、脚本、README 或日志。下载命令默认复用 ossutil 配置；只有显式指定 `--prompt-credentials` 时才会隐藏提示一次性凭据，且 Secret 只通过子进程环境传递。也可以预先设置 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` 和 `OSS_REGION` 环境变量。

### 单对象与递归下载

`--source` 必须是 `oss://bucket/key` 或 `oss://bucket/prefix`，`--destination` 是本地文件或目录：

```bash
# Linux/macOS
./oss-download download \
  --source oss://bucket/path/to/object \
  --destination /path/to/object

./oss-download download \
  --source oss://bucket/path/to/prefix \
  --destination /path/to/downloads \
  --recursive

# Windows PowerShell
python .\oss_download.py download `
  --source oss://bucket/path/to/prefix `
  --destination C:\path\to\downloads `
  --recursive
```

断点状态默认放在目标旁的 `.oss-download/`（`checkpoints/`）；也可显式指定 `--checkpoint-dir`。在 v2 中，显式指定 `--update` 可跳过本地较新的文件。`ossutil` 负责传输重试、分片、CRC 和断点恢复；本项目不自行计算 MD5。

### 并发、过滤与演练

按需显式调整 ossutil 参数：

```bash
./oss-download download --source oss://bucket/data --destination /data \
  --recursive --jobs 4 --parallel 8 \
  --include '*.bam' --include '*.bai' --exclude '*.tmp' \
  --bigfile-threshold 100MB --part-size 10MB \
  --maxdownspeed 5MB --dry-run --log /path/to/oss-download.log
```

`--maxdownspeed` 保留为本项目的易读参数名，但会传给 ossutil 2.x 的 `--bandwidth-limit`。`--parallel` 必须是正整数。`--force` 和 `--update` 也只有显式指定才会传给 ossutil；默认不会额外发出覆盖、删除或同步命令。`--dry-run` 可在实际下载前查看计划。日志只记录脱敏后的命令元数据、开始/结束事件和退出码，不记录 AccessKey ID 或 Secret。

### 安全边界

运行前会检查 `ossutil` 可执行文件、`oss://` source 格式，以及 destination 父目录是否可创建和写入。请先确认目标路径和过滤规则；所有实际传输行为、并发细节和退出状态以本机安装的官方 ossutil 2.x 版本为准。
