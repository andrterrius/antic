#!/usr/bin/env bash
# Ensure Python deps (venv) + Patchright Chromium and start Antidetect HTTP API.
# Run from any directory: bash scripts/api/run.sh
#
# Env (optional): ANTIDETECT_API_HOST, ANTIDETECT_API_PORT, ANTIDETECT_API_TOKEN,
#                 ANTIDETECT_DATA_ROOT (default: /root/antidetect-data when root)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

apt_install() {
  if [[ "$(id -u)" -eq 0 ]]; then
    apt-get update -y
    apt-get install -y "$@"
  else
    sudo apt-get update -y
    sudo apt-get install -y "$@"
  fi
}

ensure_python_deps() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 не найден." >&2
    exit 1
  fi

  local venv_dir="$ROOT_DIR/.venv"
  if [[ ! -x "$venv_dir/bin/python" ]]; then
    echo "Создание venv: $venv_dir"
    if ! python3 -m venv "$venv_dir" 2>/dev/null; then
      if command -v apt-get >/dev/null 2>&1; then
        echo "Установка python3-venv…"
        apt_install python3-venv python3-pip
        rm -rf "$venv_dir"
        python3 -m venv "$venv_dir"
      else
        echo "Не удалось создать venv (нужен python3-venv / ensurepip)." >&2
        exit 1
      fi
    fi
  fi

  # shellcheck source=/dev/null
  source "$venv_dir/bin/activate"
  PYTHON="$venv_dir/bin/python"

  echo "Установка Python-зависимостей (requirements.txt)…"
  "$PYTHON" -m pip install -U pip
  "$PYTHON" -m pip install -r "$ROOT_DIR/requirements.txt"
  echo "Python-зависимости установлены."
}

ensure_chromium() {
  echo "Проверка Patchright Chromium…"
  if ! "$PYTHON" -m patchright install chromium; then
    echo "Предупреждение: patchright install chromium завершился с ошибкой." >&2
    echo "API всё равно запустится; браузер поставится при первом launch профиля." >&2
  fi
  # Playwright ≥1.57 on Linux x64 installs chrome-linux64/; older checks expect chrome-linux/.
  ensure_chrome_linux_symlink
}

ensure_chrome_linux_symlink() {
  local cache="${PLAYWRIGHT_BROWSERS_PATH:-${XDG_CACHE_HOME:-$HOME/.cache}/ms-playwright}"
  [[ -d "$cache" ]] || return 0

  local d linked=0
  shopt -s nullglob
  for d in "$cache"/chromium-*; do
    [[ -d "$d" ]] || continue
    if [[ -d "$d/chrome-linux64" && ! -e "$d/chrome-linux" ]]; then
      ln -s chrome-linux64 "$d/chrome-linux"
      echo "Symlink: $d/chrome-linux -> chrome-linux64"
      linked=1
    fi
  done
  shopt -u nullglob
  if [[ "$linked" -eq 0 ]]; then
    echo "chrome-linux symlink: не требуется (или chromium ещё не установлен)."
  fi
}

ensure_python_deps
ensure_chromium

# Данные профилей вне репо (не стираются при git fetch/reset).
if [[ -z "${ANTIDETECT_DATA_ROOT:-}" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    export ANTIDETECT_DATA_ROOT="/root/antidetect-data"
  else
    export ANTIDETECT_DATA_ROOT="${HOME}/antidetect-data"
  fi
fi
mkdir -p "$ANTIDETECT_DATA_ROOT"
echo "Каталог данных: $ANTIDETECT_DATA_ROOT (data/ + user-data/)"

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

HOST="${ANTIDETECT_API_HOST:-127.0.0.1}"
PORT="${ANTIDETECT_API_PORT:-18765}"

echo "Starting Antidetect API from $ROOT_DIR (http://${HOST}:${PORT}/) …"
exec "$PYTHON" "$ROOT_DIR/src/cli_main.py" serve \
  --host "$HOST" \
  --port "$PORT"
