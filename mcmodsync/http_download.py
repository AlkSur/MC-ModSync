"""Concurrent HTTP downloader (C side; stdlib only).

Spec section: 7.2 http_download.py, 9.2-9
- honors HTTP_PROXY / HTTPS_PROXY environment variables
- thread pool concurrency (default 4)
- per-file retry 3x with exponential backoff; full-file re-download, no resume
- downloads into staging blob layout blobs/<xx>/<yy>/<sha256>
- verifies sha256+size after each attempt; mismatch -> retry -> exit 5 when exhausted

控制台埋点（**只上报、不做任何判定**，不参与下载/分片/重试/校验/备份的控制流）:
    on_bytes(n)  读取循环每读到一个数据块即上报其字节数（文件级进度渲染用）
    on_reset()   每次尝试开始时上报“重新计数”（重试 / 分片回退单连接时）
    trace(msg)   技术性日志（HTTP 状态码、sha256、分片数）；缺省沿用 log。
                 终端不展示哈希时由调用方传入“只写日志文件”的回调。
"""
from __future__ import annotations

import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

EXIT_NETWORK = 5

# 字节上报埋点：参数为“本次读取到的字节数”。为 None 时表示不渲染进度。
ProgressFn = Optional[Callable[[int], None]]

# 分片下载参数
DEFAULT_TOTAL_SLOTS = 8              # 分片下载的总连接预算（所有文件共享）
SEGMENT_MIN_SIZE = 2 * 1024 * 1024   # 小于此值不分片（分片反而多几次 RTT）
SEGMENT_SIZE = 1024 * 1024           # 单个分片的目标大小
SEGMENT_MAX = 8                      # 单文件最多分几片

# 平台直链（Modrinth / CurseForge 官方 CDN）尝试参数：只试 1 次、短超时、不重试。
# 这些站点在国内连通性不稳定，必须"快速失败 + 静默回落"，
# 绝不能套用对象存储那套「3 次 × 递增退避」（否则玩家要干等 3 分钟才回落）。
PLATFORM_TIMEOUT = 8
PLATFORM_RETRIES = 1


class DownloadError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _build_opener() -> urllib.request.OpenerDirector:
    handlers: List[urllib.request.BaseHandler] = []
    if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
        handlers.append(urllib.request.ProxyHandler({
            "http": os.environ.get("HTTP_PROXY") or None,
            "https": os.environ.get("HTTPS_PROXY") or None,
        }))
    return urllib.request.build_opener(*handlers)


def _fetch(url: str, dst: str, timeout: int = 60,
           progress: ProgressFn = None) -> int:
    """下载 url -> dst；返回 HTTP 状态码。"""
    opener = _build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "MC-ModSync-C/2.0"})
    with opener.open(req, timeout=timeout) as resp, open(dst, "wb") as f:
        while True:
            block = resp.read(1024 * 1024)
            if not block:
                break
            f.write(block)
            if progress:                     # 埋点：上报本块字节数（不改控制流）
                progress(len(block))
        return int(resp.status)


def _auto_segments(size: int, limit: int = SEGMENT_MAX,
                   segment_size: int = SEGMENT_SIZE) -> int:
    """按文件大小决定分片数（1 表示不分片）。"""
    if size <= 0 or size < SEGMENT_MIN_SIZE:
        return 1
    n = (size + segment_size - 1) // segment_size
    return max(1, min(limit, n))


def _fetch_segment(url: str, dst: str, start: int, end: int,
                   timeout: int = 60, progress: ProgressFn = None) -> int:
    """下载 [start, end] 字节区间到 dst；要求源站返回 206，否则视为不支持分片。"""
    opener = _build_opener()
    req = urllib.request.Request(url, headers={
        "User-Agent": "MC-ModSync-C/2.0",
        "Range": "bytes=%d-%d" % (start, end),
    })
    got = 0
    with opener.open(req, timeout=timeout) as resp, open(dst, "wb") as f:
        status = int(resp.status)
        while True:
            block = resp.read(256 * 1024)
            if not block:
                break
            f.write(block)
            got += len(block)
            if progress:                     # 埋点：上报本块字节数（不改控制流）
                progress(len(block))
    if status != 206:
        raise DownloadError(EXIT_NETWORK, "源站不支持分片（HTTP %d）" % status)
    want = end - start + 1
    if got != want:
        raise DownloadError(EXIT_NETWORK, "分片长度不符: 期望 %d 实得 %d" % (want, got))
    return status


def _fetch_segmented(url: str, dst: str, size: int, segments: int,
                     segment_size: int = SEGMENT_SIZE, timeout: int = 60,
                     log: Optional[Callable[[str], None]] = None,
                     progress: ProgressFn = None,
                     trace: Optional[Callable[[str], None]] = None) -> int:
    """多分片并发下载后按序合并到 dst。任一分片失败即抛异常（由上层回退单连接）。"""
    seg_size = max(segment_size, (size + segments - 1) // segments)
    ranges: List = []
    pos = 0
    while pos < size:
        end = min(pos + seg_size - 1, size - 1)
        ranges.append((pos, end))
        pos = end + 1

    parts: List[str] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(ranges))) as pool:
            futures = []
            for i, (s, e) in enumerate(ranges):
                p = "%s.p%02d" % (dst, i)
                parts.append(p)
                # 分片各自上报字节（渲染端负责加锁累加，这里只是埋点透传）
                futures.append(pool.submit(_fetch_segment, url, p, s, e, timeout,
                                           progress))
            for f in futures:
                f.result()                      # 任一分片异常在此抛出
        with open(dst, "wb") as out:
            for p in parts:
                with open(p, "rb") as src:
                    while True:
                        block = src.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block)
        detail = trace or log                   # 技术细节：默认进日志文件
        if detail:
            detail("分片下载完成: %d 片 × 约 %.1f MiB"
                   % (len(ranges), seg_size / 1048576.0))
        return 206
    finally:
        for p in parts:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def download_one(url: str, dst: str, expected_sha: str, expected_size: int,
                 verify_fn: Callable[[str], bool], retries: int = 3,
                 log: Optional[Callable[[str], None]] = None,
                 segments: int = 0,
                 on_bytes: Optional[Callable[[int], None]] = None,
                 on_reset: Optional[Callable[[], None]] = None,
                 trace: Optional[Callable[[str], None]] = None,
                 timeout: int = 60) -> None:
    """Download one blob with retries; verify via verify_fn(dst) on final path.

    segments > 1 时优先多分片并发下载（大文件提速显著）；分片失败自动回退单连接，
    两条路径下载的都写入同一个 tmp，校验与原子落位逻辑完全一致。

    on_bytes / on_reset 仅为**控制台渲染埋点**：上报已读字节与“重新计数”信号，
    不参与重试判定、不参与校验、不改变任何控制流。trace 用于承载含 sha256 的
    技术性日志（终端不展示哈希时，由调用方传“只写日志文件”的回调）。
    """
    detail = trace or log
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(dst), os.getpid()))
    # segments: 0=自动（默认上限 8 片）；1=强制单连接；>1=该文件的分片上限
    limit = segments if segments > 0 else SEGMENT_MAX
    use_segments = 1 if segments == 1 else _auto_segments(expected_size, limit)
    delay = 1.0
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            if on_reset:                        # 埋点：本次尝试从 0 重新计数
                on_reset()
            status = 0
            if use_segments > 1:
                try:
                    status = _fetch_segmented(url, tmp, expected_size, use_segments,
                                              log=log, progress=on_bytes, trace=trace,
                                              timeout=timeout)
                except Exception as e:          # noqa: BLE001  回退单连接
                    if log:
                        log("分片下载未成功，改用单连接: %s" % e)
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    if on_reset:                # 埋点：回退单连接后重新计数
                        on_reset()
                    status = _fetch(url, tmp, timeout=timeout, progress=on_bytes)
            else:
                status = _fetch(url, tmp, timeout=timeout, progress=on_bytes)
            # 先校验临时文件，通过后才原子落位；失败不污染 dst。
            if not verify_fn(tmp):
                raise DownloadError(5, "下载内容校验失败: %s" % url)
            os.replace(tmp, dst)
            if detail:
                detail("HTTP %d 已下载 %s (sha256=%s, size=%d)"
                       % (status, os.path.basename(dst), expected_sha or "-",
                          int(os.path.getsize(dst))))
            return
        except Exception as e:
            last_err = e
            if log:
                log("下载失败(第 %d 次): %s: %s" % (attempt, url, e))
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    raise DownloadError(EXIT_NETWORK, "下载重试耗尽: %s (%s)" % (url, last_err))


def _verify_file(path: str, expected_sha: str, expected_size: int) -> bool:
    import hashlib
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest() == expected_sha and size == expected_size


def download(url: str, dst: str, expected_sha: str, expected_size: int,
             retries: int = 3, log: Optional[Callable[[str], None]] = None,
             segments: int = 0,
             on_bytes: Optional[Callable[[int], None]] = None,
             on_reset: Optional[Callable[[], None]] = None,
             trace: Optional[Callable[[str], None]] = None) -> None:
    """契约入口（[6]/[T-20]）: 下载单文件 -> 校验 sha256 与 size -> 原子落位。

    失败（网络 / HTTP 错误 / 校验不符）整文件重试，指数退避；耗尽后抛
    DownloadError(exit_code=5)。无断点续传。
    大文件默认多分片并发下载（segments=1 可强制关闭）。
    """
    download_one(url, dst, expected_sha, expected_size,
                 lambda p: _verify_file(p, expected_sha, expected_size),
                 retries=retries, log=log, segments=segments,
                 on_bytes=on_bytes, on_reset=on_reset, trace=trace)


def _try_platform(url: str, dst: str, sha: str, size: int,
                  verify_fn: Callable[[str], bool],
                  on_bytes: Optional[Callable[[int], None]] = None,
                  on_reset: Optional[Callable[[], None]] = None,
                  trace: Optional[Callable[[str], None]] = None) -> bool:
    """平台直链尝试：1 次、超时 8s、不重试；成功返回 True。

    复用 download_one 的完整路径（分段/校验/原子落位逻辑一行不改）。
    失败**静默** —— 终端不打印任何字样，只在 trace（日志文件）里留一行，
    便于事后区分"这个文件本来就没有直链"和"直链挂了"。
    """
    try:
        download_one(url, dst, sha, size, verify_fn,
                     retries=PLATFORM_RETRIES, log=None, segments=1,
                     timeout=PLATFORM_TIMEOUT,
                     on_bytes=on_bytes, on_reset=on_reset, trace=trace)
        return True
    except Exception as e:  # noqa: BLE001  任何失败都回落；详情只进日志
        if trace:
            try:
                trace("平台直链不可用，已回落对象存储: %s（%s）"
                      % (os.path.basename(dst), e))
            except Exception:
                pass
        return False


def download_blobs(tasks: List[Dict], blob_base: str, staging: str,
                   concurrency: int = 4, retries: int = 3,
                   log: Optional[Callable[[str], None]] = None,
                   on_done: Optional[Callable[[Dict], None]] = None,
                   on_start: Optional[Callable[[Dict], None]] = None,
                   on_bytes: Optional[Callable[[str, int], None]] = None,
                   on_reset: Optional[Callable[[str], None]] = None,
                   trace: Optional[Callable[[str], None]] = None) -> int:
    """Download all blobs into staging; returns total downloaded bytes.

    tasks: list of {path, sha256, size} entries that need downloading.
      可选字段：altUrl —— 平台 CDN 直链，非空时**先试它**（1 次 / 8s / 不重试），
                失败静默回落对象存储；source —— 仅用于控制台"来源"标签。
    on_done: 每个 blob 落位后回调（供 C 端渲染进度）。
    on_start(entry) / on_bytes(sha, n) / on_reset(sha): 文件级进度渲染埋点，
        只上报不判定；on_bytes 可能在多个分片线程中并发调用，渲染端需自行加锁。
    """
    if not tasks:
        return 0
    total = 0
    # 分片并发与文件级并发共享总连接预算：一次只下 1~2 个文件时单文件多开几片，
    # 文件很多时每文件少开几片，避免 4 文件 × 8 片 = 32 连接把玩家带宽打满。
    in_flight = max(1, min(concurrency, len(tasks)))
    per_file = max(1, min(SEGMENT_MAX, DEFAULT_TOTAL_SLOTS // in_flight))
    if log:
        log("下载策略: 文件并发 %d × 单文件最多 %d 片（总连接 ≈ %d）"
            % (in_flight, per_file, min(in_flight * per_file, DEFAULT_TOTAL_SLOTS * 2)))

    def _work(expected: Dict) -> Dict:
        sha = expected["sha256"]
        url = "%s/blobs/%s/%s/%s" % (blob_base.rstrip("/"), sha[0:2], sha[2:4], sha)
        dst = os.path.join(staging, "blobs", sha[0:2], sha[2:4], sha)

        def _verify(dst_checked: str) -> bool:
            import hashlib
            h = hashlib.sha256()
            size = 0
            with open(dst_checked, "rb") as f:
                while True:
                    block = f.read(1024 * 1024)
                    if not block:
                        break
                    h.update(block)
                    size += len(block)
            return h.hexdigest() == sha and size == expected["size"]

        if on_start:                             # 埋点：文件开始下载（渲染第 1 行）
            on_start(expected)

        alt = str(expected.get("altUrl") or "")
        if alt:
            # 平台直链优先；失败静默回落（终端无任何输出），计数归零后走原有全流程
            if _try_platform(alt, dst, sha, expected["size"], _verify,
                             on_bytes=(lambda n: on_bytes(sha, n)) if on_bytes else None,
                             on_reset=(lambda: on_reset(sha)) if on_reset else None,
                             trace=trace):
                if on_done:
                    on_done(expected)
                return expected
            if on_reset:
                on_reset(sha)

        download_one(url, dst, sha, expected["size"], _verify, retries=retries,
                     log=log, segments=per_file,
                     on_bytes=(lambda n: on_bytes(sha, n)) if on_bytes else None,
                     on_reset=(lambda: on_reset(sha)) if on_reset else None,
                     trace=trace)
        if on_done:
            on_done(expected)
        return expected

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for res in pool.map(_work, tasks):
            total += int(res["size"])
    return total
