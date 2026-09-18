"""T-02 本机配置校验: 加载 pack.local.json 并输出打码摘要（不回显凭证）。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcmodsync.config import load_config  # noqa: E402
from mcmodsync.logutil import mask_text  # noqa: E402


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "pack.local.json"
    cfg = load_config(path)
    safe = json.loads(mask_text(json.dumps(cfg, ensure_ascii=False)))
    print("load_config OK: %s" % path)
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    print("--- 抽查 ---")
    print("server.password 长度 :", len(cfg["server"].get("password", "")), "(不回显)")
    print("storage.accessKey    :", "[已打码]" if "accessKey" in safe["storage"] else "?")
    print("client.publicKey 长度:", len(cfg["client"]["publicKey"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
