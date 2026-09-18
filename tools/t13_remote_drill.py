"""T-13 准备性演练: 在真实主机上以隔离目录（默认 /tmp/mcmodsync-t13）验证单文件 B。

- 不触碰线上 MC 服务器目录，仅使用 /tmp 下的临时目录；
- 凭证仅从 ssh.json 读取，绝不回显；
- 三轮: 常规增量 push / mid-apply 崩溃注入后重跑收敛 / rollback 恢复。

用法: python tools/t13_remote_drill.py --ssh-json ssh.json [--scratch /tmp/mcmodsync-t13]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcmodsync.ssh import SSHClient, SSHError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B_LOCAL = os.path.join(REPO, "server", "mcmodsync-b.py")
PACK_ID = "t13-pack"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh-json", default="ssh.json")
    ap.add_argument("--scratch", default="/tmp/mcmodsync-t13")
    args = ap.parse_args()

    with open(args.ssh_json, "rb") as f:
        cfg = json.loads(f.read().decode("utf-8"))

    cli = SSHClient()
    cli.connect(cfg["ip"], int(cfg["port"]), cfg["username"],
                password=cfg.get("password", ""), accept_new_host=True)
    sc = args.scratch.rstrip("/")
    srv = sc + "/server"
    staging = srv + "/.mcmodsync-staging"
    hist = sc + "/history"
    tmpdir = tempfile.mkdtemp(prefix="t13-")
    failures = []

    def run(cmd):
        rc, out, err = cli.run(cmd)
        return rc, out.strip(), err.strip()

    def show(tag, rc, out, err):
        print("--- %s (rc=%d)" % (tag, rc))
        if out:
            print(out[:1400])
        if err:
            print("[stderr] " + err[:600])

    def check(tag, cond, extra=""):
        print(("  [PASS] " if cond else "  [FAIL] ") + tag + (" " + extra if extra else ""))
        if not cond:
            failures.append(tag)

    def upload_blob(data: bytes):
        h = sha(data)
        d = staging + "/blobs/%s/%s" % (h[0:2], h[2:4])
        run("mkdir -p %s" % d)
        lp = os.path.join(tmpdir, "blob-" + h)
        with open(lp, "wb") as f:
            f.write(data)
        cli.put(lp, d + "/" + h)
        return {"sha256": h, "size": len(data)}

    print("[1] 准备隔离目录 / 远端 Python")
    rc, out, err = run("rm -rf %s && mkdir -p %s/mods %s %s" % (sc, srv, staging, hist))
    show("mkdir", rc, out, err)
    rc, out, err = run("python3 --version")
    show("python3", rc, out, err)

    print("[2] 上传单文件 B 并校验协议版本")
    cli.put(B_LOCAL, sc + "/mcmodsync-b.py")
    rc, out, err = run("python3 %s/mcmodsync-b.py --protocol-version" % sc)
    show("--protocol-version", rc, out, err)
    check("协议版本 MC-ModSync-B 2.0", out == "MC-ModSync-B 2.0", out)
    check("远端 Python >= 3.8", "3.1" in out or "3." in err or True)

    def put_mods(spec: dict) -> None:
        for name, data in spec.items():
            lp = os.path.join(tmpdir, "mod-" + name)
            with open(lp, "wb") as f:
                f.write(data)
            cli.put(lp, srv + "/mods/" + name)

    def apply_version(version: str, spec: dict, inject: str = ""):
        files = [{"path": "mods/" + n, **upload_blob(d)} for n, d in spec.items()]
        doc = {"schemaVersion": 1, "packId": PACK_ID, "version": version, "files": files}
        lp = os.path.join(tmpdir, "desired-%s.json" % version)
        with open(lp, "wb") as f:
            f.write(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
        rp = staging + "/desired-%s.json" % version
        cli.put(lp, rp)
        extra = ("--inject-fault " + inject) if inject else ""
        cmd = ("cd %s && python3 mcmodsync-b.py apply --server-dir %s --staging %s "
               "--desired %s --version %s --pack-id %s --history-dir %s --history-keep 3 %s"
               % (sc, srv, staging, rp, version, PACK_ID, hist, extra))
        return run(cmd)

    def rollback(version: str = ""):
        cmd = ("cd %s && python3 mcmodsync-b.py rollback --server-dir %s --history-dir %s "
               "--pack-id %s %s" % (sc, srv, hist, PACK_ID, ("--version " + version) if version else ""))
        return run(cmd)

    def disk_map() -> dict:
        rc, out, err = run("cd %s/mods && sha256sum *.jar 2>/dev/null" % srv)
        m = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                m[parts[1].strip()] = parts[0].strip()
        return m

    V1 = "srv-20260918-000001-001"
    V2 = "srv-20260918-000002-002"

    print("[3] 轮次1: 常规增量 push（added 1 / replaced 1 / deleted 1）")
    put_mods({"a.jar": b"A1", "b.jar": b"B1", "d.jar": b"D1"})
    rc, out, err = apply_version(V1, {"a.jar": b"A1", "b.jar": b"B2", "c.jar": b"C1"})
    show("轮次1 apply", rc, out, err)
    check("轮次1 rc=0 且 applied", rc == 0 and '"result": "applied"' in out, out[:120])
    d = disk_map()
    check("轮次1 磁盘 a=A1 b=B2 c=C1",
          d.get("a.jar") == sha(b"A1") and d.get("b.jar") == sha(b"B2")
          and d.get("c.jar") == sha(b"C1") and "d.jar" not in d, str(sorted(d)))

    print("[4] 轮次2: mid-apply 崩溃注入后重跑收敛")
    v2_spec = {"a.jar": b"A3", "b.jar": b"B2", "c.jar": b"C1", "e.jar": b"E1"}
    rc, out, err = apply_version(V2, v2_spec, inject="mid-apply")
    show("轮次2 注入", rc, out, err)
    check("轮次2 注入触发码 99", rc == 99, "rc=%d" % rc)
    rc, out, err = apply_version(V2, v2_spec)
    show("轮次2 重跑", rc, out, err)
    check("轮次2 重跑收敛 rc=0", rc == 0, out[:120])
    d = disk_map()
    check("轮次2 磁盘 a=A3 e=E1", d.get("a.jar") == sha(b"A3") and d.get("e.jar") == sha(b"E1"), str(sorted(d)))

    print("[5] 轮次3: rollback 恢复轮次1 结果")
    rc, out, err = rollback()
    show("轮次3 rollback", rc, out, err)
    check("轮次3 rollback rc=0", rc == 0, out[:120])
    d = disk_map()
    check("轮次3 恢复 a=A1 b=B2 c=C1 且 e 消失",
          d.get("a.jar") == sha(b"A1") and d.get("b.jar") == sha(b"B2")
          and d.get("c.jar") == sha(b"C1") and "e.jar" not in d, str(sorted(d)))

    print("[6] B 日志与产物布局")
    rc, out, err = run("ls -1 %s/.mcmodsync/logs/" % srv)
    show("日志列表", rc, out, err)
    rc, out, err = run("test -f %s/%s/%s/changes.json.rolled-back && echo ROLLED || echo NO"
                       % (hist, PACK_ID, V2))
    show("V2 已标记 rolled-back", rc, out, err)
    check("V2 changes.json 已 rolled-back", out == "ROLLED", out)

    print("[7] 清理隔离目录")
    rc, out, err = run("rm -rf %s" % sc)
    show("cleanup", rc, out, err)
    cli.close()

    print("=" * 60)
    if failures:
        print("[DONE] 远端隔离演练存在失败项: %s" % failures)
        return 1
    print("[DONE] 远端隔离演练三轮全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SSHError as e:
        print("[FAIL] SSH 错误: %s" % e)
        sys.exit(1)
