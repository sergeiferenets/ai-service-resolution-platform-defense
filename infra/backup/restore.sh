#!/usr/bin/env bash
# =============================================================================
# Восстановление базы состояния и аудита из выгрузки (ADR-0007: время
# восстановления — 2 часа).
#
#   sudo bash infra/backup/restore.sh <файл.dump>            — в рабочую базу
#   sudo bash infra/backup/restore.sh <файл.dump> <база>     — в отдельную базу, для проверки
#
# Контрольная сумма сверяется до восстановления. База пересоздаётся целиком:
# данные выгрузки не смешиваются с текущими. На время восстановления рабочей
# базы api останавливается, чтобы в неё никто не писал.
# =============================================================================
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
# shellcheck disable=SC1091
. "$INFRA_DIR/.env"
set +a

dump="${1:?укажите файл выгрузки}"
target="${2:-$POSTGRES_DB}"
[[ -f "$dump" ]] || { echo "Нет файла $dump" >&2; exit 1; }

if [[ -f "$dump.sha256" ]]; then
  (cd "$(dirname "$dump")" && sha256sum --check --quiet "$(basename "$dump").sha256")
else
  echo "ВНИМАНИЕ: рядом с выгрузкой нет контрольной суммы, целостность не проверена." >&2
fi

sql() { docker exec -i postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -q -c "$1"; }

api_stopped=0
if [[ "$target" == "$POSTGRES_DB" ]] && docker ps --format '{{.Names}}' | grep -qx api; then
  docker stop api > /dev/null
  api_stopped=1
fi

sql "DROP DATABASE IF EXISTS \"$target\" WITH (FORCE)"
sql "CREATE DATABASE \"$target\" OWNER \"$POSTGRES_USER\""
docker exec -i postgres pg_restore -U "$POSTGRES_USER" -d "$target" --no-owner --exit-on-error < "$dump"

if (( api_stopped )); then
  docker start api > /dev/null
fi
echo "Восстановлено: база $target из $dump"
