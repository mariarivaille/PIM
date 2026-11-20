#!/bin/sh
# Ежедневная копия базы (консистентная, через sqlite .backup); на сервере хранится 14 дней.
# Если в /opt/pim/app/.env задан BACKUP_CHAT_ID (свой Telegram id), копия ещё и уходит боту в этот чат файлом —
# так база переживёт удаление сервера. Ставится в /usr/local/bin/pim-backup, запускается cron от root.
set -e
DB=/opt/pim/app/pim.db
ENV=/opt/pim/app/.env
DIR=/var/backups/pim

mkdir -p "$DIR"
[ -f "$DB" ] || exit 0
OUT="$DIR/pim-$(date +%F).db"
sqlite3 "$DB" ".backup '$OUT'"
find "$DIR" -name 'pim-*.db' -mtime +14 -delete

env_value() { grep -E "^$1=" "$ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' \r"; }
TOKEN=$(env_value TELEGRAM_BOT_TOKEN)
CHAT=$(env_value BACKUP_CHAT_ID)
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || exit 0

GZ=$(mktemp --suffix=.db.gz)
trap 'rm -f "$GZ"' EXIT
gzip -c "$OUT" > "$GZ"
# адрес с токеном — через конфиг на stdin, чтобы токен не светился в списке процессов
printf 'url = "https://api.telegram.org/bot%s/sendDocument"\n' "$TOKEN" |
  curl -sS -K - -o /dev/null --fail --max-time 120 \
    -F chat_id="$CHAT" -F disable_notification=true \
    -F caption="Копия базы PIM за $(date +%d.%m.%Y)" \
    -F document=@"$GZ;filename=pim-$(date +%F).db.gz"
