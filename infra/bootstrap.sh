#!/usr/bin/env bash
# =============================================================================
# Развёртывание MVP с нуля на чистой Ubuntu 22.04 с картой NVIDIA.
#
# Целевая машина: Selectel, RTX 6000 Ada 48 ГБ, 12 vCPU, 64 ГБ RAM, диск 200 ГБ.
#
# Сервер прерываемый: может быть остановлен в любой момент. Поэтому скрипт
# идемпотентен — каждый шаг сначала проверяет, сделан ли он, и повторный
# запуск на настроенной машине ничего не ломает и не пересоздаёт.
# Ручных шагов нет: прерываемый сервер должен полностью восстанавливаться из кода.
#
#   sudo bash infra/bootstrap.sh            полный проход
#   sudo bash infra/bootstrap.sh --boot     только поднять стек (для systemd)
#   sudo bash infra/bootstrap.sh --check    диагностика без изменений
#
# Комментарии «ПРОВЕРИТЬ НА МАШИНЕ» отмечают места, которые невозможно
# проверить без реального сервера с GPU.
# =============================================================================

set -Eeuo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$INFRA_DIR")"
ENV_FILE="$INFRA_DIR/.env"
ENV_EXAMPLE="$INFRA_DIR/.env.example"
SECRETS_DIR="$INFRA_DIR/secrets"
COMPOSE_FILE="$INFRA_DIR/docker-compose.yml"
STATE_FILE="$INFRA_DIR/.bootstrap-state"
SYSTEMD_UNIT="/etc/systemd/system/service-desk-ai.service"

MODE="full"
case "${1:-}" in
  --boot)  MODE="boot" ;;
  --check) MODE="check" ;;
  "")      MODE="full" ;;
  *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
esac

# Ветка драйвера. Если драйвер уже стоит, установка пропускается.
# Проверено на машине (Selectel, Ubuntu 22.04, 14.09.2026): образ пришёл
# без драйвера, а серверные пакеты веток 550 и 570 в Ubuntu — переходные
# на 580-server. Поэтому сразу 580: для Ada Lovelace нужна ветка не ниже 535,
# а драйвер обратно совместим с CUDA 12.4 в образе vLLM.
NVIDIA_DRIVER_BRANCH="${NVIDIA_DRIVER_BRANCH:-580}"

# -----------------------------------------------------------------------------
# Журналирование
# -----------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

log()  { printf '%s[%s]%s %s\n' "$C_DIM" "$(date +%H:%M:%S)" "$C_OFF" "$*"; }
ok()   { printf '%s  ✓%s %s\n' "$C_OK" "$C_OFF" "$*"; }
skip() { printf '%s  ·%s %s %s(уже сделано)%s\n' "$C_DIM" "$C_OFF" "$*" "$C_DIM" "$C_OFF"; }
warn() { printf '%s  !%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%s  ✗%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

step() { printf '\n%s──%s %s\n' "$C_DIM" "$C_OFF" "$*"; }

on_error() {
  local code=$? line=${BASH_LINENO[0]}
  printf '\n%s✗ Прервано на строке %s, код %s%s\n' "$C_ERR" "$line" "$code" "$C_OFF" >&2
  printf 'Диагностика: sudo bash %s --check\n' "${BASH_SOURCE[0]}" >&2
  exit "$code"
}
trap on_error ERR

# -----------------------------------------------------------------------------
# Права
# -----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "Нужны права root, sudo не найден."
  log "Требуются права root, перезапуск через sudo"
  exec sudo -E bash "${BASH_SOURCE[0]}" "$@"
fi

# =============================================================================
# 1. Предварительные проверки
# =============================================================================
preflight() {
  step "Предварительные проверки"

  local id ver
  id="$(. /etc/os-release && echo "${ID:-}")"
  ver="$(. /etc/os-release && echo "${VERSION_ID:-}")"
  if [[ "$id" != "ubuntu" || "$ver" != "22.04" ]]; then
    warn "Ожидалась Ubuntu 22.04, обнаружено: ${id:-?} ${ver:-?}. Продолжаю, но шаги с репозиториями могут не подойти."
  else
    ok "Ubuntu 22.04"
  fi

  # Карта должна быть видна на шине даже до установки драйвера.
  if command -v lspci >/dev/null 2>&1 && lspci | grep -qi 'nvidia'; then
    ok "Карта NVIDIA видна на шине PCI"
  else
    warn "lspci не находит карту NVIDIA. Если это так, GPU-часть стека не поднимется."
  fi

  # Веса моделей ~15 ГБ, образ vLLM ~16 ГБ, тома хранилищ. С запасом — 60 ГБ.
  local free_gb
  free_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
  if (( free_gb < 60 )); then
    die "На / свободно ${free_gb} ГБ. Нужно не менее 60 ГБ: веса моделей, образы, тома."
  fi
  ok "Свободно на диске: ${free_gb} ГБ"

  local ram_gb
  ram_gb="$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)"
  if (( ram_gb < 24 )); then
    warn "Оперативной памяти ${ram_gb} ГБ. Профиль observability (ClickHouse) может не поместиться."
  fi
  ok "Оперативная память: ${ram_gb} ГБ"
}

# =============================================================================
# 2. Системные пакеты
# =============================================================================
APT_UPDATED=0
apt_update_once() {
  if (( APT_UPDATED )); then return 0; fi
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  APT_UPDATED=1
}

install_base_packages() {
  step "Базовые пакеты"
  local want=(ca-certificates curl gnupg lsb-release jq pciutils openssl)
  local missing=()
  for p in "${want[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  if (( ${#missing[@]} == 0 )); then
    skip "Базовые пакеты"
    return 0
  fi
  apt_update_once
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  ok "Установлены: ${missing[*]}"
}

# Ядро не обновляется автоматически. Обновление ядра требует перезагрузки и
# простоя, а на прерываемом узле со сроком жизни в дни пользы не даёт: 14.09
# автообновление поставило новое ядро, и после перезагрузки 15.09 узел остался
# без модуля NVIDIA. Остальные пакеты обновляются как прежде; ядро — вручную.
KERNEL_HOLD_FILE=/etc/apt/apt.conf.d/51service-desk-no-kernel-autoupgrade
disable_kernel_autoupgrade() {
  step "Автообновление ядра"
  if [[ -f "$KERNEL_HOLD_FILE" ]]; then
    skip "Ядро исключено из автоматических обновлений"
    return 0
  fi
  cat > "$KERNEL_HOLD_FILE" <<'EOF'
// service-desk-ai: ядро не обновляется автоматически. Обновление ядра требует
// перезагрузки и простоя, а на узле со сроком жизни в дни пользы не даёт.
// Обновление ядра — только вручную (apt-get install linux-image-generic).
Unattended-Upgrade::Package-Blacklist {
    "^linux-image";
    "^linux-headers";
    "^linux-modules";
    "^linux-generic";
};
EOF
  ok "Ядро исключено из автоматических обновлений"
}

# =============================================================================
# 3. Драйвер NVIDIA
# =============================================================================
# Единственный шаг, который может потребовать перезагрузки. Обрабатывается
# явно: без вмешательства оператора на прерываемой машине рассчитывать нельзя.
install_nvidia_driver() {
  step "Драйвер NVIDIA"

  if nvidia-smi >/dev/null 2>&1; then
    local drv
    drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
    skip "Драйвер NVIDIA (версия ${drv})"
    return 0
  fi

  log "Драйвер не отвечает, устанавливаю ветку ${NVIDIA_DRIVER_BRANCH}"

  if [[ ! -f /etc/apt/sources.list.d/cuda-ubuntu2204-x86_64.list ]]; then
    local deb="/tmp/cuda-keyring.deb"
    curl -fsSL -o "$deb" \
      https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
    DEBIAN_FRONTEND=noninteractive dpkg -i "$deb" >/dev/null
    rm -f "$deb"
    APT_UPDATED=0
  fi
  apt_update_once

  # Заголовки ядра обязательны: модуль драйвера собирается через DKMS. Без
  # них пакет драйвера ставится, а модуля нет — так было при первом запуске
  # на узле Selectel (14.09.2026). Мета-пакет linux-headers-generic нужен
  # сверх заголовков работающего ядра: он ставит заголовки к каждому новому
  # ядру, и DKMS пересобирает модуль сам. Без него обновление ядра 14.09
  # оставило узел без модуля после перезагрузки (15.09.2026).
  local kver
  kver="$(uname -r)"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "linux-headers-${kver}" linux-headers-generic

  # Для арендованного узла без графической сессии подходит серверный вариант.
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    "nvidia-driver-${NVIDIA_DRIVER_BRANCH}-server" || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    "nvidia-driver-${NVIDIA_DRIVER_BRANCH}"

  # Если драйвер был поставлен раньше заголовков, DKMS модуль не собрал —
  # собираем явно под работающее ядро.
  if ! modinfo nvidia >/dev/null 2>&1; then
    log "Модуль nvidia не собран, запускаю сборку DKMS под ядро ${kver}"
    dkms autoinstall -k "$kver" || true
  fi

  # Попытка поднять модуль без перезагрузки: часто срабатывает на чистой
  # машине, где старый модуль ещё не был загружен.
  modprobe nvidia 2>/dev/null || true

  if nvidia-smi >/dev/null 2>&1; then
    ok "Драйвер установлен и загружен без перезагрузки"
    return 0
  fi

  echo "driver_installed_awaiting_reboot" > "$STATE_FILE"
  install_systemd_unit

  # infra/.env на этом шаге ещё не загружен: значение берётся из окружения,
  # а при повторном запуске — напрямую из infra/.env.
  local auto_reboot="${AUTO_REBOOT:-}"
  if [[ -z "$auto_reboot" && -f "$ENV_FILE" ]]; then
    auto_reboot="$(grep -m1 '^AUTO_REBOOT=' "$ENV_FILE" | cut -d= -f2- || true)"
  fi

  if [[ "${auto_reboot:-0}" == "1" ]]; then
    warn "Драйвер установлен, требуется перезагрузка. AUTO_REBOOT=1 — перезагружаюсь."
    warn "После перезагрузки systemd-юнит service-desk-ai продолжит развёртывание."
    sync
    systemctl reboot
    exit 0
  fi

  die "Драйвер установлен, но модуль не загружен — нужна перезагрузка.
     Выполните: sudo reboot
     После перезагрузки развёртывание продолжится автоматически (юнит service-desk-ai),
     либо запустите скрипт повторно.
     Чтобы перезагрузка выполнялась сама: sudo AUTO_REBOOT=1 bash infra/bootstrap.sh
     (при повторном запуске действует и AUTO_REBOOT=1 в infra/.env)."
}

# =============================================================================
# 4. Docker
# =============================================================================
install_docker() {
  step "Docker и плагин compose"

  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    skip "Docker $(docker --version | awk '{print $3}' | tr -d ,), compose $(docker compose version --short)"
  else
    if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
    fi
    if [[ ! -f /etc/apt/sources.list.d/docker.list ]]; then
      local arch codename
      arch="$(dpkg --print-architecture)"
      codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
      echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
        > /etc/apt/sources.list.d/docker.list
      APT_UPDATED=0
    fi
    apt_update_once
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ok "Docker установлен"
  fi

  systemctl is-enabled docker >/dev/null 2>&1 || systemctl enable docker >/dev/null 2>&1
  systemctl is-active  docker >/dev/null 2>&1 || systemctl start  docker

  # Ротация логов на уровне демона: прерываемая машина живёт долго,
  # а место на диске делят веса моделей и тома хранилищ.
  local daemon=/etc/docker/daemon.json
  if [[ ! -f "$daemon" ]]; then
    mkdir -p /etc/docker
    cat > "$daemon" <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" },
  "live-restore": true
}
JSON
    systemctl restart docker
    ok "Настроены ротация логов и live-restore"
  else
    skip "Конфигурация демона Docker"
  fi
}

# =============================================================================
# 5. NVIDIA Container Toolkit
# =============================================================================
install_container_toolkit() {
  step "NVIDIA Container Toolkit"

  if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
    if [[ ! -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ]]; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    fi
    if [[ ! -f /etc/apt/sources.list.d/nvidia-container-toolkit.list ]]; then
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
      APT_UPDATED=0
    fi
    apt_update_once
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-container-toolkit
    ok "Пакет установлен"
  else
    skip "Пакет nvidia-container-toolkit"
  fi

  # nvidia-ctk идемпотентен: повторный вызов не дублирует запись в daemon.json.
  if ! grep -q '"nvidia"' /etc/docker/daemon.json 2>/dev/null; then
    nvidia-ctk runtime configure --runtime=docker >/dev/null
    systemctl restart docker
    ok "Среда выполнения nvidia зарегистрирована в Docker"
  else
    skip "Среда выполнения nvidia в Docker"
  fi
}

# =============================================================================
# 6. Проверка доступа к GPU из контейнера
# =============================================================================
# Ключевая проверка всего блока установки: драйвер, toolkit и Docker
# работают вместе. Без неё стек падает уже на старте vLLM.
verify_gpu_in_container() {
  step "Доступ к GPU из контейнера"

  # ПРОВЕРИТЬ НА МАШИНЕ: тег базового образа CUDA. Должен быть не новее
  # версии, поддерживаемой установленным драйвером.
  local img="nvidia/cuda:12.4.1-base-ubuntu22.04"
  local out
  if ! out="$(docker run --rm --gpus all "$img" nvidia-smi 2>&1)"; then
    printf '%s\n' "$out" >&2
    die "Контейнер не видит GPU. Проверьте: nvidia-smi на хосте, наличие
     runtime nvidia в /etc/docker/daemon.json, systemctl status docker."
  fi

  local info name mem
  info="$(docker run --rm --gpus all "$img"     nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
  name="${info%%,*}"
  mem="$(printf '%s' "${info#*,}" | sed 's/^ *//')"
  ok "GPU доступна из контейнера: ${name}, ${mem}"

  # Бюджет видеопамяти по ADR-0005 рассчитан на 48 ГБ. На меньшей карте
  # доли из .env приведут к переподписке — лучше сказать об этом сразу.
  local mib
  mib="$(printf '%s' "$mem" | tr -dc '0-9')"
  if (( mib < 40000 )); then
    warn "Видеопамяти ${mem}. Бюджет в .env рассчитан на 48 ГБ (ADR-0005):
     LLM_GPU_FRACTION + VISION_GPU_FRACTION + EMBED_GPU_FRACTION нужно пересчитать."
  fi
}

# =============================================================================
# 7. Конфигурация и секреты
# =============================================================================
# infra/.env — только несекретная конфигурация: адреса, порты, имена моделей,
# параметры поиска. Секреты — отдельные файлы в infra/secrets/, в контейнеры
# они попадают механизмом Docker secrets (docs/security/security.md, раздел 5).
#
# Секреты генерируются один раз. При повторном запуске существующие значения
# сохраняются: перевыпуск рассогласовал бы их с уже созданными томами хранилищ.
gen_secret()  { openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32; }
gen_hex64()   { openssl rand -hex 32; }

# Файл в infra/secrets : переменная прежнего .env : способ генерации.
# Переменная нужна для перехода: на уже развёрнутом узле значение переносится
# из старого .env, иначе новый пароль не совпал бы с существующим томом.
SECRET_SPECS=(
  "vllm_api_key:VLLM_API_KEY:alnum"
  "postgres_password:POSTGRES_PASSWORD:alnum"
  "qdrant_api_key:QDRANT_API_KEY:alnum"
  "mock_erp_token:MOCK_ERP_TOKEN:alnum"
  "langfuse_postgres_password:LANGFUSE_POSTGRES_PASSWORD:alnum"
  "langfuse_salt:LANGFUSE_SALT:alnum"
  "langfuse_nextauth_secret:LANGFUSE_NEXTAUTH_SECRET:alnum"
  "langfuse_encryption_key:LANGFUSE_ENCRYPTION_KEY:hex64"
  "clickhouse_password:CLICKHOUSE_PASSWORD:alnum"
  "redis_password:REDIS_PASSWORD:alnum"
  "minio_root_password:MINIO_ROOT_PASSWORD:alnum"
  "grafana_admin_password:GRAFANA_ADMIN_PASSWORD:alnum"
  # Необязательный: нужен только для закрытых весов и не генерируется.
  "hf_token:HF_TOKEN:optional"
)

materialize_env() {
  step "Конфигурация infra/.env"

  [[ -f "$ENV_EXAMPLE" ]] || die "Не найден $ENV_EXAMPLE"
  if grep -qE '^[A-Z_][A-Z0-9_]*=GENERATE' "$ENV_EXAMPLE"; then
    die "В $ENV_EXAMPLE остались значения GENERATE. Секреты задаются только в infra/secrets/."
  fi

  if [[ ! -f "$ENV_FILE" ]]; then
    : > "$ENV_FILE"
    log "Создаю .env из шаблона"
  fi
  chmod 600 "$ENV_FILE"

  local added=0 line key
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ ! "$line" =~ ^[A-Z_][A-Z0-9_]*= ]]; then
      continue
    fi
    key="${line%%=*}"
    # Ключ уже есть — не трогаем: локальные значения переживают повторный запуск.
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
      continue
    fi
    printf '%s\n' "$line" >> "$ENV_FILE"
    added=$((added + 1))
  done < "$ENV_EXAMPLE"

  if (( added > 0 )); then
    ok "Записано переменных: ${added}"
  else
    skip "Файл .env полный"
  fi
}

materialize_secrets() {
  step "Секреты infra/secrets"

  install -d -m 700 "$SECRETS_DIR"

  local spec name var kind file val created=0 migrated=0 removed=0
  for spec in "${SECRET_SPECS[@]}"; do
    IFS=: read -r name var kind <<< "$spec"
    file="$SECRETS_DIR/$name"
    if [[ -s "$file" ]]; then
      continue
    fi
    # Переход с прежней схемы: значение из старого .env переносится как есть.
    val="$(grep -m1 "^${var}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
    if [[ -n "$val" && "$val" != GENERATE* ]]; then
      migrated=$((migrated + 1))
    else
      case "$kind" in
        alnum)    val="$(gen_secret)" ;;
        hex64)    val="$(gen_hex64)" ;;
        optional) continue ;;
      esac
      created=$((created + 1))
    fi
    printf '%s' "$val" > "$file"
  done

  # Секреты удаляются из прежнего .env только после переноса.
  for spec in "${SECRET_SPECS[@]}"; do
    IFS=: read -r name var kind <<< "$spec"
    if grep -q "^${var}=" "$ENV_FILE" 2>/dev/null; then
      sed -i "/^${var}=/d" "$ENV_FILE"
      removed=$((removed + 1))
    fi
  done

  # Производный файл: конфигурация Redis с паролем. Пароль в аргументах
  # redis-server был бы виден в списке процессов.
  local conf
  conf="$(printf 'requirepass %s\nmaxmemory-policy noeviction' "$(<"$SECRETS_DIR/redis_password")")"
  if [[ ! -f "$SECRETS_DIR/redis_conf" || "$(<"$SECRETS_DIR/redis_conf")" != "$conf" ]]; then
    printf '%s\n' "$conf" > "$SECRETS_DIR/redis_conf"
  fi

  # Каталог закрыт для всех, кроме root. Файлы внутри доступны на чтение:
  # Docker Compose вне Swarm монтирует секрет как файл хоста с его правами,
  # а процессы части образов (Grafana, Redis, ClickHouse, Langfuse) работают
  # не от root. С хоста файлы недоступны никому, кроме root: их закрывает каталог.
  # ПРОВЕРИТЬ НА МАШИНЕ: чтение секретов непривилегированными процессами.
  chmod 700 "$SECRETS_DIR"
  find "$SECRETS_DIR" -type f -exec chmod 644 {} +

  if (( migrated > 0 )); then ok "Перенесено из прежнего .env: ${migrated}"; fi
  if (( removed > 0 )); then ok "Удалено секретов из .env: ${removed}"; fi
  if (( created > 0 )); then
    ok "Сгенерировано секретов: ${created}"
  else
    skip "Секреты"
  fi
}

load_env() {
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  : "${MODELS_DIR:?MODELS_DIR не задан в .env}"
  : "${VLLM_IMAGE:?VLLM_IMAGE не задан в .env}"
}

# Значение секрета для обращений с хоста. В аргументы командной строки
# не подставляется: заголовки передаются curl через файловый дескриптор.
secret() {
  local f="$SECRETS_DIR/$1"
  [[ -s "$f" ]] || die "Нет секрета ${f}. Нужен полный проход bootstrap.sh."
  cat "$f"
}

# =============================================================================
# 8. Ограничение исходящих соединений
# =============================================================================
# ADR-0001, «Проверка соблюдения»: сетевые правила узла не допускают
# исходящих соединений к внешним сервисам генеративного AI.
#
# Реализовано чёрной дырой в /etc/hosts. Это защита от случайного вызова
# (забытый SDK, пример из документации, зависимость библиотеки), а не
# граница безопасности против намеренного обхода: полноценное ограничение
# исходящего трафика делается правилами группы безопасности на стороне
# облака и здесь заведомо неполно.
EGRESS_HOSTS=(
  api.openai.com
  api.anthropic.com
  generativelanguage.googleapis.com
  api.cohere.ai
  api.mistral.ai
  api.groq.com
  api.deepseek.com
  openrouter.ai
  api.together.xyz
)

apply_egress_guard() {
  step "Ограничение исходящих соединений к внешним сервисам AI"

  if [[ "${ENABLE_EGRESS_GUARD:-1}" != "1" ]]; then
    warn "ENABLE_EGRESS_GUARD=0 — правило не применяется. ADR-0001 требует его наличия."
    return 0
  fi

  local begin="# >>> service-desk-ai: ADR-0001 egress guard >>>"
  local end="# <<< service-desk-ai: ADR-0001 egress guard <<<"

  if grep -qF "$begin" /etc/hosts; then
    skip "Список заблокированных адресов в /etc/hosts"
    return 0
  fi

  {
    printf '\n%s\n' "$begin"
    printf '# Локальный контур: обращения к внешним сервисам генеративного AI запрещены.\n'
    for h in "${EGRESS_HOSTS[@]}"; do
      printf '0.0.0.0 %s\n' "$h"
    done
    printf '%s\n' "$end"
  } >> /etc/hosts

  ok "Заблокировано адресов: ${#EGRESS_HOSTS[@]}"
}

# =============================================================================
# 9. Веса моделей
# =============================================================================
# Лежат на хосте в MODELS_DIR и монтируются в контейнеры. Предзагрузка до
# старта стека делает первый запуск наблюдаемым: иначе vLLM молча качает
# 15 ГБ и healthcheck выглядит как зависание.
hf_cache_path() {
  # Qwen/Qwen3-8B-AWQ -> models--Qwen--Qwen3-8B-AWQ
  printf '%s/hf/hub/models--%s' "$MODELS_DIR" "${1//\//--}"
}

prefetch_model() {
  local repo="$1" revision="$2" label="$3"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die "${label}: требуется immutable MODEL_REVISION (40 hex)"
  # snapshot_download сам проверяет кэш именно этой ревизии и дозагружает
  # недостающие файлы; наличие каталога модели ещё не означает полную загрузку.
  log "Скачиваю ${label}: ${repo}"
  # HF_ENDPOINT не передаётся пустым: пустую переменную huggingface_hub
  # принимает за адрес без схемы и падает (проверено на машине 14.09.2026).
  local hf_token=""
  local env_args=(-e HF_HOME=/models/hf -e "HF_ENDPOINT=${HF_ENDPOINT:-https://huggingface.co}")
  # Токен — необязательный секрет infra/secrets/hf_token. Передаётся через
  # окружение процесса docker (-e HF_TOKEN без значения), а не аргументом.
  if [[ -s "$SECRETS_DIR/hf_token" ]]; then
    hf_token="$(<"$SECRETS_DIR/hf_token")"
    env_args+=(-e HF_TOKEN)
  fi
  # Проверено на машине (vLLM v0.9.2): в образе есть huggingface-cli,
  # интерпретатор — только python3. В новых версиях huggingface_hub утилита
  # переименована в `hf`; тогда сработает резервный вызов через python3.
  HF_TOKEN="$hf_token" docker run --rm \
    -v "${MODELS_DIR}:/models" "${env_args[@]}" \
    --entrypoint huggingface-cli \
    "$VLLM_IMAGE" download "$repo" --revision "$revision" \
    || HF_TOKEN="$hf_token" docker run --rm \
      -v "${MODELS_DIR}:/models" "${env_args[@]}" \
      --entrypoint python3 \
      "$VLLM_IMAGE" -c 'import sys; from huggingface_hub import snapshot_download; snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2])' "$repo" "$revision"
  ok "${label} загружена"
}

prepare_models() {
  step "Веса моделей"

  mkdir -p "${MODELS_DIR}/hf"
  ok "Каталог весов: ${MODELS_DIR}"

  local revision_name
  for revision_name in LLM_MODEL_REVISION VISION_MODEL_REVISION EMBED_MODEL_REVISION; do
    [[ "${!revision_name:-}" =~ ^[0-9a-f]{40}$ ]] || die "${revision_name}: требуется immutable Hugging Face commit SHA (40 hex)"
  done

  if [[ "${PREFETCH_MODELS:-1}" != "1" ]]; then
    warn "PREFETCH_MODELS=0 — веса скачает сам vLLM при первом запуске."
    return 0
  fi

  prefetch_model "$LLM_MODEL_ID" "${LLM_MODEL_REVISION:-}" "языковая модель"
  prefetch_model "$VISION_MODEL_ID" "${VISION_MODEL_REVISION:-}" "модель обработки изображений"
  prefetch_model "$EMBED_MODEL_ID" "${EMBED_MODEL_REVISION:-}" "модель векторных представлений"

  local size
  size="$(du -sh "${MODELS_DIR}/hf" 2>/dev/null | awk '{print $1}')"
  ok "Объём весов на диске: ${size:-?}"
}

# =============================================================================
# 10. Запуск стека
# =============================================================================
dc() {
  docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    --project-directory "$INFRA_DIR" \
    "$@"
}

wait_healthy() {
  local name="$1" timeout="${2:-1200}" waited=0 status
  while (( waited < timeout )); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohealth{{end}}' "$name" 2>/dev/null || echo missing)"
    case "$status" in
      healthy)  ok "${name}: готов (${waited} с)"; return 0 ;;
      nohealth) ok "${name}: запущен (проверка состояния не задана)"; return 0 ;;
      missing)  sleep 5; waited=$((waited + 5)); continue ;;
      unhealthy)
        docker logs --tail 40 "$name" >&2 || true
        die "${name}: состояние unhealthy. Журнал выше."
        ;;
    esac
    sleep 10
    waited=$((waited + 10))
    if (( waited % 60 == 0 )); then log "  ${name}: ${status}, ждём... (${waited} с)"; fi
  done
  docker logs --tail 40 "$name" >&2 || true
  die "${name}: не готов за ${timeout} с."
}

start_core() {
  step "Профиль core: инференс и хранилища"

  dc --profile core up -d

  # Порядок ожидания повторяет порядок старта: экземпляры vLLM поднимаются
  # последовательно, чтобы каждый корректно замерил свободную видеопамять.
  wait_healthy postgres 180
  wait_healthy vllm-llm 1800
  wait_healthy vllm-vision 1800
  wait_healthy vllm-embeddings 1200

  # У qdrant нет healthcheck (в образе нет curl/wget) — проверяем с хоста.
  local waited=0
  until qdrant_api "http://127.0.0.1:${QDRANT_HTTP_PORT}/readyz" >/dev/null 2>&1; do
    if (( waited >= 120 )); then die "qdrant не отвечает на /readyz за 120 с."; fi
    sleep 5; waited=$((waited + 5))
  done
  ok "qdrant: готов (${waited} с)"
}

# =============================================================================
# 11. Коллекция Qdrant
# =============================================================================
# Одна коллекция на обе ветви поиска (ADR-0003): плотные векторы bge-m3
# и разреженные BM25 с модификатором idf. Фильтр по правам применяется
# к запросу целиком и не может быть обойдён через одну из ветвей.
# Запрос к Qdrant с ключом доступа. Заголовок передаётся curl через файловый
# дескриптор, а не аргументом: ключ не появляется в списке процессов.
qdrant_api() {
  curl -fsS -H @<(printf 'api-key: %s\n' "$(secret qdrant_api_key)") \
    -H "Content-Type: application/json" "$@"
}

init_qdrant_collection() {
  step "Коллекция Qdrant «${QDRANT_COLLECTION}»"

  local base="http://127.0.0.1:${QDRANT_HTTP_PORT}"
  local schema="$INFRA_DIR/configs/qdrant-collection.json"

  if qdrant_api "${base}/collections/${QDRANT_COLLECTION}" >/dev/null 2>&1; then
    skip "Коллекция ${QDRANT_COLLECTION}"
    return 0
  fi

  # Размерность берётся из .env, а не из файла схемы: при смене модели
  # представлений меняется одна переменная.
  local body
  body="$(jq --argjson dim "${EMBED_DIM}" \
    '.collection | .vectors.dense.size = $dim' "$schema")"

  qdrant_api -X PUT -d "$body" \
    "${base}/collections/${QDRANT_COLLECTION}" >/dev/null
  ok "Коллекция создана (dense ${EMBED_DIM} + sparse bm25 с idf)"

  # Индексы по полям, по которым идёт фильтрация прав и области поиска.
  # Имена полей — docs/architecture/data-model.md, сущность DOCUMENT_CHUNK.
  local n=0 idx
  while read -r idx; do
    qdrant_api -X PUT -d "$idx" \
      "${base}/collections/${QDRANT_COLLECTION}/index?wait=true" >/dev/null
    n=$((n + 1))
  done < <(jq -c '.payload_indexes[]' "$schema")
  ok "Создано индексов по полям: ${n}"

  # ПРОВЕРИТЬ НА МАШИНЕ: разреженные векторы BM25 считаются на стороне
  # клиента (fastembed, модель Qdrant/bm25) с параметром language,
  # который берётся из BM25_LANGUAGE=${BM25_LANGUAGE}. Стемминг и стоп-слова
  # русского языка обязательны: без них свободные описания симптомов
  # («не печатает, полосы на листе») находятся заметно хуже.
  # Проверяется при реализации сервиса поиска, не здесь.
  log "  Язык BM25 для сервиса поиска: ${BM25_LANGUAGE}"
}

# =============================================================================
# 12. Профиль app
# =============================================================================
start_app() {
  step "Профиль app: backend и мок учётной системы"

  if [[ ! -f "$REPO_DIR/backend/Dockerfile" || ! -f "$REPO_DIR/mock-erp/Dockerfile" ]]; then
    warn "Dockerfile в backend/ или mock-erp/ ещё нет — профиль app пропущен.
     Это ожидаемо на текущем этапе: инфраструктура готова раньше кода."
    return 0
  fi

  # Профиль core указывается вместе с app: api и мок зависят от postgres из
  # core, и без него compose отвергает проект целиком («depends on undefined
  # service», проверено на машине 14.09.2026).
  dc --profile core --profile app up -d --build
  wait_healthy api 300
  wait_healthy mock-erp 300
  load_corpus
}

# Корпус знаний загружается из образа api: там и документы, и загрузчик.
# Загрузка идемпотентна — повторный запуск обновляет фрагменты, а не плодит их.
# Отказ загрузки не роняет развёртывание: стек поднят, а корпус можно
# догрузить командой из сообщения.
load_corpus() {
  step "Корпус знаний в Qdrant"
  if docker exec api python -m backend.knowledge.loader; then
    ok "Корпус загружен"
  else
    warn "Корпус не загружен. Проверьте qdrant и vllm-embeddings, затем повторите:
     docker exec api python -m backend.knowledge.loader"
  fi
}

# =============================================================================
# 13. Профиль observability
# =============================================================================
# Поднимается в фоне и после core. Langfuse — инструмент разработчика;
# аудиторский след платформы ведётся в PostgreSQL профиля core по схеме
# docs/architecture/data-model.md. Отказ этого профиля не должен
# задерживать готовность основного стека и не должен ронять развёртывание.
start_observability() {
  step "Профиль observability (в фоне)"

  local logfile="/var/log/service-desk-ai-observability.log"
  (
    if dc --profile observability up -d >>"$logfile" 2>&1; then
      echo "$(date -Is) observability: поднят" >> "$logfile"
    else
      echo "$(date -Is) observability: ОШИБКА, стек core не затронут" >> "$logfile"
    fi
  ) &
  disown || true

  ok "Запуск отправлен в фон, журнал: ${logfile}"
  log "  Langfuse будет доступен на 127.0.0.1:${LANGFUSE_PORT} через несколько минут"
}

# =============================================================================
# 14. Проверка: осмысленный ответ на русском
# =============================================================================
# Критерий готовности из задания. Проверяются три вещи сразу: эндпоинт
# отвечает, модель загружена, ответ содержит кириллицу.
smoke_test_llm() {
  step "Проверка эндпоинта vLLM"

  local payload resp content
  # enable_thinking=false обязателен: иначе Qwen3 вернёт блок <think>,
  # что ломает и время ответа, и разбор структурированного вывода.
  payload="$(jq -n --arg m "$LLM_SERVED_NAME" '{
    model: $m,
    messages: [
      { role: "system", content: "Ты помощник сервисной службы. Отвечай кратко, на русском языке." },
      { role: "user",   content: "Принтер печатает с вертикальными полосами на листе. Назови две наиболее вероятные причины." }
    ],
    temperature: 0.2,
    max_tokens: 256,
    chat_template_kwargs: { enable_thinking: false }
  }')"

  resp="$(curl -fsS --max-time 180 \
    -H @<(printf 'Authorization: Bearer %s\n' "$(secret vllm_api_key)") \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "http://127.0.0.1:${LLM_PORT}/v1/chat/completions")" \
    || die "Эндпоинт vLLM не ответил. Журнал: docker logs vllm-llm"

  content="$(printf '%s' "$resp" | jq -r '.choices[0].message.content // empty')"

  [[ -n "$content" ]] || die "Ответ пустой. Сырой ответ: $(printf '%s' "$resp" | head -c 500)"

  if ! printf '%s' "$content" | grep -qP '[\x{0400}-\x{04FF}]'; then
    warn "В ответе нет кириллицы — модель ответила не по-русски:"
    printf '%s\n' "$content" >&2
    die "Критерий готовности не выполнен: осмысленный ответ на русском не получен."
  fi

  ok "Ответ на русском получен (${#content} символов)"
  printf '%s    %s%s\n' "$C_DIM" "$(printf '%s' "$content" | head -c 300 | tr '\n' ' ')" "$C_OFF"
}

# =============================================================================
# 15. Автоматическое восстановление после вытеснения
# =============================================================================
# Сервер прерываемый. Юнит поднимает стек после перезагрузки без оператора:
# бинарники и веса уже на диске, поэтому проход занимает минуты.
install_systemd_unit() {
  # Wants, а не Requires: при перезагрузке на шаге драйвера Docker ещё
  # не установлен, и Requires на несуществующий docker.service не дал бы
  # юниту запуститься вовсе.
  local unit
  unit="$(cat <<UNIT
[Unit]
Description=Мультиагентная AI-платформа сервисных обращений (развёртывание)
Documentation=file://${REPO_DIR}/infra/README.md
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${REPO_DIR}
ExecStart=/usr/bin/env bash ${INFRA_DIR}/bootstrap.sh --boot
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
UNIT
)"

  # Юнит перезаписывается, только если его содержимое изменилось.
  if [[ -f "$SYSTEMD_UNIT" && "$(<"$SYSTEMD_UNIT")" == "$unit" ]]; then
    skip "Юнит systemd service-desk-ai"
    return 0
  fi

  printf '%s\n' "$unit" > "$SYSTEMD_UNIT"
  systemctl daemon-reload
  systemctl enable service-desk-ai.service >/dev/null 2>&1
  ok "Юнит systemd установлен: стек поднимется сам после перезагрузки"
}

# =============================================================================
# 16. Ежедневная выгрузка базы состояния (ADR-0007)
# =============================================================================
# Хранилище состояния и аудита — единственное, что не восстанавливается из
# репозитория. Выгрузка — раз в сутки на отдельный сетевой диск BACKUP_DIR.
# Persistent=true: если узел в назначенное время был выключен, выгрузка
# выполняется сразу после старта — для прерываемого узла это существенно.
# Диск не форматируется и не монтируется здесь: это разрушительная операция
# над внешним ресурсом, она выполняется один раз вручную (infra/README.md).
BACKUP_SERVICE=/etc/systemd/system/service-desk-ai-backup.service
BACKUP_TIMER=/etc/systemd/system/service-desk-ai-backup.timer
install_backup() {
  step "Ежедневная выгрузка базы состояния"

  local dir="${BACKUP_DIR:-/var/backups/service-desk-ai}"
  install -d -m 700 "$dir"
  if [[ "$(findmnt -no SOURCE --target "$dir")" == "$(findmnt -no SOURCE /)" ]]; then
    warn "Каталог выгрузки ${dir} на системном диске: выгрузка не переживёт пересоздание узла.
     Подключите отдельный сетевой диск и смонтируйте его в ${dir} (infra/README.md, «Резервное копирование»)."
  fi

  local service timer
  service="$(cat <<UNIT
[Unit]
Description=Выгрузка базы состояния и аудита (ADR-0007)
After=docker.service service-desk-ai.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash ${INFRA_DIR}/backup/backup.sh
UNIT
)"
  timer="$(cat <<UNIT
[Unit]
Description=Ежедневная выгрузка базы состояния и аудита (ADR-0007)

[Timer]
OnCalendar=*-*-* 00:30:00 UTC
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
)"

  if [[ -f "$BACKUP_SERVICE" && -f "$BACKUP_TIMER" \
        && "$(<"$BACKUP_SERVICE")" == "$service" && "$(<"$BACKUP_TIMER")" == "$timer" ]]; then
    skip "Таймер выгрузки service-desk-ai-backup"
    return 0
  fi
  printf '%s\n' "$service" > "$BACKUP_SERVICE"
  printf '%s\n' "$timer" > "$BACKUP_TIMER"
  systemctl daemon-reload
  systemctl enable --now service-desk-ai-backup.timer >/dev/null 2>&1
  ok "Таймер выгрузки установлен: ежедневно около 00:30 UTC в ${dir}"
}

# =============================================================================
# Диагностика
# =============================================================================
run_check() {
  step "Диагностика"
  printf '  ОС:              %s\n' "$(. /etc/os-release && echo "$PRETTY_NAME")"
  printf '  Драйвер NVIDIA:  %s\n' "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || echo 'не отвечает')"
  printf '  GPU:             %s\n' "$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo '—')"
  printf '  Docker:          %s\n' "$(docker --version 2>/dev/null || echo 'нет')"
  printf '  Compose:         %s\n' "$(docker compose version --short 2>/dev/null || echo 'нет')"
  printf '  Toolkit:         %s\n' "$(dpkg-query -W -f='${Version}' nvidia-container-toolkit 2>/dev/null || echo 'нет')"
  printf '  .env:            %s\n' "$([[ -f "$ENV_FILE" ]] && echo 'есть' || echo 'нет')"
  printf '  Секреты:         %s\n' "$(find "$SECRETS_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ') файлов в infra/secrets"
  printf '  Юнит systemd:    %s\n' "$([[ -f "$SYSTEMD_UNIT" ]] && echo 'установлен' || echo 'нет')"
  printf '  Выгрузка базы:   %s\n' "$(systemctl is-active service-desk-ai-backup.timer 2>/dev/null || echo 'нет таймера'), последняя: $(ls -1t "${BACKUP_DIR:-/var/backups/service-desk-ai}"/servicedesk-*.dump 2>/dev/null | head -1 || echo 'нет')"
  printf '  Egress guard:    %s\n' "$(grep -qF 'service-desk-ai: ADR-0001' /etc/hosts && echo 'применён' || echo 'нет')"
  echo
  if command -v docker >/dev/null 2>&1; then
    docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null || true
  fi
  echo
  if nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv
  fi
}

summary() {
  local host_ip
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  cat <<SUMMARY

${C_OK}Стек развёрнут.${C_OFF}

Всё слушает 127.0.0.1 — наружу не выставлено ничего (ADR-0001, закрытый периметр).
Доступ с рабочей машины через SSH-туннель:

  ssh -N -L ${LLM_PORT}:127.0.0.1:${LLM_PORT} \\
         -L ${QDRANT_HTTP_PORT}:127.0.0.1:${QDRANT_HTTP_PORT} \\
         -L ${LANGFUSE_PORT}:127.0.0.1:${LANGFUSE_PORT} \\
         -L ${GRAFANA_PORT}:127.0.0.1:${GRAFANA_PORT} \\
         root@${host_ip:-<адрес сервера>}

  Языковая модель      http://127.0.0.1:${LLM_PORT}/v1
  Обработка изображений http://127.0.0.1:${VISION_PORT}/v1
  Векторные представления http://127.0.0.1:${EMBED_PORT}/v1
  Qdrant               http://127.0.0.1:${QDRANT_HTTP_PORT}/dashboard
  Langfuse             http://127.0.0.1:${LANGFUSE_PORT}
  Grafana              http://127.0.0.1:${GRAFANA_PORT}  (admin / infra/secrets/grafana_admin_password)

Полезное:
  sudo bash infra/bootstrap.sh --check      диагностика
  docker compose -f infra/docker-compose.yml --profile core ps
  docker logs -f vllm-llm

Секреты — файлы в infra/secrets/ (каталог 700, в git не попадает).
infra/.env — только несекретная конфигурация.
После перезагрузки стек поднимется сам: systemctl status service-desk-ai

SUMMARY
}

# =============================================================================
# Главная последовательность
# =============================================================================
main() {
  # Перезагрузка на шаге драйвера: юнит systemd запускает --boot, но машина
  # ещё не настроена — Docker и toolkit ставятся после драйвера. Продолжаем
  # полным проходом.
  if [[ "$MODE" == "boot" && -f "$STATE_FILE" ]] \
     && grep -q '^driver_installed_awaiting_reboot$' "$STATE_FILE"; then
    MODE="full"
    log "Продолжение после перезагрузки на установке драйвера"
  fi

  log "Развёртывание MVP, режим: ${MODE}"

  if [[ "$MODE" == "check" ]]; then
    run_check
    exit 0
  fi

  if [[ "$MODE" == "full" ]]; then
    preflight
    install_base_packages
    disable_kernel_autoupgrade
    install_nvidia_driver
    install_docker
    install_container_toolkit
    verify_gpu_in_container
  else
    # Режим --boot: машина уже настроена, после вытеснения нужно
    # только поднять стек. Установку пакетов пропускаем.
    command -v docker >/dev/null 2>&1 || die "Docker не установлен, нужен полный проход."
    nvidia-smi >/dev/null 2>&1 || die "Драйвер NVIDIA не отвечает, нужен полный проход."
  fi

  materialize_env
  materialize_secrets
  load_env
  apply_egress_guard
  prepare_models
  start_core
  init_qdrant_collection
  start_app
  start_observability
  smoke_test_llm
  install_systemd_unit
  install_backup

  rm -f "$STATE_FILE"
  summary
}

main "$@"
