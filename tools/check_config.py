# -*- coding: utf-8 -*-
"""配置体检: 打印 pack.local.json 的**打码**摘要，并指出缺失/仍为占位符的字段。

用法:
  python tools/check_config.py [--config pack.local.json]

输出: 每个字段一行 —— 非敏感字段显示真值；敏感字段（host/user/password/AK/SK/apiKey/
publicKey）只显示是否已填与长度，绝不回显内容。
退出码: 0 = 关键字段齐备；1 = 存在缺失或占位符。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SENSITIVE = ("host", "user", "password", "keyfile", "accesskey", "secretkey",
             "apikey", "publickey", "privatekeyfile", "token")

FIELDS = [
    ("packId", "整合包唯一标识（服务端/云端/客户端三处一致）"),
    ("packName", "展示名"),
    ("server.host", "SSH 主机"),
    ("server.port", "SSH 端口"),
    ("server.user", "SSH 用户"),
    ("server.authMethod", "认证方式 key/password"),
    ("server.keyFile", "SSH 私钥路径（authMethod=key 时）"),
    ("server.password", "SSH 密码（authMethod=password 时）"),
    ("server.knownHosts", "known_hosts 路径"),
    ("server.serverDir", "服务端根目录（含 mods/）"),
    ("server.modsDir", "服务端 mods 子目录名"),
    ("server.sourceModsDir", "A 本机「服务端侧」源目录"),
    ("server.remoteBPath", "单文件 B 的服务端落地路径"),
    ("server.historyDir", "服务端历史/备份根目录"),
    ("server.historyKeep", "服务端保留版本数"),
    ("client.sourceModsDir", "A 本机「客户端侧」源目录"),
    ("client.manifestUrl", "云端指针 URL（https://<CDN>/<prefix>/manifest.json）"),
    ("client.publicKey", "Ed25519 公钥 Base64"),
    ("client.clientStateFile", "A 本地发布状态文件"),
    ("storage.endpointUrl", "对象存储 S3 endpoint"),
    ("storage.region", "区域"),
    ("storage.pathStyle", "path-style 寻址 true/false"),
    ("storage.bucket", "桶名"),
    ("storage.prefix", "关键前缀（形如 packs/xxxx/）"),
    ("storage.accessKey", "对象存储 AK（子账号）"),
    ("storage.secretKey", "对象存储 SK"),
    ("storage.publicBaseUrl", "CDN 域名"),
    ("curseforge.apiKey", "CurseForge API key"),
    ("signing.privateKeyFile", "签名私钥路径"),
    ("concurrency.upload", "上传并发"),
    ("concurrency.download", "下载并发"),
]

REQUIRED = ("packId", "server.host", "server.user", "server.serverDir",
            "server.sourceModsDir", "client.sourceModsDir", "client.manifestUrl",
            "client.publicKey", "storage.endpointUrl", "storage.bucket",
            "storage.prefix", "storage.accessKey", "storage.secretKey",
            "storage.publicBaseUrl", "signing.privateKeyFile")

PLACEHOLDER_HINTS = ("YOUR_", "/path/to/", "BASE64_PUBLIC_KEY", "my-server-pack")


def get(doc: dict, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def render(dotted: str, value) -> str:
    if value is None or value == "":
        return "(未填)"
    text = str(value)
    if dotted.split(".")[-1].lower() in SENSITIVE:
        return "(已填, %d 字符)" % len(text)
    if len(text) > 72:
        text = text[:69] + "..."
    return text


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="pack.local.json 打码体检")
    ap.add_argument("--config", default="pack.local.json")
    args = ap.parse_args(argv[1:])
    path = os.path.abspath(args.config)
    if not os.path.isfile(path):
        print("找不到配置文件: %s" % path)
        return 1
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)

    print("配置体检: %s" % path)
    print("-" * 78)
    problems = []
    for dotted, desc in FIELDS:
        val = get(doc, dotted)
        shown = render(dotted, val)
        flag = " "
        if dotted in REQUIRED:
            if val in (None, ""):
                flag = "!"
                problems.append("%s 未填（%s）" % (dotted, desc))
            elif any(h in str(val) for h in PLACEHOLDER_HINTS):
                flag = "!"
                problems.append("%s 仍是模板占位符（%s）" % (dotted, desc))
        print("%s %-24s = %-40s  %s" % (flag, dotted, shown, desc))
    print("-" * 78)
    if problems:
        print("需处理 %d 项:" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("关键字段齐备（标记 ! 的项必须处理）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
