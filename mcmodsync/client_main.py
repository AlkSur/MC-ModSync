"""C 端打包入口与交互输出 —— [T-41]。

- 仅 import 标准库（客户端分发包不得带入 boto3/paramiko/cryptography）。
- 中文控制台输出：阶段、文件计数、总大小、已完成、纯文本进度条；无 emoji。
- 日志：<target>/_updater/logs/updater-<日期>.log，含 HTTP 状态码、每个文件的
  sha256 与决策、警告；失败时在终端打印日志路径。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import List, Optional, TextIO

from . import client, console
from .logutil import setup_logger

PROG = "mcmodsync"
LOGGER_NAME = "mcmodsync-c"


def log_path_for(target: str) -> str:
    """C 日志: _updater/logs/updater-<日期>.log（[1] 约定）。"""
    return os.path.join(client.updater_dir(target), "logs",
                        "updater-%s.log" % datetime.now().strftime("%Y-%m-%d"))


class Console:
    """终端 + 日志文件双写。

    - 终端：按内容关键词着色（复用 mcmodsync.console，纯标准库）；
    - 日志文件：始终写**无色**原文，便于事后查看；
    - 进度条：原地刷新，完成时转绿。
    """

    def __init__(self, logger, stream: Optional[TextIO] = None, bar_width: int = 28) -> None:
        self.logger = logger
        self.stream = stream if stream is not None else sys.stdout
        self.bar_width = bar_width
        self._bar_open = False

    def _plain(self, msg: str) -> None:
        if self._bar_open:
            self.stream.write("\n")
            self._bar_open = False
        self.stream.write(msg + "\n")
        self.stream.flush()

    def _emit(self, plain: str, colored: str) -> None:
        """终端写带色文本、日志文件写纯文本（颜色码绝不能进日志）。"""
        self._plain(colored)
        self.logger.info(plain)

    def log(self, msg: str) -> None:
        self._emit(msg, console.paint_text(msg))

    def rule(self, title: str = "") -> None:
        """分隔线；带标题时先分隔再输出标题。"""
        bar = "-" * 62
        self._emit(bar, console.dim(bar))
        if title:
            self._emit(title, console.bold(title))

    def header(self, command: str) -> None:
        bar = "=" * 62
        title = "MC-ModSync 客户端  %s   |   %s" % (
            command, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._emit(bar, console.dim(bar))
        self._emit(title, console.bold(title))
        self._emit(bar, console.dim(bar))

    def finish(self, rc: int, log_path: str) -> None:
        bar = "=" * 62
        self._emit(bar, console.dim(bar))
        if rc == 0:
            text = "完成：成功（退出码 0）"
            self._emit(text, console.paint(text, "PASS"))
        else:
            text = "完成：失败（退出码 %d）" % rc
            self._emit(text, console.paint(text, "FAIL"))
            self.log("详细日志：%s" % log_path)

    def progress(self, done_files: int, total_files: int,
                 done_bytes: int, total_bytes: int) -> None:
        if total_files <= 0:
            return
        frac = min(1.0, done_files / float(total_files))
        filled = int(round(frac * self.bar_width))
        bar = "#" * filled + "-" * (self.bar_width - filled)
        plain = ("进度 [%s] %3d%%  %d/%d 个文件  %.1f/%.1f MiB"
                 % (bar, int(frac * 100), done_files, total_files,
                    done_bytes / 1048576.0, total_bytes / 1048576.0))
        line = console.paint(plain, "PASS") if done_files >= total_files else plain
        self.stream.write("\r" + line)
        if done_files >= total_files:
            self.stream.write("\n")
            self._bar_open = False
        else:
            self._bar_open = True
        self.stream.flush()
        self.logger.debug(plain)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="MC-ModSync 客户端（C 端）")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--target", default="",
                        help="实例目录（缺省取 exe 所在目录的上一级）")
        sp.add_argument("--no-pause", action="store_true",
                        help="结束后不等待按键（由入口脚本 更新mod.bat/.sh 使用）")

    s = sub.add_parser("sync", help="同步 mod")
    common(s)
    s.add_argument("--strict", action="store_true", help="删除清单外的自装 jar")
    s.add_argument("--no-downgrade", action="store_true", help="版本回退时终止（码 1）")
    s.add_argument("--keep-backups", type=int, default=client.DEFAULT_KEEP_BACKUPS,
                   help="保留的备份份数（默认 1）")

    v = sub.add_parser("verify", help="只校验不修改")
    common(v)
    v.add_argument("--strict", action="store_true")

    common(sub.add_parser("rollback", help="回滚到最近一次备份"))
    common(sub.add_parser("doctor", help="环境与配置体检"))
    return p


def main(argv: Optional[List[str]] = None) -> int:
    try:                                     # 中文控制台防乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args(argv)
    target = os.path.abspath(args.target) if args.target else client.default_target()
    lp = log_path_for(target)
    logger = setup_logger(lp, name=LOGGER_NAME, console=False)
    con = Console(logger)

    con.header(args.command)
    con.log("实例目录：%s" % target)
    con.log("日志文件：%s" % lp)

    if args.command == "sync":
        rc = client.sync(target, strict=args.strict, no_downgrade=args.no_downgrade,
                         keep_backups=args.keep_backups, log=con.log, progress=con.progress)
    elif args.command == "verify":
        rc = client.verify(target, strict=args.strict, log=con.log)
    elif args.command == "rollback":
        rc = client.rollback(target, log=con.log)
    else:
        rc = client.doctor(target, log=con.log)

    con.finish(rc, lp)
    return rc


if __name__ == "__main__":
    sys.exit(main())
