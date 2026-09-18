#!/bin/sh
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
if [ -x "$DIR/_updater/mcmodsync-linux" ]; then
  "$DIR/_updater/mcmodsync-linux" sync --target "$DIR" "$@"
else
  python3 "$DIR/_updater/mcmodsync.py" sync --target "$DIR" "$@"
fi
RC=$?
echo ""
if [ "$RC" = "0" ]; then echo "=== 更新完成，请手动启动 PCL2 或 HMCL ==="; else echo "=== 更新失败，退出码 $RC，日志见 _updater/logs/ ==="; fi
echo "$@" | grep -q -- "--no-pause" || read -r -p "按回车退出..."
exit $RC
