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

### 手上没有现成压缩包？两种办法

**办法一：用整个源码仓库当 A 端（最直接）**

A 端**就是整个仓库**，不需要额外拆分。拿到源码（git clone 或下载 zip）后，只要确认这些目录/文件齐全就能直接开工：

```
mcmodsync\  server\  client\  packaging\  entries\  tools\
pyproject.toml  pack.example.json  mods.lock.json
```

**办法二：自己生成三端压缩包（一条命令）**

在仓库根目录执行：

```
.\.venv\Scripts\python.exe tools\make_release.py --build
```

它会在 `dist\release\` 下生成三个包，可以直接分发：

```
MC-ModSync-A端-OP工具-v2.1.0.zip        ← 你自己用
MC-ModSync-B端-服务端脚本-v2.1.0.zip    ← 给管服务器的人
MC-ModSync-C端-玩家更新器-v2.1.0.zip    ← 发给玩家
```

`--build` 会先跑 `mcmodsync package-client`（构建 exe + 渲染 config.json）再打三个包，所以**一条命令就够了**。

> **前提**：需要 dev 依赖（PyInstaller）。如果报 `No module named PyInstaller`，先执行一次：
> ```
> .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
> ```
>
> 如果 `dist\client-package\` 已经是最新的（没改过 C 端代码），可以省掉 `--build`：
> ```
> .\.venv\Scripts\python.exe tools\make_release.py
> ```

### 三端之间怎么分

| 端 | 从仓库里拿什么 | 给谁 |
| --- | --- | --- |
| **A 端** | 整个仓库 | 你自己（服主/OP） |
| **B 端** | 只有 `server\mcmodsync-b.py` 一个文件 | 管服务器的人（通常不用手工给，推送时自动上传） |
| **C 端** | 跑 `mcmodsync package-client` 生成的 `dist\client-package\` | 玩家 |

C 端**不能直接把源码发给玩家**——`package-client` 会把你的 CDN 地址和公钥渲染进 `config.json`，这一步必须由你来跑。

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

### 先看命令清单（照着敲）

打开 PowerShell，**每次都从这两行开始**（新开的窗口必须重新激活）：

```powershell
cd F:\Storage\ai\plugins-Dev\mc-modsync      # 进仓库目录（必须，程序默认读这里的配置）
.\.venv\Scripts\Activate.ps1                 # 激活虚拟环境，提示符前会出现 (.venv)
```

然后按下面顺序执行，**一步都不能少、顺序不能换**：

| # | 命令 | 干什么 | 大概耗时 |
| --- | --- | --- | --- |
| 1 | （手动）把新 mod 放进 `server-mods\` 和 `client-mods\` | 替换/新增/删除文件 | — |
| 2 | `mcmodsync push-server --dry-run` | 只看会改什么，不写任何东西 | 几秒 |
| 3 | `mcmodsync push-server` | 推送服务端（只传变化的 jar） | 几秒~几分钟 |
| 4 | （手动）**重启 MC 服务器**，日志看到 `Done (` | 亲眼确认服务端没问题 | — |
| 5 | `mcmodsync publish-client --dry-run` | 预览 + **缓存来源索引反查结果**（这一步最耗时） | 几秒~两分钟 |
| 6 | `mcmodsync publish-client --notes "更新了XX"` | 发布客户端（版本号自动 +1，**复用第 5 步缓存秒过反查**） | 几十秒 |
| 7 | （手动）通知玩家双击"更新mod" | 玩家侧增量更新 | — |

> **为什么第 4 步不能跳**：客户端发布是不可逆的分发动作。必须等服务端重启验证通过再发布，
> 否则玩家拿到新 mod 却进不去服——这是整套流程的"失败不上传"保障。
>
> **版本号（自动）**：不用写 `--version`，每次发布会自动在上一版基础上 +1。
> 格式为 `A.B.C`（A=大版本 1-9、B=版本类 0-9、C=小版本 0-6）：
> C 满 6 进位到 B、B 满 9 进位到 A，如 `1.1.5 → 1.1.6 → 1.2.0 → 2.0.0`。
> 首次发布从 `1.0.0` 开始。想手动指定（如大改版直接跳 `2.0.0`）可写 `--version 2.0.0`，格式不符会报错。
>
> **只改了服务端 mod（没碰 client-mods）**：不要发布客户端，客户端版本号不会变。

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

先看看会发布什么（不写任何东西）：

```
mcmodsync publish-client --dry-run
```

确认无误，正式发布（**版本号不用写，自动 +1**）：

```
mcmodsync publish-client --notes "更新了XX，移除了XX"
```

`--notes` 写的是本次更新说明，会随版本清单永久保存在对象存储里，之后可随时回查每个版本改了什么。终端只显示本版相对上一版的新增/替换/删除；历史遗留的删除记录不会重复打印，但玩家升级清单里仍然完整保留。

> **两步必须同一个终端窗口、间隔不超过 30 分钟**：`--dry-run` 会把最耗时的"来源索引反查"结果缓存到 `.publish-dryrun-cache.json`，正式发布直接复用、不再联网反查。以下任一情况都会被拒绝并要求重新 dry-run：
> - 换了终端窗口跑正式发布（防跨窗口误用）；
> - dry-run 之后又动了 `client-mods/` 里的 jar；
> - 云端清单被别的发布更新过（防覆盖别人的版本）；
> - 距 dry-run 超过 30 分钟。
> 发布成功后缓存自动作废。确实不想更新来源索引时，用 `publish-client --no-resolve` 可跳过这套流程直接发布。

#### 顺带更新"下载源索引"（自动，不用管）

发布时还会覆盖上传一份 `sources.json`（**下载源索引**，与清单同一个目录）。玩家端每次同步前拉一次，命中平台直链就从 Modrinth / CurseForge 官方 CDN 下载，失败**自动静默回落**对象存储 —— 传输链路对玩家完全无感，只是帮你省对象存储的出网流量。索引只对**本次变更**的 mod 联网反查，其余从上一版按哈希继承。

| 场景 | 怎么做 |
| --- | --- |
| 平时 | 什么都不用管，`publish-client` 自带这一步 |
| 只想发清单、不动索引 | `mcmodsync publish-client --no-resolve` |
| 索引里数据脏了 / 想全量重查 | `mcmodsync publish-client --backfill` |
| 索引整体损坏，要彻底重来 | `mcmodsync rebuild-sources`（保底命令，全量重查并覆盖上传） |
| 不查 CurseForge（限流或没配 apiKey） | 任意一条命令后面加 `--no-cf` |

`sources.json` 上传失败**不会**让发布失败（它只是加速层，缺了玩家就全走对象存储），终端最多打一行警告。

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
| `mcmodsync publish-client --dry-run` | 预览客户端会发布什么（不写任何东西），并**缓存来源索引供正式发布复用** |
| `mcmodsync publish-client --notes "..."` | 发布客户端到对象存储（版本号自动 +1） |
| `mcmodsync publish-client --version 2.0.0 --notes "..."` | 手动指定版本号发布（格式 A.B.C：A=1-9、B=0-9、C=0-6） |
| `mcmodsync publish-client --no-resolve` | 本次不更新下载源索引（线上旧索引保持不变） |
| `mcmodsync publish-client --backfill` | 来源索引全量重查一遍（保底修复时用） |
| `mcmodsync rebuild-sources` | **保底**：全量重查 client-mods 并覆盖重建 `sources.json`（索引损坏时才用，不进日常流程） |
| `mcmodsync rebuild-sources --dry-run` | 先看会重建出什么，不写任何东西 |
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
- **publish-client 报"版本号须为 A.B.C"**：手动指定的 `--version` 格式不对（A=1-9、B=0-9、C=0-6，如 `2.0.0`）。不写 `--version` 就不会遇到这个问题——自动递增永远合法。
- **`fetch-mods` 摘要三项都是 0**：因为 `mods.lock.json` 的 `mods` 是空数组。它只处理登记过的条目，**不会扫目录里已有的 jar**。用 `python tools\gen_lock.py` 自动登记一遍即可；已经放好的 jar 不去登记也不影响推送和发布。
- **下载源索引坏了 / 玩家反馈"来源"不对**：跑一次 `mcmodsync rebuild-sources`（会全量重查 client-mods 并覆盖上传）；想先看结果就加 `--dry-run`。想**彻底停用**多源下载，直接把对象存储里的 `sources.json` 删掉即可 —— 玩家端下次同步静默回落对象存储，什么都不用改。
- **玩家反馈更新变慢**：让玩家把 `_updater/config.json` 里的 `"preferPlatform"` 改成 `false`（或临时用 `mcmodsync sync --no-platform`），就回到"全部走对象存储"的老行为。
- **换电脑了**：带上 `pack.local.json`、私钥（`~/.mcmodsync/private.key`）和 mods.lock.json 就能接着干。
- **怀疑凭证泄露**：先跑 `mcmodsync doctor` 里的泄露检查；真泄露了必须换钥，光删文件没用。
