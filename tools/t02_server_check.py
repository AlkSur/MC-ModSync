"""T-02 服务端环境核验。

凭证来源: ssh.json（由调用方提供，字段 ip/port/username/password/serverRootDir）。
本脚本仅在运行期读取凭证，绝不打印、记录或回显任何凭证内容。

用法:
  python tools/t02_server_check.py --ssh-json ssh.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcmodsync.ssh import SSHClient, SSHError  # noqa: E402


def load_ssh(path: str) -> dict:
    with open(path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    for k in ("ip", "port", "username", "serverRootDir"):
        if not data.get(k):
            raise SystemExit("ssh.json 缺少字段: %s" % k)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="T-02 服务端环境核验（凭证不落屏）")
    ap.add_argument("--ssh-json", default="ssh.json")
    args = ap.parse_args()

    cfg = load_ssh(args.ssh_json)
    host = cfg["ip"]
    port = int(cfg["port"])
    user = cfg["username"]
    root = cfg["serverRootDir"].rstrip("/")

    cli = SSHClient()
    try:
        cli.connect(host, port, user, password=cfg.get("password", ""), accept_new_host=True)
    except SSHError as e:
        print("[FAIL] SSH 连接失败: %s" % e)
        return 1
    print("[PASS] SSH 连接成功（%s@%s:%d）" % (user, host, port))

    def run(cmd: str):
        try:
            rc, out, err = cli.run(cmd)
            return rc, out.strip(), err.strip()
        except SSHError as e:
            return -1, "", str(e)

    checks = [
        ("远程用户", "id -un"),
        ("python3 路径", "command -v python3 || echo '(无 python3)'"),
        ("python3 版本", "python3 --version 2>&1 || echo '(无 python3)'"),
        ("根目录存在性", "test -d '%s' && echo EXIST || echo MISSING" % root),
        ("根目录属性", "ls -ld '%s' 2>&1" % root),
        ("mods 目录", "ls -ld '%s/mods' 2>&1 || echo '(mods 不存在)'" % root),
        ("mods 内 jar 数", "ls -1 '%s/mods'/*.jar 2>/dev/null | wc -l" % root),
        ("磁盘(根目录)", "df -h '%s' 2>&1 | tail -n +1" % root),
        ("/www 存在性", "test -d /www && echo EXIST || echo MISSING"),
        ("/www 可写性", "if [ -w /www ]; then touch /www/.mcms-wtest && rm -f /www/.mcms-wtest && echo WRITABLE; else echo NOT_WRITABLE; fi"),
        ("/www 属性", "ls -ld /www 2>&1 || echo '(无 /www)'"),
        ("HOME 可写性", "touch \"$HOME/.mcms-wtest\" && rm -f \"$HOME/.mcms-wtest\" && echo WRITABLE || echo NOT_WRITABLE"),
        ("mods 体积", "du -sh '%s/mods' 2>/dev/null | cut -f1 || echo '(未知)'" % root),
        ("根目录体积", "du -sh '%s' 2>/dev/null | cut -f1 || echo '(未知)'" % root),
        ("history 目录现状", "ls -ld /www/mcmodsync-history 2>&1 || echo '(不存在)'"),
        ("B 脚本现状", "ls -l /www/mcmodsync-b.py 2>&1 || echo '(不存在)'"),
        ("内存", "free -m 2>/dev/null | head -2 || echo '(无 free)'"),
        ("CPU 核数", "nproc 2>/dev/null || echo '(未知)'"),
        ("Java 进程", "ps -eo pid,comm 2>/dev/null | grep -i java | head -5 || echo '(无 java 进程)'"),
    ]
    for name, cmd in checks:
        rc, out, err = run(cmd)
        print("--- %s (rc=%d)" % (name, rc))
        if out:
            print(out)
        if err:
            print("[stderr] " + err)

    cli.close()
    print("[DONE] 服务端核验结束（未输出任何凭证）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
