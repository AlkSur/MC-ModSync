# mod 获取策略与 mods.lock.json 字段冻结（T-30）

## 1. 优先级硬规则

同一 mod 的获取顺序固定如下，**不可调换**：

1. 先查 **Modrinth**（`https://api.modrinth.com/v2/...`）；
2. Modrinth 无此项目 → 查 **CurseForge**（`gameId=432` 为 Minecraft）；
3. CurseForge 禁第三方下载（API 返回 `downloadUrl` 为空）→ `source` 置 **`manual`**，
   输出人工下载清单，走人工闭环（T-34）；禁止经第三方站点下载。

`mods.lock.json` 中未显式指定 `source` 时，按上述顺序自动探测。

## 2. 环境锚点（实测）

| 项 | 值 | 依据 |
| --- | --- | --- |
| Minecraft 版本 | **1.21.1** | 服务端 `/www/demo-pack1.1.12/mods` 内文件名，如 `alloy_smelter-neoforge-1.21.1-1.1.2.jar`、`ApothicAttributes-1.21.1-2.9.1.jar` |
| 加载器 | **neoforge** | 同上，如 `architectury-13.0.8-neoforge.jar` |

> 注: 计划 [3.12] 的示例值为 `1.20.1`，仅为样例；实际取值以上表实测为准。本记录不修改计划原文（R-4）。

## 3. mods.lock.json 字段冻结（对齐 [3.12]）

| 字段 | 类型 | 必填 | 语义 |
| --- | --- | --- | --- |
| `schemaVersion` | int | ✅ | 固定 `1` |
| `packMeta.minecraftVersion` | str | ✅ | 如 `"1.21.1"`；用于 provider 的 `game_versions` 过滤 |
| `packMeta.loader` | str | ✅ | `"neoforge"` |
| `mods[].name` | str | ✅ | 展示名 |
| `mods[].side` | `server` / `client` / `both` | ✅ | 落盘路由，见第 4 节 |
| `mods[].source` | `modrinth` / `curseforge` / `manual` | ✅ | 未指定时按第 1 节自动探测 |
| `mods[].projectSlug` | str | ✅ | 平台项目 slug |
| `mods[].versionPin` | `latest` / 具体版本号 / `manual` | ✅ | 仅 `--upgrade` 时重新解析 `latest` |
| `mods[].resolvedVersion` | str \| null | ⬜ | 下载后回写 |
| `mods[].fileName` | str | ✅ | 目标文件名（落 `mods/` 平铺） |
| `mods[].sha256` | str \| null | ⬜ | 下载后实算并回写（平台只给 sha1/sha512，本项目统一存 sha256） |
| `mods[].size` | int \| null | ⬜ | 下载后实算并回写 |
| `mods[].downloadUrl` | str | ⬜ | 下载后回写；`manual` 时为项目页链接 |
| `mods[].note` | str | ⬜ | 备注（如"禁第三方下载，人工放入源目录后跑 fetch-mods --lock-manual"） |

字段确认后**冻结**；增删字段须升级 `schemaVersion` 并同步 A/C 两端与文档。

## 4. side 路由

- `server` → 落 `server-mods/`
- `client` → 落 `client-mods/`
- `both` → 两目录各一份（**下载一次，复制一次**）

## 5. 平台项目页

- Modrinth：`https://modrinth.com/mod/<slug>`
- CurseForge：`https://www.curseforge.com/minecraft/mc-mods/<slug>`

## 6. 变化处理

- `versionPin="latest"` 仅在 `fetch-mods --upgrade` 时重新解析并回写。
- provider API 结构变更 → 触发分支 **B-04**（provider 层隔离修改，`fetcher` 编排不动）。
- 平台整体下线 → 对应 `source` 整体降级为 `manual` 并更新本文档。

## 7. 人工介入（🔴）

本策略与字段冻结需**需求方确认**后方可进入 T-31 / T-32（`[T-30]` 成功判定：
"需求方确认优先级策略；格式冻结"）。

已在 2026-09-18 获需求方确认（"正确"），本策略生效。

## 8. CurseForge 真机验证记录（T-32，2026-09-18）

apiKey（PRE-5）已配置于 `pack.local.json` → `curseforge.apiKey`（长度 60，`$2a$10$` 前缀，
bcrypt 样式），该文件已被 `.gitignore` 覆盖，不入库。

真机结论（Minecraft 1.21.1 / NeoForge）：

| 项 | 结果 |
|---|---|
| 鉴权有效性 | 通过 —— `GET /v1/games`、`/v1/games/432`、`/v1/mods/{id}`、`/v1/mods/{id}/files`、`/v1/categories` 均 200 |
| slug 检索（步骤1） | 通过 —— `search_mod("jei")` 返回 `id=238222` |
| 允许第三方下载的 mod 真实拉取 | 通过 —— JEI → `jei-1.21.1-neoforge-19.56.0.441.jar`，2156268 字节，sha256 实算一致 |
| 禁第三方下载判定（manual-needed） | 通过 —— `not-enough-animations`（modId 433760）在 1.21.1/NeoForge 下 25/25 文件 `downloadUrl` 为空，`resolve()` 返回 `manualNeeded=true`、`downloadFile()` 抛码 5 |

**环境备注（非平台问题，不需触发 B-04）**：调试期间曾观察到本机出口链路
*间歇性* 拦截小写字面量路径 `/v1/mods/search`（返回 `HTTP 403 text/plain
"Forbidden: API Key missing or invalid"`，`Server: Kestrel`），而同一 key 下其它端点均 200、
且该路径的**大小写变体**（如 `/v1/mods/Search`）同刻返回 200。证据链指向出口链路对该字面量的
临时拦截，而非 CurseForge 接口变更：无代理环境变量、禁用代理无效、随机 cache-buster 无效
（`X-Cache: Error from cloudfront`）、任意鉴权形式（header/query/Bearer）均被拒、
而无 key 时同路径返回 **空 body** 403（真鉴权失败形态不同）。该现象已自行消失（连测 5/5 恢复 200）。
为抗此偶发情况，真机用例内置探测：优先走生产 `search` 腿，仅当探测到拦截时才以真实
`GET /v1/mods/{id}` 结果替换检索腿（其余逻辑始终为生产代码）。
