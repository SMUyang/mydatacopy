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
