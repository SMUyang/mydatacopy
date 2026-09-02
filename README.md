# md5-copy-cli

无第三方运行时依赖的目录树 MD5 校验与修复 CLI。

## 功能

- 以 `--raw` 为基准，按相对路径校验 `--tmpfile`
- 缺失文件从 raw 复制到目标目录
- MD5 不一致文件从 raw 原子覆盖到目标目录
- 复制后再次计算 MD5
- JSONL checkpoint 断点续跑
- `--workers N` 并行处理不同文件
- `--subdir DIR` 分片运行；父子分片自动互斥
- TTY 下显示 ANSI TUI：总进度、worker 状态、速率和 ETA
- 非 TTY 下输出普通事件日志
- `--log` 记录带时间戳的 START/REPAIRED/ERROR/SUMMARY 事件
- `EXTRA` 只报告，不删除

默认排除目录名 `03.BQSR`，raw 和目标两侧都排除。

## 目录

```text
md5-copy-cli/
├── md5-copy       # 无安装启动器
├── md5_copy.py    # CLI 主程序
├── pyproject.toml # 可选安装配置
└── README.md
```

## 直接使用

```bash
./md5-copy --help
./md5-copy \
  --raw /source/root \
  --tmpfile /destination/root \
  --checkpoint /path/checkpoint.jsonl \
  --log /path/checkpoint.log \
  --workers 4
```

前台 SSH 运行时直接显示 TUI：

```bash
ssh hx@100.67.101.54
/home/hx/md5-copy-cli/md5-copy \
  --raw /mnt/elements-se/wgs \
  --tmpfile /home/hx/AIP2/workspace/dataset/private/fragement/WGS_Raw \
  --checkpoint /home/hx/md5-copy-cli/wgs_md5.checkpoint.jsonl \
  --log /home/hx/md5-copy-cli/wgs_md5.log \
  --workers 4
```

后台运行时使用 `-u`：

```bash
nohup python3 -u /home/hx/md5-copy-cli/md5_copy.py \
  --raw /mnt/elements-se/wgs \
  --tmpfile /home/hx/AIP2/workspace/dataset/private/fragement/WGS_Raw \
  --checkpoint /home/hx/md5-copy-cli/wgs_md5.checkpoint.jsonl \
  --log /home/hx/md5-copy-cli/wgs_md5.log \
  --workers 4 \
  > /home/hx/md5-copy-cli/wgs_md5.out 2>&1 </dev/null &
```

## 分片并行

不同目录可以分别启动，每个分片拥有独立 checkpoint 和 lock：

```bash
./md5-copy --raw /source --tmpfile /target \
  --subdir outputs --workers 2

./md5-copy --raw /source --tmpfile /target \
  --subdir bam_qc_endmer --workers 2
```

不要同时运行重叠分片，例如 `outputs` 与 `outputs/final_bam`。全量运行也会与任意分片运行互斥。

## 断点

同一命令重新运行会自动复用 checkpoint。当前 raw/tmpfile 文件签名未变化的完成项会跳过；变化、缺失或目标文件消失的项会重新检查。

从头运行某个分片：

```bash
./md5-copy --raw /source --tmpfile /target \
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
md5-copy --help
```

CLI 使用纯 Python 标准库，不需要额外运行时依赖。
