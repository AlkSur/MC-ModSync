# C 端多源下载改造方案 v2（含「下载源索引」同步机制）

> 状态：**全部完成**（v2.1.0，2026-09-22 落地）：A 端、C 端代码 + 测试 + OP 端教程，
> 以及**重打 C 端 exe 与 `dist/release/` 三端包**均已完成（见 §10 阶段 3）。
> 附带修复了 C 端游戏运行检测对普通 java 进程的误判（见 §10 末）。
> v2 修订日期：2026-09-22（v1 为同日早间稿）
> 涉及组件：A 端（Python CLI）、C 端（玩家更新器）；B 端不涉及。

---

## 0. 本次修订结论（对应上轮 4 个问题）

| # | 上轮问题 | 你的结论 | 落地位置 | 状态 |
|---|---|---|---|---|
| 1 | `preferPlatform` 是什么意思 | （已解释，见 §1.2）按**默认启用**处理 | §6.4 | 已实施 |
| 2 | 直链失败是否在终端提示 | **静默** —— 终端一行都不打印 | §6.3 | 已实施 |
| 3 | C 端教程是否说明走官方 CDN | **不说明** | §10 | 照办 |
| 4 | 阶段 1 是否先行实施 | 暂不实施 → 后改为"直接实施" | §10 | 全部落地 |
| 5 | 追加要求 | **单独一条保底命令**：索引损坏时全量重查整个 client-mods | §5.5 | 已实施（`rebuild-sources`） |

### 0.1 本次修订的结构性变化（最重要）

| 项 | v1 设计 | v2 设计（本稿） |
|---|---|---|
| 下载源信息放哪 | 写进**版本清单**每个文件条目 | **独立文件 `sources.json`**，与版本清单解耦 |
| 谁维护它 | 跟着版本清单走 | **A 端每次 `publish-client` 一起上传，覆盖旧文件** |
| C 端怎么拿 | 读版本清单里的字段 | **每次启动先拉最新 `sources.json` 覆盖本地老文件** |
| 失效条目 | 靠清单变更自然消失 | **重建时以当前清单为准裁剪**（删除的 mod 从 json 中移除） |

你提出的机制优于 v1，v1 的"避免双份数据"论点在此让位，理由见 §1.1。

---

## 1. 名词与文件分工

### 1.1 两个文件各管一件事

| 文件 | 位置（对象存储） | 管什么 | 谁写 | 谁读 | 签名 |
|---|---|---|---|---|---|
| `manifest.json` | pack 根 | **指针**：当前版本号 + 版本清单地址 | A 端 `publish-client` | C 端 | 是 |
| `manifests/<ver>.json` | pack/manifests/ | **要哪些文件**：path / sha256 / size / delete 列表 | A 端 `publish-client` | C 端 | 是 |
| `sources.json` | pack 根 | **从哪下**：sha256 → 平台直链 | A 端 `publish-client`（每次覆盖） | C 端（每次启动拉取） | 是（见 §4.6） |

一句话：**清单说"要什么"，索引说"去哪拿"。**

**为什么独立文件更好：**

1. **不换版本也能修下载源**。某平台直链挂了 / 换了 CDN 域名 → A 端重跑一次 `publish-client` 覆盖 `sources.json` 即可，**不必发新版本**（否则玩家会被迫下载一个内容完全相同的新版本）。
2. **老 C 端无感**。旧版 exe 根本不请求 `sources.json`，继续走对象存储，行为一字不变。
3. **版本清单保持精简**，职责单一。
4. **可独立缓存、独立淘汰**，不污染清单的签名语义。

### 1.2 `preferPlatform` 是什么（回答第 1 问）

它是一个**布尔开关**，只决定一件事：

| 取值 | 行为 | 玩家感受 |
|---|---|---|
| `true` | 下载前先查 `sources.json`，命中就用 **Modrinth / CurseForge 官方 CDN 直链**；直链一旦失败立刻回落对象存储 | 更快、更省你的出网流量；极端情况下比现在慢几秒 |
| `false` | 完全不看 `sources.json`，**全部走对象存储**（= 现在的行为） | 与今天完全一致 |

写进 C 端配置 `config.json` 的 `client.preferPlatform` 字段，玩家改一行即可关闭。
**本稿按"默认 `true`（启用）"实施** —— 因为你已经明确要 `sources.json` 这套机制；若想先观望一版，改成 `false` 即可，代码不变。

---

## 2. 现状与证据（只读核实，v1 结论不变）

| 事实 | 证据 |
|---|---|
| C 端当前**只有对象存储一个下载源** | `http_download.py:282` URL 恒为 `blob_base + blobs/<sha[0:2]>/<sha[2:4]>/<sha>` |
| pack 根前缀可直接算出 | `client.py:210 blob_base_of(pointer_url)` 已实现，`sources.json` 与其同级 |
| C 端零平台下载代码 | 全仓检索 `api.modrinth.com` / `api.curseforge.com` / `edge.forgecdn.net`，仅命中 A 端 `provider_modrinth.py` / `provider_curseforge.py` |
| `\|来源:X\|` 只是标签，不影响下载地址 | `client.py:688` 注释原文 |
| 发布清单**不带来源** | `publisher.py:80 scan_client_mods()` 只产出 `{path, sha256, size}` |
| `mods.lock.json` 已记来源与直链 | modrinth 153 / curseforge 14 / manual 27；190/194 条有 `downloadUrl` |
| 但 `publish-client` **不读** lock 的有效字段 | 仅用 `fetcher.lock_stale_hint()` 出一句提示 |
| **反查底盘已存在** | `tools/gen_lock.py` 已实现并跑通：`file_hashes()` / `cf_murmur2()` / `mr_lookup_batch()` / `mr_projects()` / `cf_lookup()` / `cf_mod_slug()` |
| CF apiKey 已配置 | `pack.local.json` → `curseforge.apiKey`（非空） |
| C 端本地已有可写目录 | `<target>/_updater/`（`state.json` / `staging/` / `backup/` 均在此），`sources.json` 缓存放这里 |

### 2.1 已发现的数据缺陷（必须在方案中处理）

lock 文件中 `source: manual` 条目的 `downloadUrl` 是**脏数据**，实测值：

```
"downloadUrl": "https://modrinth.com/mod/ https://www.curseforge.com/minecraft/mc-mods/"
```

这是两个**项目主页** URL（且粘在一起），非文件下载地址。若原样写入 `sources.json`，
C 端会去下一个 HTML 页面 → sha256 不符 → 白白浪费一次请求。
→ **必须做直链白名单校验**（§5.3）。

---

## 3. 设计总览

### 3.1 数据流（改造后）

```
[发布阶段 · A 端 publish-client]
  client-mods/ ──scan_client_mods()──► [{path, sha256, size}]     ← 不变，纯哈希扫描
                                          │
                    ┌─────────────────────┴─────────────────────┐
              变更条目 (added + replaced)                 未变更条目
                    │                                            │
                    ▼                                            ▼
          resolve_sources() 三级反查                  从上一版 sources.json
          1) mods.lock.json 按 sha256 命中（0 次 API）  按 sha256 继承
          2) Modrinth 批量 sha1 反查
          3) CurseForge murmur2 指纹反查
                    │
                    ▼
          URL 白名单过滤（§5.3）
                    │
                    ▼
      合并 → 按当前清单 files 裁剪（删掉不再存在的 sha256）
                    │
                    ▼
            签名 → 上传 sources.json（覆盖旧文件）

  上传顺序（硬约束）:
    blobs/**  →  manifests/<ver>.json  →  sources.json  →  manifest.json（指针，最后）

[下载阶段 · C 端]
  启动 → GET <pack根>/sources.json（超时 3s）
          │
          ├─ 成功且验签通过 ──► 原子覆盖 <target>/_updater/sources.json
          ├─ 失败 / 超时 / 验签失败 ──► 静默沿用本地缓存（无缓存则视为空表）
          │            ※ 无论哪种结果，都**不影响本次同步的成败判定**
          ▼
  读版本清单 files[i]
          │
          ├─ sources[sha256] 命中且 URL 过白名单 ──► 试平台直链（1 次，8s，不重试）
          │                                              │
          │                                    成功且 sha256 通过 ──► 原子落位
          │                                              │
          │                                    失败/超时/哈希不符 ──► 静默回落（终端无输出）
          │
          └─ 未命中 ──► 直接走对象存储（现有完整重试逻辑，一行不改）

  两条路径最终都过**同一个** sha256 校验 + 原子落位   ◄── 这段代码不改
```

### 3.2 与 v1 的关键差异（差异清单）

| 项 | v1 | v2 |
|---|---|---|
| 版本清单字段 | 每条 +`source`/`downloadUrl` | **不动**（保持 `{path, sha256, size}`） |
| 新增文件 | 无 | **`sources.json`（对象存储）+ `_updater/sources.json`（本地缓存）** |
| 拉取时机 | 随清单天然拿到 | C 端**每次同步启动**单独拉 1 次（§6.2） |
| 直链失败提示 | 终端补一行 | **终端静默**（日志留 1 行） |
| 来源标签数据源 | 清单字段 | `sources.json` 查表 |
| 阶段 1 | 建议先实施 | **暂不实施** |

---

## 4. 下载源索引 `sources.json` 规范

### 4.1 路径与 URL

| 项 | 值 |
|---|---|
| 对象存储 key | `sources.json`（与 `manifest.json` 同级，pack 根） |
| 公网 URL | `<manifestUrl 的目录>/sources.json`，即 `blob_base_of(pointer_url) + "/sources.json"` |
| 当前实例 | `https://server-mods-u0demo00.cdn.7caiyun.com/packs/0123456789abcdef/sources.json` |
| 缓存控制 | `public, max-age=300`（与版本清单一致；**不可**用 blob 的 immutable） |
| 权限 | 只有 A 端持 AK/SK，公开读不可写 |

放 pack 根而不是 `manifests/` 下，是为了**与版本号彻底解耦**：换版本不改名，覆盖即可。

### 4.2 结构（示例）

```json
{
  "schemaVersion": 1,
  "packId": "demo-pack",
  "generatedForVersion": "1.0.6",
  "updatedAt": "2026-09-22T15:31:00+08:00",
  "sources": {
    "3f7a1c9e0b6d4e2a8c5f1b7d9e0a3c6f2b8d4e1a7c9f0b3d6e8a1c4f7b0d9e2a": {
      "source": "modrinth",
      "fileName": "Lift-n-Load-1.2.3.jar",
      "size": 1234567,
      "downloadUrl": "https://cdn.modrinth.com/data/abcdefgh/versions/xyz123/Lift-n-Load-1.2.3.jar",
      "projectSlug": "lift-n-load",
      "resolvedVersion": "1.2.3"
    },
    "9c2e5b8d1a4f7c0e3b6d9a2f5c8e1b4d7a0f3c6e9b2d5a8f1c4e7b0d3a6f9c2e": {
      "source": "curseforge",
      "fileName": "Some-Mod-4.5.0.jar",
      "size": 2345678,
      "downloadUrl": "https://edge.forgecdn.net/files/1234/567/Some-Mod-4.5.0.jar",
      "projectSlug": "some-mod",
      "resolvedVersion": "4.5.0"
    }
  }
}
```

字段约定：

| 字段 | 必需 | 说明 |
|---|---|---|
| `schemaVersion` | 是 | 固定 `1`，与版本清单同一套语义 |
| `packId` | 是 | C 端校验，不符则**静默忽略整份文件** |
| `generatedForVersion` | 否 | 仅作追溯（"这份索引是为 1.0.6 生成的"），C 端**不**用它做判断 |
| `updatedAt` | 否 | 排障用时间戳 |
| `sources` | 是 | **键 = sha256**（与清单里的 `sha256` 同值，直接命中，无需再算 sha1 / murmur2） |
| `sources[].source` | 是 | `modrinth` / `curseforge` |
| `sources[].downloadUrl` | 是 | 必须过白名单（§5.3） |
| `sources[].fileName` / `size` | 否 | 排障与日志用 |
| `sources[].projectSlug` / `resolvedVersion` | 否 | 排障用 |

**C 端只依赖 `sha256` → `downloadUrl` 一条映射**，其余字段缺失不影响运行。

### 4.3 生成规则（A 端）

1. 基准 = 上一版 `sources.json`（优先读对象存储；读取失败则用本地 `client-publish-sources.json`；都没有则空表）。
2. 只对本次 `added + replaced` 的文件联网反查（§5.1）；未变更文件按 **sha256** 从基准继承。
3. **裁剪**：只保留 sha256 出现在**本次版本清单 `files`** 中的条目 —— 删除的 mod、被替换掉的旧版本，其条目自动消失（符合你的要求：变更删除的 mod 从 json 中移除）。
4. 白名单过滤（§5.3），不通过的条目**不写入**（宁可走存储，不写脏地址）。
5. 签名后上传，覆盖同名 key。

### 4.4 同步规则（C 端）

| 步骤 | 行为 | 失败时 |
|---|---|---|
| 触发时机 | 仅在**执行同步**时（`mcmodsync sync` / `--check` 类只读命令不拉） | — |
| 请求 | GET `<pack根>/sources.json`，超时 **3 秒**，带 `If-None-Match`（服务端返回 304 则沿用缓存） | 静默 |
| 校验 | `canonicaljson.verify(obj, publicKey)` + `packId` 相等 + `schemaVersion <= 1` | 静默 |
| 落盘 | 通过后**原子写** `<target>/_updater/sources.json`（覆盖老文件，与 `state.json` 同目录） | 静默 |
| 影响范围 | **不参与**同步成败判定；拿不到就视为空表 → 全部走对象存储 | — |

### 4.5 体积与开销

| 项 | 估算 |
|---|---|
| 单条目 | 约 180~220 B |
| 200 条文件 | 约 40 KB（CDN gzip 后 < 10 KB） |
| 每次启动新增请求 | 1 次 GET（可 304 命中缓存） |
| 相对代价 | 同步本身要下 MB 级 mod，这 1 次请求可忽略 |

### 4.6 是否签名（推荐：签）

| 方案 | 好处 | 代价 |
|---|---|---|
| **签（推荐）** | 与清单同一把 Ed25519 私钥，`canonicaljson.sign/verify` 现成；防篡改、防伪造；验签失败**静默忽略**，无副作用 | 多约 90 B，多一次本地验签 |
| 不签 | 少几行代码 | 任何人改不了对象存储（无 AK/SK），但中间链路（代理 / 加速器）理论上可替换内容 |

即便不签名，安全底线仍在：**sha256 来自已签名的版本清单**，篡改的直链拿不到匹配哈希的文件 → 校验失败 → 静默回落。
**推荐签名**（成本几乎为零，防御纵深更好）。若不签，请在 §12 说明。

---

## 5. A 端改造明细

### 5.1 新增模块 `mcmodsync/source_resolve.py`

从 `tools/gen_lock.py` 抽取反查原语（**复用已验证代码，不新写算法**）：

| 函数 | 来源 | 说明 |
|---|---|---|
| `file_hashes(path)` | `gen_lock.py:46` | sha1 / sha256 / sha512 / size |
| `cf_murmur2(path)` | `gen_lock.py:61` | CurseForge 指纹：去空白字节后 `murmur2(seed=1)` |
| `mr_lookup_batch(sha1_list)` | `gen_lock.py:136` | `POST https://api.modrinth.com/v2/version_files`，`{"hashes":[sha1],"algorithm":"sha1"}`，800/批 |
| `mr_projects(ids)` | `gen_lock.py:166` | 批量取 slug |
| `cf_lookup(fps, api_key)` | `gen_lock.py:183` | `POST https://api.curseforge.com/v1/fingerprints` |
| `cf_mod_slug(mod_id, api_key)` | `gen_lock.py:264` | modId → slug |

对外暴露单一入口：

```
resolve_sources(local_dir, entries, lock_index, cfg, log, allow_network=True) -> dict
    返回 { sha256: {"source": "...", "downloadUrl": "...", ...} }
```

三级反查（前一级命中即短路，避免无谓联网）：

1. **本地 lock 命中**：按 `sha256` 查 `mods.lock.json` 索引 → 0 次 API（预期覆盖绝大多数）
2. **Modrinth**：本地算 sha1 → 批量反查 → version → 取 `files[]` 中 `primary=true` 者
3. **CurseForge**：本地算 murmur2 指纹 → 批量反查
4. 全未命中 → **不写入** `sources.json`（该文件走对象存储）

### 5.2 改动点

| 文件 | 函数 | 改动 |
|---|---|---|
| `mcmodsync/publisher.py` | 新增 `build_sources_index()` | 合并（基准 + 本次反查）→ 按清单 `files` 的 sha256 裁剪 → 签名 |
| `mcmodsync/publisher.py` | `publish_client()` | 新增步骤 5.5：上传 `sources.json`（覆盖）；dry-run 加统计行 |
| `mcmodsync/publisher.py` | `build_version_manifest()` | **不改**（v2 不再往清单写来源字段） |
| `mcmodsync/publisher.py` | `scan_client_mods()` | **不改**（保持纯哈希扫描） |
| `mcmodsync/publisher.py` | `gc_unreferenced()` | **不改**：它只枚举 `blobs/` 前缀，`sources.json` 天然不受影响（实施时补一条断言测试） |
| `mcmodsync/cli.py` | `publish-client` 参数 | 新增 `--backfill`（本次强制全量反查）、`--no-resolve`（完全跳过，不更新 `sources.json`） |

**上传顺序（硬约束，插进现有 步骤7 之前）**：

```
blobs/**  →  manifests/<ver>.json  →  sources.json  →  manifest.json（指针，最后写入）
```

**`sources.json` 上传失败如何处置？** 推荐**不阻断发布**：打一行警告，继续写指针。
理由：它只是加速层，缺了不影响玩家同步（最多退化为全走存储），不该让一次发布整体失败。
（若你要求"上传失败即中止发布"，请在 §12 说明。）

**dry-run 输出新增一行**（示例）：

```
来源索引: 命中 190（lock 186 / Modrinth 3 / CurseForge 1）、Walk存储 8、白名单剔除 0 → sources.json 共 190 条
```

### 5.3 直链白名单校验（关键）

只有同时满足以下条件的 URL 才写入 `sources.json`，否则一律剔除（走存储）：

| 条件 | 说明 |
|---|---|
| `source ∈ {modrinth, curseforge}` | `manual` 条目一律不给直链 |
| URL 命中白名单前缀 | `https://cdn.modrinth.com/data/` 或 `https://edge.forgecdn.net/files/` |
| 无空格 / 无多 URL 拼接 | 过滤 §2.1 那类脏数据 |
| scheme 为 `https` | 拒绝 `http` / 其他协议 |

### 5.4 首次 `--backfill`

存量约 190 条有可用 `downloadUrl`，其中绝大多数来自本地 lock（**0 次 API**）；
剩余少量走 Modrinth 批量 sha1 反查（800/批，一次请求即可）+ CF 指纹反查。
**一次性执行**，之后回到"只查变更"。

### 5.5 保底命令 `rebuild-sources`（你追加的要求）

**独立子命令，不进日常发布流程**：不碰清单、不碰 blob，只覆盖 `sources.json` 一个对象。

```
mcmodsync rebuild-sources [--dry-run] [--no-cf] [--lock mods.lock.json]
```

它做的是"彻底重来"：

| 项 | `publish-client --backfill` | `rebuild-sources` |
|---|---|---|
| 触发场景 | 索引里个别条目脏了 | 索引整体损坏 / 大面积失效 / 平台换 CDN 域名 |
| 反查范围 | 当前 `client-mods/` 全部文件 | 同左（完全重建，**不继承任何旧索引**） |
| 会不会发新版本 | **会**（版本号 +1，玩家要更新一次） | **不会**（版本号、清单一个字不改） |
| 上传对象 | 清单 + 索引 + 指针 | 只有 `sources.json` |
| 未命中项 | 日志统计 | **逐个列出**（前 20 个 + 总数），方便人工判断要不要手动指派 |
| 安全阀 | — | `--dry-run` 只打印结果，零写入 |

自愈能力：即便对象存储里的 `sources.json` 被删光，玩家端也只是静默回落对象存储；
重跑一次 `rebuild-sources` 即可恢复（`lock` 命中率越高，恢复越快、越省 API 配额）。

---

## 6. C 端改造明细

### 6.1 新增模块 `mcmodsync/source_index.py`（纯标准库）

| 函数 | 职责 |
|---|---|
| `fetch_index(pack_root, public_key, timeout=3, log=None) -> Optional[dict]` | GET + 验签 + packId 校验；任何异常都返回 `None`（**绝不抛出**） |
| `load_cached(target) -> dict` | 读 `<target>/_updater/sources.json`，损坏/缺失 → `{}` |
| `save_cached(target, obj) -> None` | 原子写（临时文件 + `os.replace`） |
| `lookup(obj, sha256) -> Optional[str]` | 取直链 + 白名单复检（**在 C 端也复检一次**，双重保险） |

**红线**：本模块不导入任何第三方包（`tests/test_stdlib_purity.py` 会守）。

### 6.2 拉取与替换时序

插入位置：`client.py` 步骤 6（版本回退判定）之后、步骤 7（规划）之前。

```
idx = source_index.load_cached(target)                  # 先拿本地缓存兜底
if cfg.get("preferPlatform", True):
    fresh = source_index.fetch_index(base, cfg["publicKey"], 3, trace)   # 失败返回 None
    if fresh is not None:
        source_index.save_cached(target, fresh)          # 原子覆盖老 json
        idx = fresh
```

**为什么放这里**：此时已拿到 `pointer_url`（可算 pack 根），且尚未构造下载任务；
失败也不会污染任何状态。

### 6.3 下载分支（终端的"静默"定义）

| 事件 | 终端 | 日志文件 |
|---|---|---|
| 命中直链并下载成功 | 正常进度条，来源显示 `Modrinth` / `CurseForge` | 正常 |
| 直链超时 / 404 / 403 / 内容截断 | **无任何输出**（不换行、不提示） | `[trace] 平台直链不可用，回落对象存储: <文件名>` |
| 直链下载完成但 sha256 不符 | **无任何输出** | `[trace] 平台直链内容校验不符，回落对象存储: <文件名>` |
| 回落仍失败 | 正常失败提示（与现在一致） | 正常失败日志 |

> **"静默"落实为：终端 0 行输出，日志保留 1 行。**
> 若要求连日志也不留，请在 §12 说明（不建议：排障时无法区分"本来就没直链"和"直链挂了"）。

### 6.4 改动点

| 文件 | 改动 | 红线 |
|---|---|---|
| `mcmodsync/source_index.py` | **新增**（§6.1） | 纯标准库 |
| `mcmodsync/client.py` | 步骤 6.5 拉取索引（§6.2）；构造下载任务时用 sha256 查表，把命中条目的 `downloadUrl` 附加到任务 dict（**不改变任务集合、顺序、并发策略**） | 不碰规划逻辑 |
| `mcmodsync/http_download.py` | `download_one()` / `download_blobs()` 新增可选 `alt_url`：非空时先试一次（8s、不重试），失败**丢弃残留并走原有 url 全流程** | **不改** sha256 校验、原子落位、分片算法、重试退避；存储路径代码原样保留 |
| `mcmodsync/client_main.py` / `console.py` | `\|来源:\|` 三种取值：`Modrinth` / `CurseForge` / `对象存储`（查表命中即平台名） | 仅渲染层 |
| `mcmodsync/config.py` | 新增可选 `client.preferPlatform`（bool，默认 `true`） | 缺省即启用 |
| `mcmodsync/docs/C端使用教程.md` | **不提及**多源下载（你的决定） | — |

### 6.5 关键约束

1. **staging 路径不变**：无论从哪下载，仍落到 `blobs/<sha[0:2]>/<sha[2:4]>/<sha>`，后续流程零感知。
2. **超时与重试**：平台直链 **只试 1 次、超时 8s、不重试**，失败**立即**回落。
   绝不能复用存储路径的"3 次 × 递增退避"——否则玩家要干等 3 分钟才回落。
3. **校验统一**：两条路径的产物都交给**同一个** `verify_fn` 校验后才 `os.replace` 落位。校验逻辑一行不改。
4. **分片兼容**：平台 CDN 支持 Range，可继续分片；若返回非 206，已有"回退单连接"逻辑兜住，无需新增。
5. **索引拉取不阻断同步**：`sources.json` 拿不到 = 少一层优化，绝不返回非 0 退出码。

---

## 7. 兼容性与回滚

| 项 | 结论 |
|---|---|
| 旧版 C 端 exe | **根本不请求** `sources.json`，继续走存储 → 行为完全不变 |
| `sources.json` 缺失 | C 端静默视为空表 → 全走存储（等同现状） |
| 版本清单 `schemaVersion` | **保持 1**（清单结构未变） |
| `sources.json` 的 `schemaVersion` | 独立为 1；将来扩容（如加 `md5`）时递增，老 C 端按 §6.1 校验值不符即忽略 |
| 存储桶 | **永久保留**全部被引用 blob；GC 规则不变 |
| 回滚手段 | ① 配 `client.preferPlatform=false`；② A 端 `--no-resolve` 发布（不更新索引）；③ 直接删对象存储里的 `sources.json`（C 端静默回落）；④ 历史清单已全量保留，可回退版本 |

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 国内直连 Modrinth / forgecdn 慢或被加速器拦 | 玩家等待 | 短超时 8s + 只试 1 次 + 立即回落 + 开关 |
| 每次启动多 1 次网络请求 | 启动慢 ~0.2s | 3s 超时 + `max-age=300` + `If-None-Match`(304) + 失败静默 |
| 平台直链内容与预期不符（作者回链/换包） | 下载错误文件 | 沿用 sha256 校验，不通过即回落（**安全**） |
| CF 作者禁用第三方下载 | 无法直连 | 白名单过滤 → 不写入索引 → 走存储 |
| lock 脏 URL 被误采信 | 浪费一次请求 | §5.3 白名单（A 端写入时 + C 端读取时**双重校验**） |
| `sources.json` 与清单不一致 | 索引指向旧文件 | C 端**以 sha256 为唯一键**：清单里没有的 sha256 根本用不到；A 端重建时按当前清单裁剪 |
| `sources.json` 上传失败 | 索引滞后一版 | 不阻断发布，警告；下次发布会重建（基准含旧索引，无损） |
| 索引被篡改 | 下载到错误内容 | 签名验签（§4.6）+ sha256 校验双保险 |
| 多一个失败面 | 可用性 | 所有异常路径都收敛到"走存储"，不会让同步失败 |

---

## 9. 测试计划

| 测试文件 | 新增用例 |
|---|---|
| `tests/test_source_resolve.py`（新） | murmur2 已知向量；sha1 正确；白名单拒绝脏 URL / 非白名单域名 / http；`manual` 不给直链 |
| `tests/test_sources_index.py`（新） | 构建+裁剪（删除的 sha256 消失）；未变更条目按 sha256 继承；`--backfill` 全量；`--no-resolve` 不写；lock 命中 0 次网络；签名/验签往返 |
| `tests/test_publisher_minio.py` | 上传顺序为 `manifests → sources.json → manifest.json`；`sources.json` 上传失败不阻断发布；GC 不误删 `sources.json` |
| `tests/test_client.py` | 启动拉取并覆盖本地缓存；拉取失败静默沿用缓存；无缓存 → 全走存储；命中索引时优先平台；平台 404/超时 → **静默**回落且同步成功；平台内容不符 → 回落；`preferPlatform=false` → 不请求索引 |
| `tests/test_client_main.py` | 来源标签三种取值；**直链失败时终端零输出**（断言 stdout 不含新提示行） |
| `tests/test_stdlib_purity.py` | C 端仍为纯标准库（`source_index.py` 不得引入第三方依赖） |

回归要求：除既有的 `test_loose_keeps_self_installed_and_strict_deletes`（历史既有失败，与本改造无关）外，**0 失败**。

---

## 10. 实施阶段划分

### 阶段 1 — A 端生成并上传索引（低风险）✅ 已完成
- 新增 `source_resolve.py`、`build_sources_index()` / `resolve_index_for_publish()`；`publish-client` 上传 `sources.json`；新增 `--backfill` / `--no-resolve` / `--no-cf`
- **验收**：对象存储出现 `sources.json`；`sources` 键全是当前清单里的 sha256；C 端未改时行为零变化
- 附带交付：保底命令 `rebuild-sources`（见 §5.5）

### 阶段 2 — C 端拉取 + 多源下载（核心）✅ 已完成
- 新增 `source_index.py`；启动拉取覆盖本地；`alt_url` 优先直链 + 静默回落；`preferPlatform` 开关；来源标签
- **验收**：日志/抓包证实优先从 `cdn.modrinth.com` / `edge.forgecdn.net` 下载；人为切断平台后**终端无提示**但同步成功；删掉对象存储里的 `sources.json` 后一切照旧

### 阶段 3 — 分发 ✅ 已完成（2026-09-22）
- 已把系统版本升到 **2.1.0**（`pyproject.toml` / `tools/make_release.py` / C 端包内说明统一）
- 已重打 C 端 exe（`mcmodsync package-client`）并重发 `dist/release/` 三端包（A 26 万 / B 1.7 万 / C 944 万字节）
- 已写 `dist/release/RELEASE_NOTES-v2.1.0.md`（含三包 SHA256）
- A 端包已含 `source_resolve.py` 与 `rebuild-sources`
- 注意：**C 端教程不改**（你的决定，§0 第 3 项）

### 附带修复 — C 端游戏运行检测误判（2026-09-22）
- 旧实现用 `tasklist`/`ps` 全量文本做子串匹配，**裸 `java` 也算命中** → 机上有任意 Java 程序
  （IDEA / 其它 Java 应用 / MC 服务端 / 另一个整合包）就误判"游戏运行中"并退出码 3
- 现改为**只认 Minecraft 客户端**：映像名含 `minecraft` → 拦截；`java/javaw` 看命令行，
  命中 MC 客户端特征且非专用服务端（`nogui` / `server.jar` / `net.minecraft.server` …）才拦截；
  命令行取不到时不拦截（真在跑的客户端会占用 jar，写入时干净失败）
- 命令行来源：`wmic` → 回退 PowerShell CIM（新系统已移除 wmic）；仅在存在 java 进程时才取
- 文档同步：`玩家使用说明.md` / `故障排查与应急换钥.md`

---

## 11. 明确不做（边界）

- 不改 sha256 校验、原子落位、分片、重试退避、文件锁、备份、清单读取的**业务控制逻辑**
- 不撤除对象存储桶（永久兜底）
- 不改版本清单结构、不升 `schemaVersion`
- 不改 `scan_client_mods()` 的纯哈希扫描职责
- 不改 `push-server` / `fetch-mods` 行为
- 不为服务端 mod 引入平台来源（服务端走 SSH 直推，不需要）
- 不在 C 端教程中说明"流量走官方 CDN"
- 不做 changelog 命令

---

## 12. 待确认事项 —— 已按推荐值实施

| # | 事项 | 采用的默认值 | 依据 |
|---|---|---|---|
| 1 | `sources.json` 是否签名 | **签**（与清单同一把 Ed25519 私钥） | §4.6：成本≈0，防篡改；验签失败静默忽略 |
| 2 | 上传失败是否阻断发布 | **不阻断**（警告后继续写指针） | 它只是加速层，缺了玩家回落对象存储 |
| 3 | `preferPlatform` 默认值 | **true**（启用多源） | 你已要求这套机制；玩家可一键关闭 |
| 4 | 本地缓存路径 | `<target>/_updater/sources.json` | 与 `state.json` 同目录，随实例走 |
| 5 | 拉取超时 | **3 秒** + `max-age=300` | 短超时，失败静默，不拖慢同步 |
| 6 | 直链失败提示 | **终端 0 行**、日志 1 行 | 你的决定（§0 第 2 项） |
| 7 | 白名单校验 | A 端写入 + C 端读取**双重校验** | 防脏数据与漂移（有测试守住两端常量一致） |

---

## 13. 实施记录（v2.1）

### 新增文件

| 文件 | 内容 |
|---|---|
| `mcmodsync/source_resolve.py` | A 端反查（lock → Modrinth 批量 sha1 → CF 指纹）+ 白名单 `is_allowed_url()` |
| `mcmodsync/source_index.py` | C 端索引（纯标准库）：`fetch_index` / `load_cached` / `save_cached` / `lookup` |

### 改动文件

| 文件 | 改动 |
|---|---|
| `mcmodsync/publisher.py` | `SOURCES_KEY`/`SOURCES_CC`；`build_sources_index()`；`resolve_index_for_publish()`；`read_store_sources()`；`fetch_sources_index()`；`_published_version()`；`rebuild_sources()`；`publish_client()` 新增步骤 4.6 解析 + 步骤 7.5 上传（在指针之前） |
| `mcmodsync/cli.py` | `publish-client` 加 `--backfill` / `--no-resolve` / `--no-cf`；新增 `rebuild-sources` 子命令 |
| `mcmodsync/http_download.py` | `download_one(..., timeout=)`；`PLATFORM_TIMEOUT=8` / `PLATFORM_RETRIES=1`；`_try_platform()`；`download_blobs` 支持任务里的 `altUrl`（失败静默回落，计数归零） |
| `mcmodsync/client.py` | `sources_path()`；`load_source_index()`；下载前拉取索引并按 sha256 附加 `source` / `altUrl`；`sync(..., prefer_platform=True)` |
| `mcmodsync/client_main.py` | `sync --no-platform` |
| `mcmodsync/packaging.py` | C 端 `config.json` 增加 `preferPlatform`（默认 true） |

### 明确未动的逻辑（红线自查）

sha256 校验、原子落位、分片算法、重试退避、文件锁、备份、清单读取、`scan_client_mods()`、`plan_changes()`、
`gc_unreferenced()`、版本清单结构 —— **一行未改**；旧版 C 端 exe 不请求 `sources.json`，行为与改造前完全一致。

### 测试

| 文件 | 内容 |
|---|---|
| `tests/test_source_resolve.py`（新） | 白名单（含 lock 真实脏数据）、murmur2/哈希与 `tools/gen_lock.py` **逐项比对**、lock 命中零联网、MR/CF 反查、白名单剔除、无 apiKey 跳过 CF、断网模式 |
| `tests/test_sources_index.py`（新） | 两端白名单常量一致、`index_url` 推导、缓存读写/损坏容错、查表命中与拒绝、拉取成功/未签名/异钥/packId 不符/schema 过新/不可达（静默） |
| `tests/test_publisher_sources.py`（新） | 纯函数裁剪与继承、上传顺序（清单 → 索引 → 指针）、跨版本裁剪与继承、`--backfill` / `--no-resolve`、上传失败与反查崩溃**均不阻断发布**、dry-run 零写入、`rebuild-sources` 覆盖与 dry-run |
| `tests/test_client.py`（增补） | 命中索引走平台直链（且**完全不碰**对象存储 blob）、404 静默回落、内容不符（sha256 拦下）回落、未签名索引被忽略、每次同步覆盖本地缓存、无需下载时不拉索引、`--no-platform` / `preferPlatform=false`、缓存损坏仍可同步、无索引时行为不变 |
| `tests/test_stdlib_purity.py`（增补） | `source_index` 纳入纯标准库与 3.8 语法校验 |

其余细节（默认启用 `preferPlatform`、本地缓存 `_updater/sources.json`、拉取超时 3s、终端静默+日志留 1 行、白名单双重校验）均按本稿推荐值实施，如无异议请回复"按稿执行"。
