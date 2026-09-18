"""Config loading with required-field validation (A side).

Spec section: 7.2 config.py, 12.x
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict


class ConfigError(Exception):
    pass


class Config(dict):
    """配置对象（[6]/[T-20] 契约的 load_config(path) -> Config）。

    既是 dict（兼容既有下标访问 cfg["server"]），也支持顶层属性访问 cfg.server。
    """

    def __getattr__(self, item: str):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)

    def __setattr__(self, key: str, value: object) -> None:
        self[key] = value


_REQUIRED_TOP = ("packId",)
_REQUIRED_SERVER = ("host", "port", "user", "serverDir", "sourceModsDir",
                    "remoteBPath", "historyDir")
_REQUIRED_CLIENT = ("sourceModsDir", "manifestUrl", "publicKey")
_REQUIRED_STORAGE = ("endpointUrl", "bucket", "prefix", "accessKey", "secretKey")


def _expand(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Expand ~ in string values that look like paths."""
    for key in ("keyFile", "knownHosts", "privateKeyFile", "clientStateFile"):
        if isinstance(obj.get(key), str) and obj[key]:
            obj[key] = os.path.expanduser(obj[key])
    for sub in ("server", "client", "storage", "signing"):
        s = obj.get(sub)
        if isinstance(s, dict):
            _expand(s)
    return obj


def load_config(path: str) -> "Config":
    """Load pack config JSON; missing required fields raise ConfigError naming them."""
    if not os.path.isfile(path):
        raise ConfigError("配置文件不存在: %s" % path)
    with open(path, "rb") as f:
        try:
            cfg = json.loads(f.read().decode("utf-8"))
        except ValueError as e:
            raise ConfigError("配置 JSON 解析失败: %s" % e)
    if not isinstance(cfg, dict):
        raise ConfigError("配置必须是 JSON 对象")
    missing: list = []
    for k in _REQUIRED_TOP:
        if not cfg.get(k):
            missing.append(k)
    srv = cfg.get("server") or {}
    for k in _REQUIRED_SERVER:
        if not srv.get(k) and srv.get(k) != 0:
            missing.append("server.%s" % k)
    cli = cfg.get("client") or {}
    for k in _REQUIRED_CLIENT:
        if not cli.get(k):
            missing.append("client.%s" % k)
    st = cfg.get("storage") or {}
    for k in _REQUIRED_STORAGE:
        if not st.get(k):
            missing.append("storage.%s" % k)
    if missing:
        raise ConfigError("配置缺少必填字段: %s" % ", ".join(missing))
    # defaults
    srv.setdefault("authMethod", "key")
    srv.setdefault("modsDir", "mods")
    srv.setdefault("historyKeep", 3)
    srv.setdefault("password", "")
    srv.setdefault("knownHosts", os.path.expanduser("~/.ssh/known_hosts"))
    st.setdefault("region", "")
    st.setdefault("pathStyle", True)
    st.setdefault("publicBaseUrl", "")
    cfg.setdefault("signing", {})
    cfg["signing"].setdefault("privateKeyFile", os.path.expanduser("~/.mcmodsync/private.key"))
    cfg.setdefault("concurrency", {})
    cfg["concurrency"].setdefault("upload", 4)
    cfg["concurrency"].setdefault("download", 4)
    return Config(_expand(cfg))
