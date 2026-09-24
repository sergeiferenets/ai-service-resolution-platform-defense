#!/usr/bin/env bash
# =============================================================================
# Выгрузка базы состояния и аудита (ADR-0007, «Цели восстановления»).
#
# Хранилище состояния — единственное, что невосстановимо иным способом:
# аудиторский след и ход разбора обращений. Остальное воспроизводится из
# репозитория: мок — повторным заполнением, справочники платформы — загрузчиком.
# Допустимая потеря — 24 часа, поэтому выгрузка ежедневная: таймер systemd
# service-desk-ai-backup.timer ставит bootstrap.sh.
#
# Выгрузка пишется в BACKUP_DIR — отдельный сетевой диск, переживающий
# пересоздание узла. Файл публикуется только после проверки: оглавление
# читается pg_restore, контрольная сумма записывается рядом.
#
#   sudo bash infra/backup/backup.sh
# =============================================================================
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
# shellcheck disable=SC1091
. "$INFRA_DIR/.env"
set +a

DIR="${BACKUP_DIR:-/var/backups/service-desk-ai}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
install -d -m 700 "$DIR"

if [[ "$(findmnt -no SOURCE --target "$DIR")" == "$(findmnt -no SOURCE /)" ]]; then
  echo "ВНИМАНИЕ: $DIR на системном диске — выгрузка не переживёт пересоздание узла (ADR-0007)." >&2
fi

name="servicedesk-$(date -u +%Y%m%dT%H%M%SZ).dump"
part="$DIR/.${name}.part"
trap 'rm -f "$part"' EXIT

docker exec postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6 > "$part"
# Выгрузка, которую нельзя прочитать, хуже отсутствия выгрузки: она создаёт
# ложную уверенность. Проверяется до публикации.
docker exec -i postgres pg_restore --list < "$part" > /dev/null

mv "$part" "$DIR/$name"
trap - EXIT
(cd "$DIR" && sha256sum "$name" > "$name.sha256")

find "$DIR" -maxdepth 1 -name 'servicedesk-*.dump*' -mtime "+${KEEP_DAYS}" -delete
printf 'Выгрузка готова: %s (%s), хранится %s дн.\n' "$DIR/$name" "$(du -h "$DIR/$name" | cut -f1)" "$KEEP_DAYS"
