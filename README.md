# MC-ModSync

Minecraft NeoForge 服务端 mods 增量同步系统。三端构成：

- **A 端（OP 的 PC）**：Python 包 + CLI，从 Modrinth/CurseForge 拉取 mod，经 SSH 推送
  服务端变更，回滚服务端，发布客户端清单与 blob 到对象存储（七彩云）。
- **B 端（MC 服务器）**：单文件 `mcmodsync-b.py`，仅用 Python 标准库（兼容 3.8），
  非常驻，经 SSH 按需调用。负责清单导出、应用变更、备份、回滚。
- **C 端（玩家 PC）**：纯标准库，PyInstaller 单文件 exe。拉清单、验签、增量下载、
  原子替换；双击"更新mod"后手动启动 PCL2/HMCL。

## 目录结构

见 `docs/`（文档五件套）与仓库目录树。核心代码在 `mcmodsync/`（A 端包）、
`server/`（B 端源码）、`tools/`（构建与运维脚本）、`tests/`（测试）。

## 开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

## 配置文件

- `pack.example.json`：提交仓库的占位模板。
- `pack.local.json`：真实的 `endpoint/桶/AK/SK/SSH` 等，**不入库**（见 `.gitignore`）。

## 安全

凭证零入库（AC-6）：`pack.local.json`、`ssh.json`、`private.key`、`*.pem` 一律被
`.gitignore` 覆盖。
