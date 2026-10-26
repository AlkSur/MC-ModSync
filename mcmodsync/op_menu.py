"""OP interactive entry; all operations use the existing CLI implementation."""
from __future__ import annotations

from typing import Callable, List, Optional


def run_menu(config: str = "./pack.local.json", *,
             read: Optional[Callable[[str], str]] = None,
             write: Callable[[str], None] = print,
             dispatch: Optional[Callable[[List[str]], int]] = None) -> int:
    if read is None:
        read = input
    if dispatch is None:
        from .cli import main
        dispatch = main

    def ask(prompt: str, default: str = "") -> str:
        return read(prompt).strip() or default

    def confirm(prompt: str) -> bool:
        return ask(prompt + " [1=确认，其他=取消]：") == "1"

    def execute(command: List[str]) -> int:
        result = dispatch(command + ["--config", config])
        write("操作完成。" if result == 0 else "操作失败（返回码 %s），请查看上方提示。" % result)
        return result

    def preview_then_run(command: List[str]) -> None:
        if execute(command + ["--dry-run"]) != 0:
            write("预览失败，未执行正式操作。")
            return
        if confirm("预览完成，是否正式执行？"):
            execute(command)

    try:
        while True:
            write("\n========== MC-ModSync · OP 管理菜单 ==========\n"
                  "配置文件：%s\n"
                  "1、环境与配置检查\n"
                  "2、拉取 / 升级 Mod\n"
                  "3、推送服务端（先预览）\n"
                  "4、发布玩家 Mod 更新（先预览）\n"
                  "5、打包玩家更新器\n"
                  "6、发布更新器程序（先预览）\n"
                  "7、回滚服务端\n"
                  "8、重建下载来源索引（先预览）\n"
                  "9、生成签名密钥\n"
                  "10、切换配置文件\n"
                  "0、退出 / exit" % config)
            choice = ask("请选择编号：").lower()
            if choice in ("0", "exit", "quit", "q"):
                return 0
            try:
                if choice == "1":
                    mode = ask("1、联网检查  2、离线检查  0、返回：")
                    if mode in ("1", "2"):
                        execute(["doctor"] + (["--offline"] if mode == "2" else []))
                elif choice == "2":
                    mode = ask("1、按清单拉取  2、升级平台 Mod  3、仅预览拉取  0、返回：")
                    options = {"1": [], "2": ["--upgrade"], "3": ["--dry-run"]}
                    if mode in options:
                        execute(["fetch-mods"] + options[mode])
                elif choice == "3":
                    preview_then_run(["push-server"])
                    write("推送成功后，请手动重启服务器并检查，再选择 4 发布玩家更新。")
                elif choice == "4":
                    if confirm("是否已确认服务端重启并运行正常？"):
                        version = ask("内容版本号（回车自动递增）：")
                        notes = ask("更新说明（可留空）：")
                        command = ["publish-client", "--notes", notes]
                        if version:
                            command += ["--version", version]
                        preview_then_run(command)
                elif choice == "5":
                    out = ask("分发包输出目录（回车使用 dist/client-package）：", "dist/client-package")
                    execute(["package-client", "--out", out])
                elif choice == "6":
                    package = ask("已打包的更新器目录（回车使用 dist/client-package）：", "dist/client-package")
                    preview_then_run(["publish-updater", "--package", package])
                elif choice == "7":
                    if confirm("即将回滚远端服务端，是否继续？"):
                        execute(["rollback-server"])
                elif choice == "8":
                    preview_then_run(["rebuild-sources"])
                elif choice == "9":
                    path = ask("新私钥保存路径（回车使用 ~/.mcmodsync/private.key）：", "~/.mcmodsync/private.key")
                    execute(["keygen", "--key-file", path])
                    write("生成成功后，请将公钥填入配置文件的 client.publicKey。")
                elif choice == "10":
                    config = ask("配置文件路径（回车保持当前）：", config)
                else:
                    write("无效选择，请输入菜单编号。")
            except KeyboardInterrupt:
                write("\n已中断当前操作。请检查上方结果后继续。")
            except EOFError:
                raise
            except Exception:
                # Keep credentials out of generic exception messages. Commands
                # already provide their own operation-specific diagnostics.
                write("操作异常，未能确认完成。请先检查日志和配置，再重试。")
    except (EOFError, KeyboardInterrupt):
        write("\n已退出 OP 菜单。")
        return 0
