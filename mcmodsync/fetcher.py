"""fetch-mods 编排（A 端）—— [T-33] / [T-34]。

职责: 解析 mods.lock.json -> 按 source/优先级路由 provider -> 下载 -> 按 side 落盘
      -> 回写锁字段 -> 输出三段式中文摘要。

路由: source 指定则直达；未指定按优先级 Modrinth -> CurseForge -> manual 自动探测。
落盘: side=server -> server-mods/；client -> client-mods/；both -> 两目录各一份（下载一次，复制一次）。
幂等: 目标文件已存在且 sha256 一致 -> 跳过（"无需更新"）。
锁版本: 下载后回写 resolvedVersion / sha256 / size / downloadUrl；仅 --upgrade 重新解析 latest。
退出码: 全部成功 0；存在 manual-needed 仍 0（摘要显著提示）；存在下载失败 5。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from . import hashing, manifest, provider_curseforge as cf
from . import provider_modrinth as mr
from . import console

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_NETWORK = 5

REPO_MODRINTH_BASE = mr.BASE_URL
REPO_CURSEFORGE_BASE = cf.BASE_URL


class FetchError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# lock 文件
# ---------------------------------------------------------------------------

def load_lock(path: str) -> dict:
    if not os.path.isfile(path):
        raise FetchError(EXIT_GENERIC, "mods.lock.json 不存在: %s" % path)
    with open(path, "rb") as f:
        doc = __import__("json").loads(f.read().decode("utf-8"))
    if not isinstance(doc, dict) or doc.get("schemaVersion") != 1:
        raise FetchError(EXIT_GENERIC, "mods.lock.json 结构非法（schemaVersion 必须为 1）")
    doc.setdefault("packMeta", {})
    doc.setdefault("mods", [])
    return doc


def save_lock(path: str, doc: dict) -> None:
    manifest.write_json(path, doc)


# ---------------------------------------------------------------------------
# 路由与落盘
# ---------------------------------------------------------------------------

def lock_stale_hint(cfg, lock_path: str = "mods.lock.json") -> Optional[str]:
    """[T-35] 联动提示: mods.lock.json 存在且源目录最近 jar 比它新 -> 返回提示语。

    仅提示（warning），不阻断 push-server / publish-client。
    """
    if not lock_path or not os.path.isfile(lock_path):
        return None
    lock_mtime = os.path.getmtime(lock_path)
    latest = 0.0
    for side in ("server", "client"):
        try:
            dirs = side_dirs(cfg, side)
        except FetchError:
            continue
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                if n.lower().endswith(".jar"):
                    latest = max(latest, os.path.getmtime(os.path.join(d, n)))
    if latest > lock_mtime:
        return ("mod 列表未刷新（源目录中的 jar 比 %s 更新），建议先执行 mcmodsync fetch-mods"
                % os.path.basename(lock_path))
    return None


def side_dirs(cfg, side: str) -> List[str]:
    """side -> 目标目录列表（both 返回两个）。"""
    server_dir = cfg["server"]["sourceModsDir"]
    client_dir = cfg["client"]["sourceModsDir"]
    if side == "server":
        return [server_dir]
    if side == "client":
        return [client_dir]
    if side == "both":
        return [server_dir, client_dir]
    raise FetchError(EXIT_GENERIC, "非法 side: %r（应为 server/client/both）" % side)


def _resolve_one(entry: dict, pack_meta: dict, cfg, upgrade: bool,
                 log: Callable[[str], None]) -> Optional[dict]:
    """按 source/优先级解析下载信息；返回带 manualNeeded 的 info，或 None（彻底找不到）。"""
    mc = pack_meta.get("minecraftVersion") or ""
    loader = pack_meta.get("loader") or "neoforge"
    slug = entry.get("projectSlug") or ""
    pin = entry.get("versionPin") or "latest"
    source = entry.get("source") or ""
    cf_key = (cfg.get("curseforge", {}) or {}).get("apiKey") or ""

    def _mr():
        return mr.resolve(slug, mc, loader, pin, REPO_MODRINTH_BASE, log)

    def _curse():
        return cf.resolve(slug, mc, cf_key, pin, REPO_CURSEFORGE_BASE, log)

    if not upgrade and entry.get("sha256") and entry.get("downloadUrl") \
            and entry.get("resolvedVersion") and not entry.get("manualNeeded"):
        return {"resolvedVersion": entry["resolvedVersion"], "fileName": entry["fileName"],
                "downloadUrl": entry["downloadUrl"], "size": entry.get("size") or 0,
                "source": entry.get("source") or "cached", "manualNeeded": False,
                "projectUrl": entry.get("downloadUrl") if entry.get("source") == "manual" else ""}

    try:
        if source == "modrinth":
            return _mr()
        if source == "curseforge":
            return _curse()
        if source == "manual":
            return None
        # 未指定: 按优先级自动探测
        info = _mr()
        if info is not None:
            return info
        log("Modrinth 无 %s，回退 CurseForge" % slug)
        return _curse()
    except cf.KeyInvalidError as e:
        log("CurseForge 源跳过（%s）" % e)
        return None
    except (mr.ProviderError, cf.ProviderError) as e:
        raise FetchError(e.exit_code, str(e))


def _manual_entry(entry: dict, project_url: str, note: str,
                  info: Optional[dict] = None) -> dict:
    return {"name": entry.get("name") or entry.get("projectSlug"),
            "fileName": entry.get("fileName") or (info or {}).get("fileName"),
            "projectUrl": project_url,
            "note": note}


def fetch_mods(cfg, lock_path: str, upgrade: bool = False, lock_manual: bool = False,
               dry_run: bool = False, log: Callable[[str], None] = print,
               tmp_dir: Optional[str] = None) -> int:
    doc = load_lock(lock_path)
    pack_meta = doc.get("packMeta") or {}
    mods: List[dict] = doc["mods"]

    updated: List[str] = []
    skipped: List[str] = []
    manual: List[dict] = []
    failed: List[Tuple[str, str]] = []

    if not mods:
        # 空锁文件是最常见的"为什么什么都没下载"困惑来源，显式提示而不是静默 0。
        log(console.paint(
            "[提示] mods.lock.json 的 mods 是空列表，没有任何条目可处理。", "WARN"))
        log("  要自动下载: 在 mods 数组里登记 {name, projectSlug, source, side, versionPin}；")
        log("  已有本地 jar: 直接放进 server-mods/ / client-mods/ 即可，推送和发布不依赖锁文件。")

    if lock_manual:
        return _lock_manual(doc, lock_path, cfg, updated, skipped, manual, log)

    tmp_root = tmp_dir or os.path.join(tempfile.gettempdir(), "mcmodsync-fetch")
    os.makedirs(tmp_root, exist_ok=True)

    for entry in mods:
        name = entry.get("name") or entry.get("projectSlug") or "?"
        pin = entry.get("versionPin") or "latest"

        if pin == "manual" and not entry.get("sha256"):
            manual.append(_manual_entry(entry, entry.get("downloadUrl") or "", entry.get("note") or ""))
            continue

        try:
            info = _resolve_one(entry, pack_meta, cfg, upgrade, log)
        except FetchError as e:
            log("失败: %s（%s）" % (name, e))
            failed.append((name, str(e)))
            continue

        if info is None or info.get("manualNeeded"):
            url = (info or {}).get("projectUrl") or entry.get("downloadUrl") or ""
            if not url:
                url = "%s %s" % (mr.project_url(entry.get("projectSlug", "")),
                                 cf.project_url(entry.get("projectSlug", "")))
            note = "CurseForge 禁第三方下载" if (info and info.get("manualNeeded")) else "平台未找到可下载版本"
            entry["source"] = "manual"
            entry["downloadUrl"] = url
            entry["note"] = note
            # 人工闭环的匹配键：把平台解析到的文件名回写进 lock，
            # 否则 OP 不知道该放哪个文件名、--lock-manual 也无从匹配。
            if not entry.get("fileName") and (info or {}).get("fileName"):
                entry["fileName"] = info["fileName"]
            manual.append(_manual_entry(entry, url, note, info))
            continue

        dirs = side_dirs(cfg, entry.get("side") or "server")
        fname = os.path.basename(str(info.get("fileName") or entry.get("fileName") or ""))
        if not fname:
            failed.append((name, "解析结果缺少 fileName"))
            continue

        # 幂等: 所有目标目录均已存在且哈希一致
        want = entry.get("sha256")
        if want and all(os.path.isfile(os.path.join(d, fname)) and
                        _sha_of(os.path.join(d, fname)) == want for d in dirs):
            skipped.append(name)
            continue

        if dry_run:
            log("[dry-run] 将下载 %s (%s) -> %s" % (fname, info.get("resolvedVersion"),
                                                   ", ".join(dirs)))
            updated.append(name)
            continue

        # 下载一次
        dl_dir = os.path.join(tmp_root, "dl")
        got = _download(info, dl_dir, log)
        if got is None:
            failed.append((name, "下载失败"))
            continue
        src_path = os.path.join(dl_dir, got["fileName"])

        # 落盘（both: 复制一份）
        for d in dirs:
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, got["fileName"])
            if os.path.abspath(src_path) != os.path.abspath(dst):
                manifest.atomic_copy(src_path, dst)
        updated.append(name)

        entry["fileName"] = got["fileName"]
        entry["resolvedVersion"] = info.get("resolvedVersion") or entry.get("resolvedVersion")
        entry["sha256"] = got["sha256"]
        entry["size"] = got["size"]
        entry["downloadUrl"] = got["downloadUrl"]
        entry["source"] = info.get("source") or entry.get("source") or "modrinth"
        entry.pop("manualNeeded", None)

    if not dry_run:
        save_lock(lock_path, doc)
        log("已回写 %s" % lock_path)

    _summary(log, updated, skipped, manual, failed)
    return EXIT_NETWORK if failed else EXIT_OK


def _sha_of(path: str) -> str:
    return str(hashing.hash_file(path)["sha256"])


def _download(info: dict, dl_dir: str, log) -> Optional[dict]:
    source = info.get("source") or "modrinth"
    try:
        if source == "curseforge":
            return cf.download_file(info, dl_dir, log)
        if source == "modrinth":
            return mr.download_file(info, dl_dir, log)
        # 已缓存的直链（downloadUrl 指向静态文件）: 用 modrinth 的下载器
        return mr.download_file(info, dl_dir, log)
    except (mr.ProviderError, cf.ProviderError) as e:
        log("下载失败: %s" % e)
        return None


def _summary(log, updated: List[str], skipped: List[str], manual: List[dict],
             failed: List[Tuple[str, str]]) -> None:
    log("=" * 62)
    log("fetch-mods 摘要")
    log("=" * 62)
    log("【已更新 %d】%s" % (len(updated), "、".join(updated) if updated else "无"))
    log("【无需更新 %d】%s" % (len(skipped), "、".join(skipped) if skipped else "无"))
    log("【需人工下载 %d】" % len(manual))
    for m in manual:
        log("  - %s -> %s（%s）%s" % (m["name"], m["fileName"] or "?", m["note"], m["projectUrl"]))
    if manual:
        log("  请人工下载后放入对应源目录，再执行: mcmodsync fetch-mods --lock-manual")
    if failed:
        log("【失败 %d】%s" % (len(failed), "、".join("%s(%s)" % (n, r) for n, r in failed)))
    log("-" * 62)


# ---------------------------------------------------------------------------
# T-34: manual-needed 闭环
# ---------------------------------------------------------------------------

# 从文件名解析版本号: 形如 1.2 / 1.2.3 / 1.2.3-beta / 1.2.3+build；
# 后缀必须以 - 或 + 起头，避免把 ".jar" 吃掉。
_VER_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z._-]*)?)")


def _norm_token(s: str) -> str:
    """规范化用于匹配的 token：仅保留字母数字并转小写（如 'not-enough-animations'
    -> 'notenoughanimations'，可与 notenoughanimations-neoforge-1.12.4... 匹配）。"""
    return re.sub(r"[^0-9a-z]+", "", (s or "").lower())


def _lock_manual(doc: dict, lock_path: str, cfg, updated: List[str], skipped: List[str],
                 manual: List[dict], log) -> int:
    """扫描源目录，匹配 manual 条目并回写 sha256/size/resolvedVersion。"""
    candidates: Dict[str, List[str]] = {}
    for side in ("server", "client"):
        try:
            for d in side_dirs(cfg, side):
                if os.path.isdir(d):
                    for n in os.listdir(d):
                        if n.lower().endswith(".jar"):
                            candidates.setdefault(n, []).append(os.path.join(d, n))
        except FetchError:
            continue

    for entry in doc["mods"]:
        if entry.get("source") != "manual" and entry.get("versionPin") != "manual":
            continue
        if entry.get("sha256"):
            skipped.append(entry.get("name") or "?")
            continue
        want = entry.get("fileName")
        matches = candidates.get(want or "", [])
        if not matches and want:
            matches = [p for n, ps in candidates.items() if want in n for p in ps]
        if not matches:
            # 兜底: lock 里没有 fileName 时，用 projectSlug 的规范化子串做确定性筛选
            # （仍遵循「禁止自动猜测」: 命中多于一个且内容不一致 -> 交人工，不自行选择）
            slug = _norm_token(entry.get("projectSlug") or "")
            if slug:
                matches = [p for n, ps in candidates.items() if slug in _norm_token(n) for p in ps]
        if len(matches) > 1:
            # side=both 会在两个源目录各放一份；内容一致则视为唯一
            if len({_sha_of(p) for p in matches}) != 1:
                log("手工锁定无法唯一匹配 %s，候选: %s" % (want, matches))
                manual.append(_manual_entry(entry, entry.get("downloadUrl") or "",
                                            "候选文件多于一个，需人工确认"))
                continue
        if not matches:
            manual.append(_manual_entry(entry, entry.get("downloadUrl") or "",
                                        "源目录未找到人工放入的文件"))
            continue

        path = matches[0]
        entry["fileName"] = os.path.basename(path)
        info = hashing.hash_file(path)
        entry["sha256"] = str(info["sha256"])
        entry["size"] = int(info["size"])
        m = _VER_RE.search(entry["fileName"])
        ver = m.group(1) if m else None
        if ver and ver.lower().endswith(".jar"):     # 避免把扩展名当版本号的一部分
            ver = ver[:-4]
        entry["resolvedVersion"] = ver
        if m is None:
            entry["note"] = ((entry.get("note") or "") + " 无法从文件名解析版本号，resolvedVersion 留空")
        updated.append(entry.get("name") or entry["fileName"])

    save_lock(lock_path, doc)
    log("已回写 %s（手工锁定）" % lock_path)
    _summary(log, updated, skipped, manual, [])
    return EXIT_OK
