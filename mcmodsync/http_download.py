"""Concurrent HTTP downloader (C side; stdlib only).

Spec section: 7.2 http_download.py, 9.2-9
- honors HTTP_PROXY / HTTPS_PROXY environment variables
- thread pool concurrency (default 4)
- per-file retry 3x with exponential backoff; full-file re-download, no resume
- downloads into staging blob layout blobs/<xx>/<yy>/<sha256>
- verifies sha256+size after each attempt; mismatch -> retry -> exit 5 when exhausted
"""
from __future__ import annotations

import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

EXIT_NETWORK = 5


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


def _fetch(url: str, dst: str, timeout: int = 60) -> int:
    """下载 url -> dst；返回 HTTP 状态码。"""
    opener = _build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "MC-ModSync-C/2.0"})
    with opener.open(req, timeout=timeout) as resp, open(dst, "wb") as f:
        while True:
            block = resp.read(1024 * 1024)
            if not block:
                break
            f.write(block)
        return int(resp.status)


def download_one(url: str, dst: str, expected_sha: str, expected_size: int,
                 verify_fn: Callable[[str], bool], retries: int = 3,
                 log: Optional[Callable[[str], None]] = None) -> None:
    """Download one blob with retries; verify via verify_fn(dst) on final path."""
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(dst), os.getpid()))
    delay = 1.0
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            status = _fetch(url, tmp)
            # 先校验临时文件，通过后才原子落位；失败不污染 dst。
            if not verify_fn(tmp):
                raise DownloadError(5, "下载内容校验失败: %s" % url)
            os.replace(tmp, dst)
            if log:
                log("HTTP %d 已下载 %s (sha256=%s, size=%d)"
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
             retries: int = 3, log: Optional[Callable[[str], None]] = None) -> None:
    """契约入口（[6]/[T-20]）: 下载单文件 -> 校验 sha256 与 size -> 原子落位。

    失败（网络 / HTTP 错误 / 校验不符）整文件重试，指数退避；耗尽后抛
    DownloadError(exit_code=5)。无断点续传。
    """
    download_one(url, dst, expected_sha, expected_size,
                 lambda p: _verify_file(p, expected_sha, expected_size),
                 retries=retries, log=log)


def download_blobs(tasks: List[Dict], blob_base: str, staging: str,
                   concurrency: int = 4, retries: int = 3,
                   log: Optional[Callable[[str], None]] = None,
                   on_done: Optional[Callable[[Dict], None]] = None) -> int:
    """Download all blobs into staging; returns total downloaded bytes.

    tasks: list of {path, sha256, size} entries that need downloading.
    on_done: 每个 blob 落位后回调（供 C 端渲染进度）。
    """
    if not tasks:
        return 0
    total = 0

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

        download_one(url, dst, sha, expected["size"], _verify, retries=retries, log=log)
        if on_done:
            on_done(expected)
        return expected

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for res in pool.map(_work, tasks):
            total += int(res["size"])
    return total
