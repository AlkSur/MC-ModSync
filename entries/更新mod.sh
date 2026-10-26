#!/bin/sh
cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
UPDATES=0
while :; do
  if [ -x "$DIR/_updater/mcmodsync-linux" ]; then
    "$DIR/_updater/mcmodsync-linux" sync --target "$DIR" "$@"
  else
    python3 "$DIR/_updater/mcmodsync.py" sync --target "$DIR" "$@"
  fi
  RC=$?
  [ "$RC" = "42" ] || break
  UPDATES=$((UPDATES + 1))
  if [ "$UPDATES" -gt 2 ]; then
    echo "更新器反复请求重启，已停止，请联系服主。"
    RC=1
    break
  fi
  if [ -x "$DIR/_updater/mcmodsync-linux" ]; then
    "$DIR/_updater/self-update/mcmodsync-linux" apply-update --target "$DIR" --no-pause
  else
    python3 "$DIR/_updater/self-update/mcmodsync.py" apply-update --target "$DIR" --no-pause
  fi
  RC=$?
  [ "$RC" = "0" ] || break
done
echo ""
if [ "$RC" = "0" ]; then echo "=== 更新完成，请手动启动 PCL2 或 HMCL ==="; else echo "=== 更新失败，退出码 $RC，日志见 _updater/logs/ ==="; fi
MODSYNC_PAUSE=1
for MODSYNC_ARG in "$@"; do
  if [ "$MODSYNC_ARG" = "--no-pause" ]; then
    MODSYNC_PAUSE=0
    break
  fi
done
if [ "$MODSYNC_PAUSE" = "1" ]; then
  printf '%s' '按回车退出...'
  IFS= read -r MODSYNC_REPLY || :
fi
exit $RC
