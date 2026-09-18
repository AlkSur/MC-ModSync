"""T-00 七彩云（7caiyun）S3 兼容性实测工具。

用途: 逐项验证七彩云对象存储对 MC-ModSync 发布链路的 8 项前置要求，
      输出 JSON 结论，供 docs/七彩云配置.md 引用。

凭证与参数一律来自环境变量，禁止硬编码（G-7）:
  QCY_ENDPOINT      S3 API 接入地址，如 https://s3.7caiyun.com
  QCY_REGION        区域，如 us-west
  QCY_BUCKET        桶名
  QCY_AK            子账号 AccessKey
  QCY_SK            子账号 SecretKey
  QCY_PUBLIC_BASE   对外 HTTPS 域名（不含 scheme），如 <bucket>.cdn.7caiyun.com

用法:
  python tools/t00_verify_7caiyun.py [--prefix packs/t00-<rand>/]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

TIMEOUT = 30
SSE_POINTER = "no-cache, no-store, must-revalidate"
SSE_MANIFEST = "public, max-age=300"
SSE_BLOB = "public, max-age=31536000, immutable"


def _load_env() -> Dict[str, str]:
    keys = ["QCY_ENDPOINT", "QCY_REGION", "QCY_BUCKET", "QCY_AK", "QCY_SK", "QCY_PUBLIC_BASE"]
    cfg: Dict[str, str] = {}
    missing: List[str] = []
    for k in keys:
        v = os.environ.get(k, "").strip()
        if not v:
            missing.append(k)
        cfg[k] = v
    if missing:
        raise SystemExit("缺少环境变量: " + ", ".join(missing))
    return cfg


def _client(cfg: Dict[str, str], addressing: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=cfg["QCY_ENDPOINT"],
        region_name=cfg["QCY_REGION"],
        aws_access_key_id=cfg["QCY_AK"],
        aws_secret_access_key=cfg["QCY_SK"],
        config=Config(
            s3={"addressing_style": addressing},
            signature_version="s3v4",
            connect_timeout=TIMEOUT,
            read_timeout=TIMEOUT,
            retries={"max_attempts": 2},
        ),
    )


def _anon_get(url: str) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.getcode(), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except urllib.error.URLError as e:
        return -1, {}, str(e).encode("utf-8")


def _record(results: List[Dict[str, Any]], item: str, passed: bool, detail: str) -> None:
    results.append({"item": item, "pass": passed, "detail": detail})
    print(("[PASS] " if passed else "[FAIL] ") + item + " -> " + detail)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="T-00 七彩云 S3 兼容性实测")
    ap.add_argument("--prefix", default=None, help="测试前缀（默认随机生成）")
    args = ap.parse_args()

    cfg = _load_env()
    prefix = args.prefix or ("t00-verify-%08x/" % int.from_bytes(os.urandom(4), "big"))
    public_base = cfg["QCY_PUBLIC_BASE"].rstrip("/")
    results: List[Dict[str, Any]] = []

    print("=" * 60)
    print("T-00 七彩云 S3 兼容性实测")
    print("endpoint = " + cfg["QCY_ENDPOINT"])
    print("region   = " + cfg["QCY_REGION"])
    print("bucket   = " + cfg["QCY_BUCKET"])
    print("prefix   = " + prefix)
    print("=" * 60)

    # 步骤2: path-style / virtual-host 探测
    chosen = None
    chosen_client = None
    probe_key = prefix + "_probe"
    for style in ("path", "virtual"):
        try:
            c = _client(cfg, style)
            c.put_object(Bucket=cfg["QCY_BUCKET"], Key=probe_key, Body=b"probe")
            chosen, chosen_client = style, c
            _record(results, "步骤2 addressing_style", True, style + " 可用")
            break
        except (ClientError, BotoCoreError) as e:
            _record(results, "步骤2 addressing_style(" + style + ")", False, repr(e)[:200])
    if chosen_client is None:
        print(json.dumps({"results": results, "fatal": "两种寻址均不可用"}, ensure_ascii=False, indent=2))
        return 1
    s3 = chosen_client
    bucket = cfg["QCY_BUCKET"]

    # 步骤6: 四动作 PutObject / GetObject / DeleteObject / ListBucket
    try:
        s3.put_object(Bucket=bucket, Key=prefix + "put.txt", Body=b"put-ok")
        _record(results, "步骤6 PutObject", True, "OK")
    except (ClientError, BotoCoreError) as e:
        _record(results, "步骤6 PutObject", False, repr(e)[:200])
    try:
        body = s3.get_object(Bucket=bucket, Key=prefix + "put.txt")["Body"].read()
        _record(results, "步骤6 GetObject", body == b"put-ok", "回读内容一致")
    except (ClientError, BotoCoreError) as e:
        _record(results, "步骤6 GetObject", False, repr(e)[:200])
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=10)
        n = len(resp.get("Contents", []))
        _record(results, "步骤6 ListBucket", n > 0, "列出 " + str(n) + " 个对象")
    except (ClientError, BotoCoreError) as e:
        _record(results, "步骤6 ListBucket", False, repr(e)[:200])
    try:
        s3.delete_object(Bucket=bucket, Key=prefix + "put.txt")
        try:
            s3.head_object(Bucket=bucket, Key=prefix + "put.txt")
            _record(results, "步骤6 DeleteObject", False, "删除后仍可 head")
        except ClientError as e:
            ok = e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")
            _record(results, "步骤6 DeleteObject", ok, "删除后 head 返回 " + str(e.response.get("Error", {}).get("Code")))
    except (ClientError, BotoCoreError) as e:
        _record(results, "步骤6 DeleteObject", False, repr(e)[:200])

    # 步骤3: Cache-Control 三类对象可写入并读回
    cc_cases: List[Tuple[str, str, str]] = [
        ("pointer", prefix + "manifest.json", SSE_POINTER),
        ("manifest", prefix + "manifests/1.0.0.json", SSE_MANIFEST),
        ("blob", prefix + "blobs/ab/cd/" + ("a" * 64), SSE_BLOB),
    ]
    for name, key, cc in cc_cases:
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=b"x", ContentType="application/json",
                          CacheControl=cc)
            got = s3.head_object(Bucket=bucket, Key=key).get("CacheControl", "")
            _record(results, "步骤3 Cache-Control[" + name + "]", got == cc,
                    "写入=" + cc + " 读回=" + got)
        except (ClientError, BotoCoreError) as e:
            _record(results, "步骤3 Cache-Control[" + name + "]", False, repr(e)[:200])

    # 步骤5: 公开读（匿名 GET 可见）+ 匿名不可列举
    anon_key = prefix + "public.txt"
    s3.put_object(Bucket=bucket, Key=anon_key, Body=b"public-read-ok", ContentType="text/plain")
    anon_url = "https://" + public_base + "/" + anon_key
    code, _, body = _anon_get(anon_url)
    _record(results, "步骤5 匿名 GET（CDN 域名）", code == 200 and body == b"public-read-ok",
            "HTTP " + str(code) + " url=" + anon_url)
    # 匿名列举: 分别针对 CDN 域名与 S3 接入点；两者都不得返回 200。
    cdn_list_url = "https://" + public_base + "/?list-type=2&prefix=" + prefix
    ccode, _, _ = _anon_get(cdn_list_url)
    _record(results, "步骤5 匿名列举[CDN]应被拒", ccode != 200,
            "HTTP " + str(ccode) + "（非 200 即不可列举）")
    s3_list_url = cfg["QCY_ENDPOINT"].rstrip("/") + "/" + bucket + "/?list-type=2&prefix=" + prefix
    scode, _, _ = _anon_get(s3_list_url)
    _record(results, "步骤5 匿名列举[S3]应被拒", scode != 200,
            "HTTP " + str(scode) + "（非 200 即不可列举）")

    # 步骤7: no-cache 指针匿名 GET 立即可见
    ptr_key = prefix + "manifest-ptr.json"
    s3.put_object(Bucket=bucket, Key=ptr_key, Body=b'{"latest":"1.0.0"}',
                  ContentType="application/json", CacheControl=SSE_POINTER)
    pcode, phdrs, pbody = _anon_get("https://" + public_base + "/" + ptr_key + "?t=" + str(int(time.time())))
    _record(results, "步骤7 no-cache 指针匿名立即可见",
            pcode == 200 and b"1.0.0" in pbody,
            "HTTP " + str(pcode) + " cache-control=" + phdrs.get("Cache-Control", phdrs.get("cache-control", "")))

    # 步骤3/6: 版本清单也做一次匿名可见性（max-age=300）
    ver_key = prefix + "manifests/1.0.0.json"
    vcode, vhdrs, _ = _anon_get("https://" + public_base + "/" + ver_key + "?t=" + str(int(time.time())))
    _record(results, "步骤3 版本清单匿名可见", vcode == 200,
            "HTTP " + str(vcode) + " cache-control=" + vhdrs.get("Cache-Control", vhdrs.get("cache-control", "")))

    # 步骤4: HTTPS 域名
    _record(results, "步骤4 HTTPS 接入", cfg["QCY_ENDPOINT"].startswith("https://") and bool(public_base),
            "endpoint=" + cfg["QCY_ENDPOINT"] + " public=" + public_base)

    # 清理
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        for k in keys:
            s3.delete_object(Bucket=bucket, Key=k)
        _record(results, "清理测试对象", True, "已删除 " + str(len(keys)) + " 个")
    except (ClientError, BotoCoreError) as e:
        _record(results, "清理测试对象", False, repr(e)[:200])

    passed = sum(1 for r in results if r["pass"])
    all_ok = passed == len(results)
    summary = {
        "endpoint": cfg["QCY_ENDPOINT"],
        "region": cfg["QCY_REGION"],
        "bucket": bucket,
        "publicBase": public_base,
        "addressingStyle": chosen,
        "prefix": prefix,
        "passed": passed,
        "total": len(results),
        "allPass": all_ok,
        "results": results,
    }
    print("=" * 60)
    print("结论: " + str(passed) + "/" + str(len(results)) + (" 全部通过" if all_ok else " 存在失败项"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
