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
| Minecraft 版本 | **1.21.1** | 服务端 `/www/zdjxd1.1.12/mods` 内文件名，如 `alloy_smelter-neoforge-1.21.1-1.1.2.jar`、`ApothicAttributes-1.21.1-2.9.1.jar` |
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
