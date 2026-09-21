"""C 端打包入口与交互输出 —— [T-41]。

- 仅 import 标准库（客户端分发包不得带入 boto3/paramiko/cryptography）。
- 中文控制台输出：阶段、文件计数、总大小、已完成、ASCII 进度条；无 emoji。
- 下载阶段双行渲染（纯 ASCII，规避中日韩字体下 Unicode 方块的双宽错位）：

      正在下载 |来源:Modrinth| 文件名:ModernUI-NeoForge-1.21.1.jar
      [##########----------] 62% | 14.8MB / 24.1MB | 2.3MB/s

  第 1 行只在该文件开始下载时打印一次（被日志打断后随重绘补回）；
  第 2 行用 '\\r' 原地覆写，并以 ANSI \\x1b[K 清行尾残影。
  输出被管道/重定向/日志文件接管时自动降级为普通逐行文本（不做动态刷新）。
- 终端全程不展示 sha256（console.strip_hashes 屏蔽）；哈希细节只写日志文件。
- 日志：<target>/_updater/logs/updater-<日期>.log，含 HTTP 状态码、每个文件的
  sha256 与决策、警告；失败时在终端打印日志路径。
- 打包：本模块只用标准库 + 包内模块，无运行期外部依赖，可直接被 PyInstaller
  打成**独立单文件 exe**（玩家电脑无需安装 Python）。

上述渲染只消费客户端上报的进度事件，不参与下载 / 分片 / 重试 / 哈希校验 / 备份。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, TextIO

from . import client, console
from .logutil import setup_logger

PROG = "mcmodsync"
LOGGER_NAME = "mcmodsync-c"

# 进度条字符：纯 ASCII（填充 #、空白 -），彻底规避字符宽度/字体兼容问题。
# 若需适配极老旧字体，把 BAR_FILL/BAR_EMPTY 换成 "=" / " " 即可（无需改逻辑）。
BAR_FILL = "#"
BAR_EMPTY = "-"
BAR_WIDTH = 20                      # 与预览一致：[##########----------]

# 清单 files[].source -> 终端「来源」标识；缺省/未知一律按对象存储展示
SOURCE_LABELS = {
    "modrinth": "Modrinth",
    "curseforge": "CurseForge",
    "storage": "对象存储",
    "cached": "对象存储",
}
DEFAULT_SOURCE = "对象存储"

# 动态刷新节流：Windows conhost 每次写屏开销较大，限制重绘频率（不影响最终 100%）
PAINT_INTERVAL = 0.04


def log_path_for(target: str) -> str:
    """C 日志: _updater/logs/updater-<日期>.log（[1] 约定）。"""
    return os.path.join(client.updater_dir(target), "logs",
                        "updater-%s.log" % datetime.now().strftime("%Y-%m-%d"))


def source_label(entry: Optional[dict]) -> str:
    """从清单条目读取来源标识（支持 Modrinth / CurseForge / 对象存储）。"""
    raw = str((entry or {}).get("source") or "").strip().lower()
    return SOURCE_LABELS.get(raw, DEFAULT_SOURCE)


class FileProgress:
    """文件级双行进度渲染（纯排版层；只消费埋点事件，不做任何业务判定）。

    事件来源（均为只读上报）：
        start(entry)        文件开始下载 —— 打印第 1 行
        add_bytes(sha, n)   已读字节上报 —— 刷新第 2 行（多分片线程会并发调用）
        reset(sha)          重试/回退单连接 —— 本文件计数归零
        done(entry)         文件落位 —— 第 2 行打到 100% 后直接切下一个文件
    """

    def __init__(self, stream: TextIO, interactive: bool = False,
                 ansi: bool = False, bar_width: int = BAR_WIDTH) -> None:
        self.stream = stream
        self.interactive = interactive
        self.ansi = ansi
        self.bar_width = bar_width
        self.active = False           # 是否已有文件进入下载（决定聚合条是否让位）
        self._lock = threading.RLock()
        self._files: Dict[str, dict] = {}
        self._order: List[str] = []   # 文件启动顺序（挑选下一个展示对象用）
        self._focus: Optional[str] = None   # 当前展示的 sha
        self._open = False            # 双行块是否展开（光标停在进度行）
        self._last_len = 0            # 上一帧宽度（无 ANSI 清行时用空格补残影）
        self._last_paint = 0.0
        self.paint_interval = PAINT_INTERVAL    # 可调（测试可置 0 关闭节流）

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def start(self, entry: dict) -> None:
        sha = str(entry.get("sha256") or "")
        info = {
            "name": os.path.basename(str(entry.get("path") or "")),
            "size": int(entry.get("size") or 0),
            "source": source_label(entry),
            "got": 0,
            "speed": 0.0,
            "t0": time.time(),
            "done": False,
        }
        with self._lock:
            self._files[sha] = info
            self._order.append(sha)
            self.active = True
            if not self.interactive:
                # 非交互（日志/管道）：不动态刷新，只补一行「开始下载」
                self._write(self._line1(info))
                return
            if self._focus is None:
                self._focus = sha
                self._paint()
            # 已有文件正在展示时不打断，等它 100% 后自动接管下一个

    def add_bytes(self, sha: str, n: int) -> None:
        with self._lock:
            info = self._files.get(sha)
            if info is None or info["done"]:
                return
            info["got"] += int(n)
            size = info["size"]
            if size > 0 and info["got"] > size:
                info["got"] = size        # 重试/回退可能重复上报，做钳位
            elapsed = time.time() - info["t0"]
            if elapsed > 0.05:
                info["speed"] = info["got"] / elapsed
            if not self.interactive or self._focus != sha:
                return
            now = time.time()
            if now - self._last_paint >= self.paint_interval:  # 节流重绘
                self._last_paint = now
                self._paint()

    def reset(self, sha: str) -> None:
        with self._lock:
            info = self._files.get(sha)
            if info is not None:
                info["got"] = 0
                info["speed"] = 0.0
                info["t0"] = time.time()
                self._last_paint = 0.0        # 下一块立即重绘，避免残留上一轮百分比

    def done(self, entry: dict) -> None:
        sha = str(entry.get("sha256") or "")
        with self._lock:
            info = self._files.get(sha)
            if info is None:
                return
            info["done"] = True
            info["got"] = info["size"]
            if not self.interactive or self._focus != sha:
                return
            self._paint()                 # 打完 100%（不再单独打印“下载完成”）
            self._close_block()
            nxt = self._next_pending()
            if nxt:
                self._focus = nxt
                self._paint()             # 直接进入下一个文件的双行输出
            else:
                self._focus = None

    # ------------------------------------------------------------------
    # 与普通日志的协作
    # ------------------------------------------------------------------
    def interrupt(self) -> bool:
        """普通日志插入前调用：收尾进度块；返回此前是否停留在进度行上。"""
        with self._lock:
            was_open = self._open
            self._open = False
            self._last_len = 0
            return was_open

    def close(self) -> None:
        """全部输出结束：收尾进度块，避免残留半行。"""
        with self._lock:
            self._close_block()
            self._focus = None
            self._last_len = 0

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _line1(self, info: dict) -> str:
        return "正在下载 |来源:%s| 文件名:%s" % (info["source"], info["name"])

    def _line2(self, info: dict) -> str:
        size = int(info["size"])
        got = int(info["got"])
        frac = 1.0 if size <= 0 else min(1.0, got / float(size))
        filled = int(round(frac * self.bar_width))
        bar = BAR_FILL * filled + BAR_EMPTY * (self.bar_width - filled)
        # 大小按 1 MiB = 1048576 字节换算，标签沿用预览稿的 MB
        return "[%s] %d%% | %.1fMB / %.1fMB | %.1fMB/s" % (
            bar, int(frac * 100), got / 1048576.0, size / 1048576.0,
            info["speed"] / 1048576.0)

    def _write(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def _close_block(self) -> None:
        """结束双行块（保留最后一次进度行不动）。"""
        if self._open:
            self.stream.write("\n")
            self.stream.flush()
        self._open = False
        self._last_len = 0

    def _paint(self) -> None:
        """刷新双行块：必要时先补第 1 行，再原地覆写第 2 行。"""
        info = self._files.get(str(self._focus))
        if info is None:
            return
        if not self._open:
            self._write(self._line1(info))       # 首帧 / 被日志打断后的重绘
        text = self._line2(info)
        if self.ansi:
            tail = "\x1b[K"                      # 清行尾残影（100% -> 9% 不留尾巴）
        else:
            pad = self._last_len - len(text)     # 无 VT：用空格覆盖旧内容
            tail = " " * pad if pad > 0 else ""
        self.stream.write("\r" + text + tail)
        self.stream.flush()
        self._last_len = len(text)
        self._open = True

    def _next_pending(self) -> Optional[str]:
        """挑下一个待展示文件：最近启动且未完成的那个（减少切换次数）。"""
        for sha in reversed(self._order):
            if sha == self._focus:
                continue
            info = self._files.get(sha)
            if info is not None and not info["done"]:
                return sha
        return None


class Console:
    """终端 + 日志文件双写。

    - 终端：按内容关键词着色（复用 mcmodsync.console，纯标准库）；
            并屏蔽 sha256（终端全程不展示哈希）；
    - 日志文件：始终写**无色**原文（含哈希），便于事后排查；
    - 进度：文件级双行原地刷新（FileProgress），完成后直接切下一个文件。
    """

    def __init__(self, logger, stream: Optional[TextIO] = None, bar_width: int = 28,
                 files: Optional[FileProgress] = None) -> None:
        self.logger = logger
        self.stream = stream if stream is not None else sys.stdout
        self.bar_width = bar_width
        self.files = files if files is not None else FileProgress(self.stream)

    def _plain(self, msg: str) -> None:
        """写一行到终端；若进度块正展开，先收尾再写（日志不插进进度行中间）。"""
        if self.files.interrupt():
            self.stream.write("\n")
        self.stream.write(msg + "\n")
        self.stream.flush()

    def _emit(self, plain: str, colored: str) -> None:
        """终端写“屏蔽哈希后的带色文本”，日志文件写“含哈希的纯文本”。"""
        self._plain(colored)
        self.logger.info(plain)

    def log(self, msg: str) -> None:
        self._emit(msg, console.paint_text(console.strip_hashes(msg)))

    def trace(self, msg: str) -> None:
        """技术细节（HTTP 状态码 / sha256 / 分片信息）：只写日志文件，不上终端。"""
        self.logger.info(msg)

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
        self.files.close()
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
        """聚合进度（原有统计信息，保留）。

        交互终端上文件级双行渲染已接管进度显示，这里只写日志；
        非交互（管道/重定向/日志文件）时逐行输出，且不再使用 '\\r'（避免日志串行）。
        """
        if total_files <= 0:
            return
        frac = min(1.0, done_files / float(total_files))
        filled = int(round(frac * self.bar_width))
        bar = BAR_FILL * filled + BAR_EMPTY * (self.bar_width - filled)
        plain = ("进度 [%s] %d%%  %d/%d 个文件  %.1f/%.1f MiB"
                 % (bar, int(frac * 100), done_files, total_files,
                    done_bytes / 1048576.0, total_bytes / 1048576.0))
        if not (self.files.interactive and self.files.active):
            self._plain(plain if done_files < total_files
                        else console.paint(plain, "PASS"))
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


def make_console(logger) -> Console:
    """按输出环境装配控制台渲染（交互终端 -> 双行动态进度）。"""
    stream = sys.stdout
    # 管道 / 重定向 / 显式关闭时降级为逐行输出（日志友好，且不写控制字符）
    interactive = console.is_interactive(stream) and not os.environ.get(
        "MCMODSYNC_NO_PROGRESS")
    ansi = console.enable_vt() if interactive else False
    files = FileProgress(stream, interactive=interactive, ansi=ansi)
    return Console(logger, stream=stream, files=files)


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
    con = make_console(logger)

    con.header(args.command)
    con.log("实例目录：%s" % target)
    con.log("日志文件：%s" % lp)

    if args.command == "sync":
        rc = client.sync(target, strict=args.strict, no_downgrade=args.no_downgrade,
                         keep_backups=args.keep_backups, log=con.log,
                         progress=con.progress,
                         on_file_start=con.files.start,
                         on_file_bytes=con.files.add_bytes,
                         on_file_reset=con.files.reset,
                         on_file_done=con.files.done,
                         trace=con.trace)
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
