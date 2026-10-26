"""Isolated, short-lived objects for the OP storage health check."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
import uuid
from typing import Callable


CACHE_CONTROL = "no-cache, no-store, must-revalidate"


def cache_control_matches(value: str) -> bool:
    # HTTP directives are case-insensitive and their order is insignificant.
    actual = {part.strip().lower() for part in (value or "").split(",")}
    return {"no-cache", "no-store", "must-revalidate"}.issubset(actual)


def _error(error: Exception) -> str:
    """Report useful status codes without echoing credentials or signed URLs."""
    code = getattr(error, "code", None)
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code", code)
    return "%s%s" % (type(error).__name__, " (%s)" % code if code else "")


def check_storage(st: dict, add: Callable, *, store_factory=None,
                  open_url=None, sleep=None) -> None:
    if store_factory is None:
        from .storage.s3 import S3Store
        store_factory = S3Store
    open_url = open_url or urllib.request.urlopen
    sleep = sleep or time.sleep
    token = uuid.uuid4().hex
    # Keep the probe inside the real pack prefix: public access rules may
    # intentionally permit only that prefix. Never reuse a CDN-cached key.
    key = "_doctor/%s/probe.txt" % token
    payload = "doctor-" + token
    store = None
    uploaded = False
    try:
        store = store_factory(st["endpointUrl"], st.get("region", ""), st["bucket"],
                              st.get("prefix", ""), st["accessKey"], st["secretKey"],
                              bool(st.get("pathStyle", True)))
        store.put_bytes(key, payload.encode("utf-8"), CACHE_CONTROL)
        uploaded = True
        add("对象存储上传", True)
        for name, check in (
            ("对象存储 HEAD", lambda: (store.head(key), "上传后对象不可见")),
            ("对象存储内容往返", lambda: (store.get_text(key) == payload, "下载内容与本次上传不一致")),
        ):
            try:
                ok, detail = check()
                add(name, ok, "" if ok else detail)
            except Exception as error:
                add(name, False, _error(error))
        try:
            actual = store.get_cache_control(key)
            ok = cache_control_matches(actual)
            add("对象存储缓存头", ok,
                "" if ok else "期望 %s；实际 %r。请检查对象存储是否保留 Cache-Control。" % (CACHE_CONTROL, actual))
        except Exception as error:
            add("对象存储缓存头", False, _error(error))
        base = (st.get("publicBaseUrl") or "").rstrip("/")
        if base:
            prefix = st.get("prefix", "").strip("/")
            url = base + "/" + (prefix + "/" if prefix else "") + key
            detail = ""
            for attempt in range(3):
                try:
                    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
                    with open_url(request, timeout=15) as response:
                        ok = response.read(len(payload.encode("utf-8")) + 1) == payload.encode("utf-8")
                    detail = "" if ok else "匿名读取内容与本次上传不一致（可能被缓存或回源路径错误）"
                    if ok:
                        break
                except Exception as error:
                    ok = False
                    detail = _error(error) + "；请检查公开读取权限、CDN 回源路径和新对象可见性"
                if attempt < 2:
                    sleep(1)
            add("对象存储匿名读", ok, detail)
        else:
            add("对象存储匿名读", None, "未配置 publicBaseUrl，未验证玩家公开读取")
    except Exception as error:
        add("对象存储上传", False, _error(error))
    finally:
        if store is not None and uploaded:
            try:
                store.delete(key)
                ok = not store.head(key)
                add("对象存储 delete", ok, "" if ok else "删除后对象仍可见")
            except Exception as error:
                add("对象存储 delete", False, _error(error))
