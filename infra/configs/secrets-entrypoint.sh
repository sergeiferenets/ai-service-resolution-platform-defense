#!/bin/sh
# =============================================================================
# Обёртка точки входа для образов, которые не умеют читать секрет из файла
# (vLLM, Qdrant, Langfuse). Переводит Docker secrets из /run/secrets
# в переменные окружения процесса и передаёт управление исходной команде.
#
# Секрет при этом не попадает ни в infra/.env, ни в описание контейнера
# (docker inspect), ни в аргументы командной строки.
# docs/security/security.md, раздел 5.
#
# SECRETS_TO_ENV — пары ПЕРЕМЕННАЯ=имя_секрета через пробел или перевод строки.
# Исходные точку входа и команду образа сервис передаёт в command.
# =============================================================================
set -eu
set -f

for pair in ${SECRETS_TO_ENV:-}; do
  var="${pair%%=*}"
  name="${pair#*=}"
  file="/run/secrets/${name}"
  if [ ! -r "$file" ]; then
    echo "secrets-entrypoint: секрет ${name} не смонтирован (${file})" >&2
    exit 1
  fi
  value="$(cat "$file")"
  export "${var}=${value}"
done
unset SECRETS_TO_ENV pair var name file value
set +f

exec "$@"
