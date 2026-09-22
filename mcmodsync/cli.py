"""A 端 CLI 入口与子命令分发（[T-23]）。

  mcmodsync keygen
  mcmodsync doctor -c pack.local.json
  mcmodsync fetch-mods -c pack.local.json [--upgrade] [--lock-manual] [--dry-run]
  mcmodsync push-server -c pack.local.json [--dry-run] [--check-server-client] [--accept-new-host]
  mcmodsync rollback-server -c pack.local.json
  mcmodsync publish-client -c pack.local.json [--version <A.B.C>] [--notes "..."] [--dry-run]
  mcmodsync rebuild-sources -c pack.local.json [--dry-run]     # 保底：全量重建来源索引
  mcmodsync package-client -c pack.local.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

from . import config as mconfig
from . import console, logutil, signing

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VERIFY = 2

DEFAULT_KEY_FILE = "~/.mcmodsync/private.key"


# ---------------------------------------------------------------------------
# keygen
# ---------------------------------------------------------------------------

def cmd_keygen(key_file: str = DEFAULT_KEY_FILE, log: Callable[[str], None] = print) -> int:
    path = os.path.expanduser(key_file)
    try:
        pub_b64, path = signing.keygen(path)
    except FileExistsError as e:
        log("失败: %s" % e)
        log("如需轮换密钥: 先备份并删除旧私钥，再重跑 keygen（公钥变更后需重新分发客户端 config.json）")
        return EXIT_GENERIC
    log("私钥已写入: %s（POSIX 权限 600）" % path)
    log("公钥（Base64，填入 pack.local.json 的 client.publicKey 与分发包 config.json）:")
    log(pub_b64)
    return EXIT_OK


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _git(args: List[str], cwd: str) -> Tuple[int, str]:
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except OSError as e:
        return 127, str(e)


def _collect_secrets(cfg_path: str) -> List[str]:
    secrets: List[str] = []
    try:
        with open(cfg_path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
    except Exception:
        return secrets
    st = doc.get("storage") or {}
    for k in ("accessKey", "secretKey"):
        v = (st.get(k) or "").strip()
        if len(v) >= 8:
            secrets.append(v)
    pwd = ((doc.get("server") or {}).get("password") or "").strip()
    if len(pwd) >= 6:
        secrets.append(pwd)
    cf = ((doc.get("curseforge") or {}).get("apiKey") or "").strip()
    if len(cf) >= 8:
        secrets.append(cf)
    return secrets


def doctor(cfg_path: str, repo_dir: str = ".", offline: bool = False,
           accept_new_host: bool = False, log: Callable[[str], None] = print) -> int:
    results: List[Tuple[str, str, str]] = []   # (名称, PASS/FAIL/SKIP, 详情)

    def add(name: str, ok: Optional[bool], detail: str = "") -> None:
        results.append((name, "SKIP" if ok is None else ("PASS" if ok else "FAIL"), detail))

    # 1. 配置完整性
    cfg = None
    try:
        cfg = mconfig.load_config(cfg_path)
        add("配置完整性", True, "已加载 %s" % cfg_path)
    except Exception as e:
        add("配置完整性", False, str(e))

    # 2-3. 凭证入库检查（无论配置是否加载成功都可做）
    name = os.path.basename(cfg_path)
    rc_ignore, _o = _git(["check-ignore", "-q", name], repo_dir)
    rc_tracked, _o2 = _git(["ls-files", "--error-unmatch", name], repo_dir)
    rc_repo, out_repo = _git(["rev-parse", "--is-inside-work-tree"], repo_dir)
    in_repo = rc_repo == 0 and out_repo.strip() == "true"
    if rc_ignore == 127:
        add("git 可用性", None, "未检测到 git，跳过入库检查")
    elif not in_repo:
        # 非 git 目录（例如解压得到的副本）：check-ignore 会以 128 失败，
        # 既不是"已忽略"也不是"未忽略"，属环境不适用，不应判 FAIL。
        add("%s 入库检查" % name, None,
            "当前目录不是 git 仓库，跳过（AC-6 不适用）；"
            "若此处将来要提交到 git，先确认 .gitignore 已含 %s" % name)
    else:
        add("%s 被 .gitignore 覆盖" % name, rc_ignore == 0,
            "" if rc_ignore == 0 else "未忽略！请加入 .gitignore")
        add("%s 未被 git 追踪" % name, rc_tracked != 0,
            "" if rc_tracked != 0 else "已被追踪！需换钥并重写历史")

        secrets = _collect_secrets(cfg_path)
        if not secrets:
            add("git 历史凭证扫描", None, "配置中无可扫描凭证")
        else:
            hits = []
            for s in secrets:
                rc, out = _git(["log", "--all", "-p", "-S%s" % s, "--oneline"], repo_dir)
                if rc == 0 and out.strip():
                    hits.append(s[:2] + "****" + s[-2:])
            add("git 历史凭证扫描", not hits,
                "命中: %s（必须换钥或重写历史；删文件无效）" % ", ".join(hits) if hits
                else "未在 git 历史中发现明文凭证")

    # 4. 密钥对
    if cfg is not None:
        kf = os.path.expanduser(cfg.get("signing", {}).get("privateKeyFile") or DEFAULT_KEY_FILE)
        try:
            pem = signing.load_private_key(kf)
            derived = signing.public_key_b64_from_private(pem)
            expect = cfg["client"].get("publicKey") or ""
            ok = (not expect) or (expect == derived)
            add("密钥对可加载且与配置公钥一致", ok,
                "" if ok else "配置中的 publicKey 与私钥不匹配（需重新 keygen 并更新分发包）")
        except Exception as e:
            add("密钥对可加载", False, "%s（先运行 mcmodsync keygen）" % e)

    # 5. SSH + B 引导 + 协议版本 + 磁盘
    conn = None
    if cfg is not None and not offline:
        try:
            from . import pusher
            conn = pusher.connect_ssh(cfg, accept_new_host=accept_new_host)
            c = pusher._server_conf(cfg)
            pusher.ensure_remote_dirs(conn, c["server_dir"], c["history_dir"], lambda m: None)
            pusher.bootstrap_b(conn, c["server_dir"], c["remote_b"], lambda m: None)
            add("SSH 连接 + B 引导 + 协议版本", True, pusher.PROTOCOL_VERSION)
            rc, out, _err = conn.run("df -h %s | tail -1" % c["server_dir"])
            add("服务器磁盘余量", rc == 0, out.strip())
        except Exception as e:
            add("SSH 连接 + B 引导 + 协议版本", False, str(e))
    else:
        add("SSH 连接 + B 引导 + 协议版本", None, "offline 或配置不可用，跳过")

    # 6. 对象存储往返 + 缓存头 + 匿名读
    if cfg is not None and not offline:
        try:
            from .storage.s3 import S3Store
            import urllib.request

            st = cfg["storage"]
            doctor_prefix = st.get("prefix", "").rstrip("/") + "-doctor/"
            store = S3Store(st["endpointUrl"], st.get("region", ""), st["bucket"],
                            doctor_prefix,
                            st["accessKey"], st["secretKey"], bool(st.get("pathStyle", True)))
            store.put_bytes("probe.txt", b"doctor", "no-cache, no-store, must-revalidate")
            ok = store.head("probe.txt") and store.get_text("probe.txt") == "doctor" \
                and store.get_cache_control("probe.txt") == "no-cache, no-store, must-revalidate"
            add("对象存储 put/head/get + 缓存头", ok, "" if ok else "往返或缓存头不一致")
            base = (st.get("publicBaseUrl") or "").rstrip("/")
            if base:
                url = "%s/%sprobe.txt" % (base, doctor_prefix.lstrip("/"))
                try:
                    with urllib.request.urlopen(url, timeout=15) as r:
                        anon = r.read() == b"doctor"
                except Exception:
                    anon = False
                add("对象存储匿名读", anon, url)
            store.delete("probe.txt")
            add("对象存储 delete", not store.head("probe.txt"), "")
        except Exception as e:
            add("对象存储往返", False, str(e))
    else:
        add("对象存储往返 + 缓存头", None, "offline 或配置不可用，跳过")

    # 7. CurseForge apiKey
    if cfg is not None:
        cf = (cfg.get("curseforge", {}) or {}).get("apiKey") or ""
        if not cf:
            add("CurseForge apiKey", None, "未配置（source=curseforge 的 mod 将不可用）")
        else:
            add("CurseForge apiKey", True, "已配置（长度 %d，不校验在线有效性）" % len(cf))

    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass

    # 输出
    fails = 0
    skips = 0
    log(console.dim("=" * 62))
    log(console.bold("mcmodsync doctor"))
    log(console.dim("=" * 62))
    for name, status, detail in results:
        log(console.status_line(status, name, detail))
        if status == "FAIL":
            fails += 1
        elif status == "SKIP":
            skips += 1
    log(console.dim("-" * 62))
    if fails:
        log(console.paint("结论: %d 项 FAIL，需修复后才能发布" % fails, "FAIL"))
        return EXIT_GENERIC
    tip = "结论: 全绿" + ("（%d 项 SKIP 表示环境未提供或 offline）" % skips if skips else "")
    log(console.paint(tip, "PASS"))
    return EXIT_OK


# ---------------------------------------------------------------------------
# 其他子命令
# ---------------------------------------------------------------------------

def _not_implemented(task: str) -> int:
    print("该子命令的编排将在 %s 实现（当前为占位）。" % task)
    return EXIT_GENERIC


def cmd_fetch_mods(args) -> int:
    from . import fetcher

    cfg = mconfig.load_config(args.config)
    logger = logutil.setup_logger(None, name="mcmodsync")
    lock_path = args.lock or "mods.lock.json"
    try:
        return fetcher.fetch_mods(cfg, lock_path, upgrade=args.upgrade,
                                  lock_manual=args.lock_manual, dry_run=args.dry_run,
                                  log=logger.info)
    except fetcher.FetchError as e:
        logger.info("fetch-mods 失败（码 %s）: %s" % (e.exit_code, e))
        return e.exit_code


def cmd_push_server(args) -> int:
    from . import pusher

    cfg = mconfig.load_config(args.config)
    logger = logutil.setup_logger(None, name="mcmodsync")
    log = logger.info
    try:
        return pusher.push_server(cfg, None, dry_run=args.dry_run,
                                  check_server_client=args.check_server_client,
                                  accept_new_host=args.accept_new_host, log=log)
    except pusher.PushError as e:
        log("push-server 失败（码 %s）: %s" % (e.exit_code, e))
        return e.exit_code


def cmd_rollback_server(args) -> int:
    from . import pusher

    cfg = mconfig.load_config(args.config)
    logger = logutil.setup_logger(None, name="mcmodsync")
    try:
        return pusher.rollback_server(cfg, None, log=logger.info)
    except pusher.PushError as e:
        logger.info("rollback-server 失败（码 %s）: %s" % (e.exit_code, e))
        return e.exit_code


def cmd_publish_client(args) -> int:
    from . import publisher
    from .storage.s3 import S3Store

    cfg = mconfig.load_config(args.config)
    st = cfg["storage"]
    store = S3Store(st["endpointUrl"], st.get("region", ""), st["bucket"],
                    st.get("prefix", ""), st["accessKey"], st["secretKey"],
                    bool(st.get("pathStyle", True)))
    logger = logutil.setup_logger(None, name="mcmodsync")
    try:
        return publisher.publish_client(cfg, store, args.version, notes=args.notes or "",
                                        dry_run=args.dry_run, log=logger.info,
                                        gc=not getattr(args, "no_gc", False),
                                        resolve=not getattr(args, "no_resolve", False),
                                        backfill=getattr(args, "backfill", False),
                                        no_cf=getattr(args, "no_cf", False))
    except publisher.PublishError as e:
        logger.info("publish-client 失败（码 %s）: %s" % (e.exit_code, e))
        return e.exit_code


def cmd_rebuild_sources(args) -> int:
    """保底命令：对 client-mods 全部 mod 全量重查，整份重建 sources.json。

    与日常发布无关 —— 索引损坏 / 大面积失效 / 平台换域名时才用。
    """
    from . import publisher
    from .storage.s3 import S3Store

    cfg = mconfig.load_config(args.config)
    st = cfg["storage"]
    store = S3Store(st["endpointUrl"], st.get("region", ""), st["bucket"],
                    st.get("prefix", ""), st["accessKey"], st["secretKey"],
                    bool(st.get("pathStyle", True)))
    logger = logutil.setup_logger(None, name="mcmodsync")
    try:
        return publisher.rebuild_sources(cfg, store, log=logger.info,
                                         dry_run=args.dry_run,
                                         lock_path=args.lock,
                                         no_cf=args.no_cf)
    except publisher.PublishError as e:
        logger.info("rebuild-sources 失败（码 %s）: %s" % (e.exit_code, e))
        return e.exit_code


def cmd_package_client(args) -> int:
    """[T-50] 组装 C 端分发包（exe + .py 兜底 + config.json + SHA256SUMS）。"""
    from . import packaging

    out = args.out or packaging.repo_assets()["default_out"]
    try:
        res = packaging.assemble(args.config, out, exe=args.exe or None,
                                 no_exe=args.no_exe, fallback=args.fallback or None,
                                 skip_verify=args.skip_verify)
    except packaging.PackagingError as e:
        print("package-client 失败: %s" % e)
        return EXIT_GENERIC
    print("分发包已生成: %s" % res["out"])
    for rel, size in res["files"]:
        print("  %-28s %9d 字节" % (rel, size))
    return EXIT_GENERIC if res["banned"] else EXIT_OK


def _common_parent() -> argparse.ArgumentParser:
    """-c/--config 同时支持「子命令 -c ...」写法（计划书用法）。

    --no-color 用 SUPPRESS 作默认值：子命令没写它时不会把顶层解析到的 True
    覆盖回 False（否则 `mcmodsync --no-color doctor` 会失效）。
    """
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("-c", "--config", default=None,
                   help="配置文件（默认 ./pack.local.json）")
    c.add_argument("--no-color", dest="no_color", action="store_true",
                   default=argparse.SUPPRESS,
                   help="关闭彩色输出（写在子命令前或后都可以）")
    return c


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcmodsync", description="MC-ModSync A 端工具")
    p.add_argument("-c", "--config", default=None, help="配置文件（默认 ./pack.local.json）")
    p.add_argument("--no-color", action="store_true",
                   help="关闭彩色输出（重定向到日志文件时用）")
    sub = p.add_subparsers(dest="command", required=True)
    C = _common_parent()

    k = sub.add_parser("keygen", parents=[C], help="生成 Ed25519 密钥对")
    k.add_argument("--key-file", default=DEFAULT_KEY_FILE)

    d = sub.add_parser("doctor", parents=[C], help="环境与凭证体检")
    d.add_argument("--offline", action="store_true", help="跳过联网检查")
    d.add_argument("--repo-dir", default=".")
    d.add_argument("--accept-new-host", action="store_true", help="首次连接放行未知主机")

    f = sub.add_parser("fetch-mods", parents=[C], help="拉取 mod（Modrinth/CurseForge/manual）")
    f.add_argument("--upgrade", action="store_true")
    f.add_argument("--lock-manual", action="store_true")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--lock", default="mods.lock.json", help="mod 清单路径（默认 mods.lock.json）")

    ps = sub.add_parser("push-server", parents=[C], help="推送服务端变更")
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--check-server-client", action="store_true")
    ps.add_argument("--accept-new-host", action="store_true")

    sub.add_parser("rollback-server", parents=[C], help="回滚服务端")

    pc = sub.add_parser("publish-client", parents=[C], help="发布客户端清单与 blob")
    pc.add_argument("--version", default="",
                    help="版本号 A.B.C（A=1-9、B=0-9、C=0-6）；缺省时在上版基础上自动 +1"
                         "（C 满 6 进 B，B 满 9 进 A，如 1.1.6 -> 1.2.0）")
    pc.add_argument("--notes", default="")
    pc.add_argument("--dry-run", action="store_true")
    pc.add_argument("--no-gc", action="store_true",
                    help="发布后不清理对象存储（默认会删除不再被引用的旧 blob 与旧清单）")
    pc.add_argument("--backfill", action="store_true",
                    help="来源索引：本次对全部文件强制重新反查（保底修复时才需要）")
    pc.add_argument("--no-resolve", action="store_true",
                    help="来源索引：本次完全跳过（不读不写，线上旧索引保持不变）")
    pc.add_argument("--no-cf", action="store_true", help="来源索引：跳过 CurseForge 反查")

    rb = sub.add_parser("rebuild-sources", parents=[C],
                        help="保底：全量重查 client-mods 并覆盖重建 sources.json"
                             "（索引损坏/失效时用，不进日常流程）")
    rb.add_argument("--dry-run", action="store_true")
    rb.add_argument("--lock", default="mods.lock.json",
                    help="mod 清单路径（默认 mods.lock.json）")
    rb.add_argument("--no-cf", action="store_true", help="跳过 CurseForge 反查")

    pk = sub.add_parser("package-client", parents=[C],
                        help="打包客户端分发包（exe + .py 兜底 + config.json + SHA256SUMS）")
    pk.add_argument("--out", default="", help="输出目录（默认 <仓库>/dist/client-package）")
    pk.add_argument("--no-exe", action="store_true", help="跳过 exe 构建（仅 .py 兜底）")
    pk.add_argument("--exe", default="", help="复用已构建的 exe 路径")
    pk.add_argument("--fallback", default="", help=".py 兜底来源（默认 client/mcmodsync.py）")
    pk.add_argument("--skip-verify", action="store_true", help="跳过产物内模块核验")
    return p


# 需要在开始处打印运行分隔头的子命令（doctor 自带表头，故不在内）
_RUN_HEADER_CMDS = ("fetch-mods", "push-server", "rollback-server",
                    "publish-client", "rebuild-sources", "package-client")


def print_run_header(command: str, log: Callable[[str], None] = print) -> None:
    """每次运行打印一段带命令名与时间的分隔头。

    连续跑多条命令时，一眼就能分清哪段输出属于哪次运行。
    纯 ASCII，避免在 cp1252 等代码页下编码失败。
    """
    log(console.dim("=" * 62))
    log(console.bold("mcmodsync %s   |   %s"
                     % (command, time.strftime("%Y-%m-%d %H:%M:%S"))))
    log(console.dim("=" * 62))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args_cfg = getattr(args, "config", None) or "./pack.local.json"
    # 各 cmd_* 直接读 args.config；未显式传 -c 时统一兜底成 ./pack.local.json，
    # 否则它们会拿到 None 并在 load_config 处抛 TypeError（教程里的命令都不带 -c）。
    if hasattr(args, "config"):
        args.config = args_cfg
    if getattr(args, "no_color", False):
        console.disable_color()
    if args.command in _RUN_HEADER_CMDS:
        print_run_header(args.command)

    try:
        if args.command == "keygen":
            return cmd_keygen(args.key_file)
        if args.command == "doctor":
            return doctor(args_cfg, repo_dir=args.repo_dir, offline=args.offline,
                          accept_new_host=args.accept_new_host)
        if args.command == "push-server":
            return cmd_push_server(args)
        if args.command == "rollback-server":
            return cmd_rollback_server(args)
        if args.command == "publish-client":
            return cmd_publish_client(args)
        if args.command == "rebuild-sources":
            return cmd_rebuild_sources(args)
        if args.command == "fetch-mods":
            return cmd_fetch_mods(args)
        if args.command == "package-client":
            return cmd_package_client(args)
    except mconfig.ConfigError as e:
        print("配置错误: %s" % e)
        return EXIT_GENERIC
    parser.print_help()
    return EXIT_GENERIC


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
