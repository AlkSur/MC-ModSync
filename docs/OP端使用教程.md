# OP端使用教程（A 端）

你是服主/OP。这个教程只讲怎么用，不讲原理。

## 〇、你需要哪些文件

你手上的 A 端压缩包解压后应该是这个结构，**全部留着**——跑 `package-client` 时会用到 `client\`、`packaging\`、`entries\`：

```
mc-modsync\
├── mcmodsync\               ← A 端 Python 包（核心，必需）
├── server\mcmodsync-b.py    ← B 端脚本，push-server 会把它传到服务器（必需）
├── client\mcmodsync.py      ← C 端兜底源码，打包玩家端时用（必需）
├── packaging\mcmodsync.spec ← 打包玩家端 exe 用（必需）
├── entries\                 ← 玩家端入口模板（更新mod.bat / .sh，必需）
├── tools\                   ← 运维脚本：gen_lock.py 等（必需）
├── docs\                    ← 本文档
├── pyproject.toml           ← 安装用（必需）
├── pack.example.json        ← 配置模板，复制成 pack.local.json（必需）
└── mods.lock.json           ← 客户端 mod 清单（初始为空）
```

**另外两样要你自己准备**（不随包分发）：

| 东西 | 怎么来 |
| --- | --- |
| `pack.local.json` | 把 `pack.example.json` 复制改名，填服务器地址 / SSH / 七彩云 AK/SK |
| 私钥 | 首次运行 `mcmodsync keygen` 生成，存在 `~/.mcmodsync/private.key`，**换电脑要带着** |

包里**没有也不该有**：AK/SK、私钥、`server-mods/`、`client-mods/`——这些是你自己的数据。

## 零、第一步：先让 `mcmodsync` 这个命令能跑起来（必做）

`mcmodsync` 不是系统自带的命令，它是**这个仓库自带的一个 Python 命令行工具**。
如果你直接敲：

```
mcmodsync
```

报下面这种错，**说明命令是有的，只是它所在的目录没进 PATH**（虚拟环境没激活）：

```
mcmodsync: 术语 'mcmodsync' 不会被识别为 cmdlet、函数、脚本文件或可执行程序的名称。
```

在仓库根目录任选一种方式解决（推荐 A）：

**0. 先确认有没有虚拟环境（下载 zip / clone 的仓库默认没有）**

仓库根目录里如果没有 `.venv` 文件夹，就先建一个并装依赖，**只需做一次**：

```
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

第二条要联网下载依赖，装完就有了。已经有 `.venv` 的直接跳到 A。

**A. 激活仓库自带的虚拟环境（只对当前窗口有效，最稳）**

PowerShell 里：

```
.\.venv\Scripts\Activate.ps1
mcmodsync doctor
```

Git Bash 里：

```
source .venv/Scripts/activate
mcmodsync doctor
```

激活成功后提示符前面会多出 `(.venv)`。**新开一个终端窗口要重新激活一次。**

**B. 不想激活环境：用 `python -m` 方式调用**

```
.\.venv\Scripts\python.exe -m mcmodsync doctor
```

Git Bash 里：

```
./.venv/Scripts/python.exe -m mcmodsync doctor
```

**C. 装到全局 PATH（一次搞定，以后所有窗口直接敲 `mcmodsync`）**

```
.\.venv\Scripts\python.exe -m pip install -e .
mcmodsync doctor
```

> 说明：以上三种方式任选其一即可。出现
> `usage: mcmodsync [-h] [-c CONFIG] {keygen,doctor,fetch-mods,push-server,...}` 的帮助信息，就说明命令已经能用了。
>
> 另外，**所有命令都要在仓库根目录执行**（就是能看到 `mcmodsync/`、`pack.local.json` 的那一层），因为程序默认读取当前目录下的 `./pack.local.json`。

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

   结果按颜色区分，扫一眼就知道情况：

   | 颜色 | 标记 | 含义 |
   | --- | --- | --- |
   | 绿 | `[PASS]` | 通过 |
   | 红 | `[FAIL]` | 必须修，不修发布不了 |
   | 橙 | `[SKIP]` | 环境不具备，这项没检查（例如当前目录不是 git 仓库），**不影响发布** |

   要把结果存成日志文件时加 `--no-color`，免得颜色转义码混进日志：

   ```
   mcmodsync doctor --no-color > doctor.log
   ```

## 二、日常发布流程（每次更新 mod 就按这个走）

### 第 1 步：整理 mod

> **`fetch-mods` 只认 `mods.lock.json` 里登记过的条目**，它不会自己去扫目录。刚上手不用手写，用仓库自带的脚本生成：
>
> ```
> .\.venv\Scripts\python.exe tools\gen_lock.py
> ```
>
> 它会读取 `client-mods/` 里的 jar，按**文件哈希**去 Modrinth / CurseForge 反查来源；名字对不上但哈希一致的也会认出来。认出来的写成下载条目，**认不出来的标成本地文件**——本地文件照常参与推送和发布，只是不会自动升级。
>
> 结果先写到 `mods.lock.generated.json`（不碰你的 `mods.lock.json`）。看完确认没问题，再应用成正式文件：
>
> ```
> .\.venv\Scripts\python.exe tools\gen_lock.py --apply
> ```
>
> 这一步**不联网、一秒完成**，还会自动把原文件备份成 `mods.lock.json.bak`。（用 `--write` 也能一步到位写完，但那会重新联网再查一遍。）
>
> **服务端不用登记**：`server-mods/` 走 SSH 直推，不需要知道平台来源，所以脚本默认只扫客户端。要连服务端一起登记才加 `--only both`。

之后日常更新：

- **平台能下到的 mod**：直接在 `mods.lock.json` 的 `mods` 数组里加条目，然后跑：

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
| `python tools\gen_lock.py` | 扫描 client-mods/ 的 jar，自动把平台来源写进锁文件 |
| `python tools\gen_lock.py --apply` | 把生成结果应用成正式的 mods.lock.json（不联网、秒完成，自动备份原文件） |
| `python tools\gen_lock.py --write` | 一步到位生成并覆盖（会重新联网再查一遍） |
| `python tools\gen_lock.py --only both` | 连 server-mods/ 一起登记（默认只扫客户端） |

## 四、常见问题

- **`doctor` 报"publicKey 与私钥不匹配"**：说明 `pack.local.json` 里的 `client.publicKey` 和你机器上 `~/.mcmodsync/private.key` 不是一对（常见于换了电脑、或私钥被重新生成过）。**玩家端还没发出去**时，直接把私钥对应的公钥填回配置，然后重新打包玩家端即可；**已经发出去**了就必须重新分发，所有玩家都要重新下载更新器。
- **`doctor` 报"不是 git 仓库"（橙色）**：正常，解压下来的 zip / 直接拷贝的文件夹都没有 `.git`。这项只是检查凭证有没有被提交进 git，不适用就跳过，不影响使用。
- **`doctor` 报对象存储上传失败**：多半是本机网络或代理的问题（挂了代理就先关掉试试），换个网络再跑一次确认。
- **push-server 报码 11**：脚本会自动重传并重试一次；还失败就把提示里的 B 日志路径发给维护者。
- **publish-client 忘了写 --version**：会直接报错，补上版本号再跑。
- **`fetch-mods` 摘要三项都是 0**：因为 `mods.lock.json` 的 `mods` 是空数组。它只处理登记过的条目，**不会扫目录里已有的 jar**。用 `python tools\gen_lock.py` 自动登记一遍即可；已经放好的 jar 不去登记也不影响推送和发布。
- **换电脑了**：带上 `pack.local.json`、私钥（`~/.mcmodsync/private.key`）和 mods.lock.json 就能接着干。
- **怀疑凭证泄露**：先跑 `mcmodsync doctor` 里的泄露检查；真泄露了必须换钥，光删文件没用。
