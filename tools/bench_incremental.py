# -*- coding: utf-8 -*-
"""AC-8 验收基线：整合包 200~700MB 场景下，单次更新只下载「变更」而非全量。

做法（全程本地，不触碰任何生产资源）:
  1. 生成 N 个 jar 共约 T MB，发布 v1 到本地 HTTP 对象存储替身；
  2. 首次同步（全量下载），记录下载字节；
  3. 改动其中 K 个 jar，发布 v2；
  4. 再次同步，记录下载字节 —— 应约为「K 个 jar 的大小」，而非整包。

用法:
  python tools/bench_incremental.py [--total-mb 200] [--jars 60] [--changed 2] [--keep]
退出码: 0 通过（增量占比 < 20%）；1 失败。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from charness import PACK_ID, Cloud, Instance, make_keys  # noqa: E402


def make_payload(seed: bytes, size: int) -> bytes:
    """确定性伪随机内容（比 os.urandom 稳定可复现）。"""
    out = bytearray()
    h = hashlib.sha256(seed).digest()
    while len(out) < size:
        out += h
        h = hashlib.sha256(h).digest()
    return bytes(out[:size])


def blob_bytes(hits, cloud) -> int:
    """统计本轮命中过的 blob 文件大小合计（按路径去重）。"""
    total = 0
    for path in sorted(set(hits)):
        if not path.startswith("/blobs/"):
            continue
        full = os.path.join(cloud.root, path.lstrip("/").replace("/", os.sep))
        if os.path.isfile(full):
            total += os.path.getsize(full)
    return total


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="AC-8 增量下载基线")
    ap.add_argument("--total-mb", type=int, default=200)
    ap.add_argument("--jars", type=int, default=60)
    ap.add_argument("--changed", type=int, default=2)
    ap.add_argument("--keep", action="store_true", help="保留临时目录以便排查")
    args = ap.parse_args(argv[1:])

    total_bytes = args.total_mb * 1024 * 1024
    per = max(1, total_bytes // args.jars)
    root = tempfile.mkdtemp(prefix="ac8-")
    print("场景: %d 个 jar / 合计约 %d MiB（单 jar 约 %.1f MiB），本版改动 %d 个"
          % (args.jars, args.total_mb, per / 1048576.0, args.changed))

    pem, pub = make_keys(root)
    cloud = Cloud(os.path.join(root, "cloud"), pem, PACK_ID)
    base = cloud.start()
    instance = None
    try:
        # v1
        v1 = {}
        for i in range(args.jars):
            name = "mods/bench-%03d.jar" % i
            v1[name] = make_payload(("v1-%d" % i).encode(), per)
        t0 = time.time()
        cloud.publish("1.0.0", v1)
        print("已发布 v1（%.1f s）" % (time.time() - t0))

        instance = Instance(os.path.join(root, "inst"), base + "/manifest.json", pub)
        t0 = time.time()
        assert instance.sync() == 0, "首次同步失败"
        first = blob_bytes(cloud.hits, cloud)
        print("首次同步（全量）: 下载 %d 个 blob / %.1f MiB，用时 %.1f s"
              % (len([h for h in set(cloud.hits) if h.startswith("/blobs/")]),
                 first / 1048576.0, time.time() - t0))

        # v2: 改动 changed 个
        v2 = dict(v1)
        for i in range(args.changed):
            v2["mods/bench-%03d.jar" % i] = make_payload(("v2-%d" % i).encode(), per)
        cloud.publish("1.0.1", v2)

        cloud.hits.clear()
        t0 = time.time()
        assert instance.sync() == 0, "增量同步失败"
        second = blob_bytes(cloud.hits, cloud)
        ratio = (second / first * 100.0) if first else 0.0
        print("增量同步: 下载 %d 个 blob / %.1f MiB（占全量 %.1f%%），用时 %.1f s"
              % (len([h for h in set(cloud.hits) if h.startswith("/blobs/")]),
                 second / 1048576.0, ratio, time.time() - t0))

        # 一致性复核
        for name, data in v2.items():
            p = os.path.join(instance.root, name)
            assert os.path.isfile(p), "缺少 %s" % name
            if os.path.getsize(p) != len(data):
                raise AssertionError("%s 大小不符" % name)

        ok = ratio < 20.0 and second <= first * 0.2 + (per * (args.changed + 1))
        print("结论: %s（阈值: 增量 < 全量 20%%）" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        cloud.stop()
        if args.keep:
            print("临时目录保留: %s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
