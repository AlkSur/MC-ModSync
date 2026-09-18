"""T-02 本机初始化: keygen + 生成 pack.local.json。

凭证策略:
  - SSH 凭证只从 ssh.json 读取（脚本内使用，绝不打印）。
  - 对象存储 AK/SK 从环境变量 MCMS_AK / MCMS_SK 读取（脚本内使用，绝不打印）。
  - 产物 pack.local.json 仅供本机使用，必须被 .gitignore 覆盖（AC-6）。

用法:
  MCMS_AK=... MCMS_SK=... python tools/t02_init_local.py --ssh-json ssh.json
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcmodsync import signing  # noqa: E402

PUBLIC_BASE = "default-u0demo00.cdn.7caiyun.com"


def main() -> int:
    ap = argparse.ArgumentParser(description="T-02 本机初始化")
    ap.add_argument("--ssh-json", default="ssh.json")
    ap.add_argument("--out", default="pack.local.json")
    ap.add_argument("--pack-id", default="demo-pack")
    ap.add_argument("--pack-name", default="我的整合包整合包")
    ap.add_argument("--prefix", default="", help="留空则随机生成 packs/<hex>/")
    ap.add_argument("--endpoint", default="https://s3.7caiyun.com")
    ap.add_argument("--region", default="us-west")
    ap.add_argument("--bucket", default="default-u0demo00")
    ap.add_argument("--public-base", default=PUBLIC_BASE)
    ap.add_argument("--server-root", default="", help="留空则取 ssh.json 的 serverRootDir")
    ap.add_argument("--history-dir", default="/www/mcmodsync-history")
    ap.add_argument("--remote-b-path", default="/www/mcmodsync-b.py")
    ap.add_argument("--history-keep", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 pack.local.json")
    args = ap.parse_args()

    ak = os.environ.get("MCMS_AK", "").strip()
    sk = os.environ.get("MCMS_SK", "").strip()
    if not ak or not sk:
        raise SystemExit("缺少环境变量 MCMS_AK / MCMS_SK")

    if os.path.exists(args.out) and not args.force:
        raise SystemExit("%s 已存在（如需覆盖请加 --force）" % args.out)

    with open(args.ssh_json, "rb") as f:
        ssh = json.loads(f.read().decode("utf-8"))
    for k in ("ip", "port", "username", "serverRootDir"):
        if not ssh.get(k):
            raise SystemExit("ssh.json 缺少字段: %s" % k)

    server_root = (args.server_root or ssh["serverRootDir"]).rstrip("/")
    prefix = args.prefix or ("packs/%s/" % secrets.token_hex(8))

    # keygen（已存在则复用，不覆盖）
    key_path = os.path.expanduser("~/.mcmodsync/private.key")
    created = False
    if not os.path.isfile(key_path):
        pub_b64, key_path = signing.keygen(key_path)
        created = True
    else:
        # 复用已有私钥推导公钥
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open(key_path, "rb") as f:
            k = load_pem_private_key(f.read(), password=None)
        import base64
        pub_b64 = base64.b64encode(k.public_key().public_bytes_raw()).decode("ascii")

    cfg = {
        "packId": args.pack_id,
        "packName": args.pack_name,
        "server": {
            "host": ssh["ip"],
            "port": int(ssh["port"]),
            "user": ssh["username"],
            "authMethod": "password",
            "keyFile": "",
            "password": ssh.get("password", ""),
            "knownHosts": "~/.ssh/known_hosts",
            "serverDir": server_root,
            "modsDir": "mods",
            "sourceModsDir": "./server-mods",
            "remoteBPath": args.remote_b_path,
            "historyDir": args.history_dir,
            "historyKeep": args.history_keep,
        },
        "client": {
            "sourceModsDir": "./client-mods",
            "manifestUrl": "https://%s/%smanifest.json" % (args.public_base, prefix),
            "publicKey": pub_b64,
            "clientStateFile": "./client-publish-state.json",
        },
        "storage": {
            "endpointUrl": args.endpoint,
            "region": args.region,
            "pathStyle": True,
            "bucket": args.bucket,
            "prefix": prefix,
            "accessKey": ak,
            "secretKey": sk,
            "publicBaseUrl": "https://%s" % args.public_base,
        },
        "curseforge": {"apiKey": ""},
        "signing": {"privateKeyFile": "~/.mcmodsync/private.key"},
        "concurrency": {"upload": 4, "download": 4},
    }

    with open(args.out, "wb") as f:
        f.write((json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    print("[OK] 已写入 %s（凭证未回显）" % args.out)
    print("  packId      = %s" % cfg["packId"])
    print("  serverDir   = %s" % server_root)
    print("  historyDir  = %s (keep=%d)" % (args.history_dir, args.history_keep))
    print("  remoteBPath = %s" % args.remote_b_path)
    print("  prefix      = %s" % prefix)
    print("  manifestUrl = %s" % cfg["client"]["manifestUrl"])
    print("  publicKey   = %s..." % pub_b64[:12])
    print("  privateKey  = %s (新建=%s)" % (key_path, created))
    print("  storage.ak/sk = [已写入，长度 %d/%d，不回显]" % (len(ak), len(sk)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
