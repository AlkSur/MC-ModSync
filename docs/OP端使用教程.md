# OP端使用教程（A 端）

你是服主/OP。这个教程只讲怎么用，不讲原理。

## 一、一次性准备（只做一次）

1. 装好 Python 3.10 以上版本，安装依赖：

   ```
   pip install boto3 paramiko cryptography
   ```

2. 复制 `pack.example.json` 为 `pack.local.json`，填好真实值（服务器地址、SSH、七彩云桶、AK/SK详细可查看- [管理员部署指南](管理员部署指南.md)）。这个文件**不要提交 git**（.gitignore 已默认排除）。

3. 生成签名密钥：

   ```
   mcmodsync keygen
   ```

   把输出的公钥填进 `pack.local.json` 的 `client.publicKey`。

4. 建两个源文件夹：

   - `server-mods/` —— 放服务端 mod
   - `client-mods/` —— 放客户端 mod

5. 自检，全绿就可以开始用了：

   ```
   mcmodsync doctor
   ```

## 二、日常发布流程（每次更新 mod 就按这个走）

### 第 1 步：整理 mod

- **平台能下到的 mod**：写进 `mods.lock.json`，然后跑：

  ```
  mcmodsync fetch-mods
  ```

  它会自动从 Modrinth / CurseForge 下载，按你写的 `side`（server / client / both）放进对应源文件夹。下不了的（CurseForge 禁止第三方下载的）会列出来，你手动下载放进去，再跑：

  ```
  mcmodsync fetch-mods --lock-manual
  ```

- **自己写的 mod / 本地已有的 jar**：直接丢进 `server-mods/` 或 `client-mods/` 就行，不用登记。

- 想升级所有 mod 到最新版：

  ```
  mcmodsync fetch-mods --upgrade
  ```

### 第 2 步：推送到服务器

先看看会改什么（不写任何东西）：

```
mcmodsync push-server --dry-run
```

确认无误，正式推送：

```
mcmodsync push-server
```

推送期间服务器**不用停**。

### 第 3 步：手动重启 MC 服务器，亲眼确认

- 日志出现 `Done (` → 成功，走第 4 步。
- 启动失败 → 回滚：

  ```
  mcmodsync rollback-server
  ```

  再手动重启确认恢复，修好 mod 后回到第 2 步。**这个过程中不要发布客户端，玩家无感知。**

### 第 4 步：发布客户端到对象存储

```
mcmodsync publish-client --version 1.0.1 --notes "更新了XX，移除了XX"
```

版本号每次要比上次大（1.0.0 → 1.0.1 → 1.0.2……）。

### 第 5 步：通知玩家

让玩家双击"更新mod"，完成后自己打开启动器进游戏。

## 三、命令速查

| 命令 | 干什么 |
| --- | --- |
| `mcmodsync doctor` | 全身体检（配置、SSH、对象存储、密钥） |
| `mcmodsync fetch-mods` | 按 mods.lock.json 从平台下载 mod 到源文件夹 |
| `mcmodsync fetch-mods --upgrade` | 全部升级到最新版 |
| `mcmodsync fetch-mods --lock-manual` | 手动放的 mod 登记锁定 |
| `mcmodsync push-server` | 推送服务端 mod 到 MC 服务器 |
| `mcmodsync push-server --dry-run` | 只看不改 |
| `mcmodsync rollback-server` | 回滚服务端到上一版 |
| `mcmodsync publish-client --version X.Y.Z` | 发布客户端到对象存储 |
| `mcmodsync package-client` | 重新打包玩家端（只有更新器本身改了才需要） |

## 四、常见问题

- **push-server 报码 11**：脚本会自动重传并重试一次；还失败就把提示里的 B 日志路径发给维护者。
- **publish-client 忘了写 --version**：会直接报错，补上版本号再跑。
- **换电脑了**：带上 `pack.local.json`、私钥（`~/.mcmodsync/private.key`）和 mods.lock.json 就能接着干。
- **怀疑凭证泄露**：先跑 `mcmodsync doctor` 里的泄露检查；真泄露了必须换钥，光删文件没用。
