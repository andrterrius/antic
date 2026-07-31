# Antidetect CLI

Командная строка (`antidetect-cli`) даёт тот же основной функционал, что и графический интерфейс: управление профилями браузера Playwright, импорт, запуск Chromium и вспомогательные утилиты.

> **Назначение:** тестирование и автоматизация. Функции «уникальный отпечаток / stealth / обход детекта» здесь не реализуются.

## Содержание

- [Требования](#требования)
- [Запуск](#запуск)
- [Общие опции](#общие-опции)
- [Хранение данных](#хранение-данных)
- [Миграция из profiles.json](#миграция-из-profilesjson)
- [Команды профилей](#команды-профилей)
  - [list](#profiles-list)
  - [show](#profiles-show)
  - [new](#profiles-new)
  - [set](#profiles-set)
  - [delete](#profiles-delete)
  - [import-proxies](#profiles-import-proxies)
  - [import-archive](#profiles-import-archive)
  - [export](#profiles-export)
  - [recover](#profiles-recover)
- [Запуск браузера](#запуск-браузера)
  - [run](#run)
  - [run-all](#run-all)
- [Скрипты автоматизации](#скрипты-автоматизации)
- [Утилиты](#утилиты)
  - [install-chromium](#install-chromium)
  - [proxy-ip](#proxy-ip)
  - [geoip](#geoip)
- [HTTP API и веб-UI (serve)](#http-api-и-веб-ui-serve)
- [Коды возврата](#коды-возврата)
- [Примеры сценариев](#примеры-сценариев)

---

## Требования

```bash
python -m pip install -r requirements.txt
python src/cli_main.py install-chromium
```

Нужен установленный Chromium для Playwright/Patchright. Команда `install-chromium` скачивает его при отсутствии.

---

## Запуск

Из корня репозитория:

```bash
python src/cli_main.py --help
python src/cli_main.py <команда> --help
```

Справка по группе профилей:

```bash
python src/cli_main.py profiles --help
python src/cli_main.py profiles <подкоманда> --help
```

### Дерево команд

```
antidetect-cli
├── profiles
│   ├── list
│   ├── show
│   ├── new
│   ├── set
│   ├── delete
│   ├── import-proxies
│   ├── import-archive
│   ├── export
│   └── recover
├── run
├── run-all
├── install-chromium
├── proxy-ip
├── geoip
└── serve
```

---

## Общие опции

| Опция | Описание |
|-------|----------|
| `--log-file PATH` | Дописывать логи запуска профилей в файл (UTF-8). Применяется к командам `run`, `run-all`, `install-chromium`. |
| `-h`, `--help` | Справка по команде. |

Многие подкоманды `profiles` поддерживают:

| Опция | Описание |
|-------|----------|
| `--format text\|json` | Формат вывода (по умолчанию `text`). |
| `--quiet` | Не печатать результат в stdout (код возврата сохраняется). |

---

## Хранение данных

Профили и каталоги браузера хранятся в одном корне приложения:

| Платформа | Путь |
|-----------|------|
| Windows | `%APPDATA%\AntidetectUI\` |
| macOS | `~/Library/Application Support/AntidetectUI/` |
| Linux / прочее | `./data/` относительно репозитория |

Структура:

```
AntidetectUI/
├── data/
│   ├── profiles.db          # SQLite-база профилей
│   └── profiles.db.bak.*    # автобэкапы перед импортом
└── user-data/
    └── <profile_id>/        # persistent context Chromium (cookies, storage)
```

CLI и UI используют **одну и ту же** базу. Изменения из CLI сразу видны в UI и наоборот.

---

## Миграция из profiles.json

При первом запуске CLI, если обнаружен устаревший `profiles.json`, появится интерактивный запрос:

```
Обнаружен старый profiles.json (N профилей): ...
Перенести данные в SQLite? [y/N]:
```

- Ответ `y` / `yes` / `д` / `да` — миграция в SQLite.
- Любой другой ответ или пустой ввод — миграция пропускается.
- При неинтерактивном запуске (нет stdin) миграция не выполняется.

---

## Команды профилей

### `profiles list`

Список всех сохранённых профилей.

```bash
python src/cli_main.py profiles list
python src/cli_main.py profiles list --format json
```

**Текстовый вывод** (табуляция между полями):

```
<profile_id>    <name>    tags=<теги или ->    proxy=<url или ->
```

**JSON:** массив объектов профиля (все поля `BrowserProfile`).

---

### `profiles show`

Полный профиль в JSON.

```bash
python src/cli_main.py profiles show <profile_id>
```

---

### `profiles new`

Создать новый профиль с автогенерированным тестовым отпечатком.

```bash
python src/cli_main.py profiles new --name "Мой профиль"
```

| Опция | Описание |
|-------|----------|
| `--profile-id ID` | Задать ID вручную (по умолчанию — случайный 12-символьный hex). |
| `--name NAME` | Имя (по умолчанию `Profile N`). |
| `--tags TAGS` | Теги через запятую, `;` или `\|` (напр. `work,ads,EU`). |
| `--description TEXT` | Текстовое описание. |
| `--proxy-server URL` | `http://host:port`, `socks5://host:port` или `host:port`. |
| `--proxy-username USER` | Логин прокси. |
| `--proxy-password PASS` | Пароль прокси. |
| `--format text\|json` | Формат вывода. |
| `--quiet` | Без вывода. |

При указании прокси автоматически подстраиваются гео/таймзона (best-effort) и записывается результат проверки прокси.

**Пример:**

```bash
python src/cli_main.py profiles new \
  --name "EU Ads" \
  --tags "work,EU" \
  --proxy-server "socks5://1.2.3.4:1080" \
  --proxy-username user \
  --proxy-password pass \
  --format json
```

---

### `profiles set`

Обновить поля существующего профиля. Указанные опции перезаписывают значения; неуказанные остаются без изменений.

```bash
python src/cli_main.py profiles set <profile_id> --name "Новое имя"
```

| Опция | Описание |
|-------|----------|
| `--name` | Имя профиля. |
| `--tags` | **Заменить** все теги. Пустая строка — сбросить теги. |
| `--description` | Описание. Пустая строка — удалить. |
| `--proxy-server` | URL прокси. |
| `--proxy-username` | Логин прокси. |
| `--proxy-password` | Пароль прокси. |
| `--device-preset` | Пресет Playwright (напр. `iPhone 13`). |
| `--user-agent` | User-Agent. |
| `--locale` | Локаль (напр. `en-US`). Сбрасывается при отсутствии прокси. |
| `--timezone-id` | Таймзона (напр. `Europe/Moscow`). |
| `--country-code` | ISO-3166 alpha-2 (напр. `RU`). |
| `--color-scheme` | `light`, `dark`, `no-preference`. |
| `--viewport-width`, `--viewport-height` | Размер viewport. |
| `--geo-lat`, `--geo-lon` | Координаты геолокации. |
| `--webgl-vendor`, `--webgl-renderer` | Переопределение WebGL. |
| `--webgl-version`, `--webgl-shading-language-version` | Версии WebGL. |
| `--sync-proxy-geo` | Синхронизировать страну/таймзону/координаты с IP прокси (best-effort). |
| `--format text\|json` | Формат вывода. |
| `--quiet` | Без текстового подтверждения (при `--format json` выводится JSON). |

При смене прокси сбрасывается кэш `proxy_health_*`.

**Пример:**

```bash
python src/cli_main.py profiles set abc123def456 \
  --proxy-server "http://proxy.example:8080" \
  --proxy-username user \
  --proxy-password secret \
  --sync-proxy-geo \
  --format json
```

---

### `profiles delete`

Удалить профиль из базы.

```bash
python src/cli_main.py profiles delete <profile_id>
```

| Опция | По умолчанию | Описание |
|-------|--------------|----------|
| `--purge-data` | включено | Удалить каталог `user-data/<profile_id>/`. |
| `--no-purge-data` | — | Оставить данные браузера на диске. |
| `--quiet` | — | Без вывода. |

---

### `profiles import-proxies`

Массовое создание профилей из текстового файла: **одна строка = один профиль**.

```bash
python src/cli_main.py profiles import-proxies proxies.txt
```

**Формат файла** — по одной прокси на строку:

```
host:port:username:password
```

- Пустые строки и строки, начинающиеся с `#`, пропускаются.
- Пароль может содержать символ `:` — всё после третьего двоеточия считается паролем.
- Ожидается IPv4-хост (как в UI).

| Опция | По умолчанию | Описание |
|-------|--------------|----------|
| `--proxy-scheme http\|socks5` | `http` | Схема URL для `host:port`. |
| `--encoding` | `utf-8` | Кодировка файла. |
| `--format text\|json` | `text` | Формат вывода созданных профилей. |
| `--quiet` | — | Без вывода. |

**Пример файла `proxies.txt`:**

```
# рабочие прокси
192.168.1.10:8080:user1:pass1
10.0.0.5:1080:user2:complex:pass:with:colons
```

**Примеры:**

```bash
python src/cli_main.py profiles import-proxies proxies.txt --proxy-scheme socks5
python src/cli_main.py profiles import-proxies proxies.txt --format json --quiet
```

Для каждой валидной строки: новый ID, тестовый fingerprint, прокси, синхронизация гео, проверка доступности прокси.

---

### `profiles import-archive`

Импорт ZIP-архива, экспортированного из UI Antidetect.

```bash
python src/cli_main.py profiles import-archive backup.zip
```

Поддерживаемые форматы архива:

| Формат | Содержимое |
|--------|------------|
| `antidetect-profiles-v1` | `manifest.json`, `profiles.json`, каталоги `user-data/<id>/` |
| `antidetect-profiles-cookies-v1` | `manifest.json`, `profiles.json`, файлы `cookies/<id>.json` |

Поведение:

- Перед импортом создаётся бэкап `profiles.db`.
- Профили **добавляются** к существующим (не заменяют базу целиком).
- При конфликте `profile_id` назначается новый ID.
- Прогресс импорта выводится в **stderr** (если не `--quiet`).

| Опция | Описание |
|-------|----------|
| `--format text\|json` | Текстовая сводка или JSON с полями `added`, `remapped`, `profiles`. |
| `--quiet` | Без вывода. |

**JSON-ответ:**

```json
{
  "added": 3,
  "remapped": 1,
  "profiles": [ /* массив новых профилей */ ]
}
```

> Экспорт: см. [`profiles export`](#profiles-export) или веб-UI / `POST /profiles/export`.

---

### `profiles export`

Экспорт профилей в полный ZIP (`antidetect-profiles-v1`: metadata + `user-data`).

```bash
python src/cli_main.py profiles export
python src/cli_main.py profiles export id1 id2 --out-dir ./backups
```

| Опция | Описание |
|-------|----------|
| `profile_ids` | Необязательные ID; без аргументов — все профили. |
| `--out-dir` | Каталог для ZIP (по умолчанию `.`). |
| `--quiet` | Без прогресса в stderr; путь к файлу всё равно печатается только без `--quiet`. |

---

### `profiles recover`

Восстановить записи профилей из каталогов `user-data/`, если база `profiles.db` потеряна или повреждена, а папки браузера остались.

```bash
python src/cli_main.py profiles recover
```

- Создаёт минимальные записи для папок, которых ещё нет в базе.
- Настройки прокси и fingerprint будут **дефолтными**.
- Для полного восстановления настроек используйте `import-archive` с полным ZIP-экспортом.

| Опция | Описание |
|-------|----------|
| `--quiet` | Без вывода. |

---

## Запуск браузера

### `run`

Запустить один или несколько профилей в Chromium (persistent context).

```bash
python src/cli_main.py run <profile_id> [--url URL] [опции]
python src/cli_main.py run <id1> <id2> --parallel
```

| Опция | По умолчанию | Описание |
|-------|--------------|----------|
| `profile_ids` | — | Один или несколько ID (позиционные аргументы). |
| `--url` | `https://studio.youtube.com` | Стартовая страница. |
| `--script PATH` | — | Путь к `.py` скрипту автоматизации (см. ниже). |
| `--headless` | выкл. | Запуск без окна браузера. |
| `--parallel` | выкл. | Параллельный запуск при нескольких ID. |
| `--no-protect-webrtc` | — | Отключить флаги защиты WebRTC. |
| `--no-force-webrtc-proxy-ip` | — | Не пытаться определить IP прокси для WebRTC. |

Логи печатаются в stdout с префиксом `[Имя:profile_id]`. Остановка: **Ctrl+C** — CLI запросит закрытие контекстов.

**Примеры:**

```bash
python src/cli_main.py run abc123 --url "https://2ip.ru"
python src/cli_main.py run id1 id2 id3 --parallel --log-file run.log
python src/cli_main.py run abc123 --script ./scripts/login.py --headless
```

---

### `run-all`

Запустить **все** профили из базы. Принимает те же опции, что и `run` (кроме списка ID).

```bash
python src/cli_main.py run-all --parallel
python src/cli_main.py run-all --url "https://example.com" --headless
```

---

## Скрипты автоматизации

Передайте путь к `.py` файлу через `--script`. Файл должен определять функцию:

```python
def run(page, log=None):
  if log:
    log("hello from script")
  page.goto("https://example.com")
  page.click("text=Login")
```

- `page` — объект Playwright `Page`.
- `log` — опциональный колбэк для сообщений в лог CLI.
- Если `run(page, log)` не принимает `log`, вызывается `run(page)`.

Скрипт выполняется после открытия стартового URL в контексте профиля.

---

## Утилиты

### `install-chromium`

Установить Chromium для Patchright/Playwright, если он отсутствует.

```bash
python src/cli_main.py install-chromium
```

---

### `proxy-ip`

Определить внешний IP через прокси (сервис ipify).

```bash
python src/cli_main.py proxy-ip "http://host:port" --proxy-username user --proxy-password pass
python src/cli_main.py proxy-ip "socks5://host:port"
```

При успехе печатает IP в stdout. Код возврата `2`, если IP не получен.

---

### `geoip`

Геолокация по IP (JSON в stdout).

```bash
python src/cli_main.py geoip 8.8.8.8
```

Код возврата `2`, если lookup не удался.

---

## HTTP API и веб-UI (serve)

Запуск HTTP API (FastAPI + uvicorn) и веб-интерфейса на том же порту. Zaliver и другие клиенты подключаются к этому API (по умолчанию `http://127.0.0.1:18765`).

```bash
# собрать UI один раз
cd web && npm install && npm run build && cd ..

python src/cli_main.py serve
python src/cli_main.py serve --token "my-secret" --host 127.0.0.1 --port 18765
python src/cli_main.py serve --host 0.0.0.0   # слушать снаружи (осторожно; лучше nginx)
```

### systemd (Linux)

Как у Zaliver API: скрипт поднимает venv/зависимости, unit держит процесс.

```bash
# один раз: зависимости + проверка запуска
bash scripts/api/run.sh
# Ctrl+C

sudo mkdir -p /etc/antidetect
sudo cp scripts/api/antidetect-api.env.example /etc/antidetect/api.env
sudo nano /etc/antidetect/api.env   # токен / host / port

# путь /root/antidetect в .service поправьте под свой клон
sudo cp scripts/api/antidetect-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now antidetect-api
sudo systemctl status antidetect-api
journalctl -u antidetect-api -f
```

| Опция / переменная | По умолчанию | Описание |
|------------------|--------------|----------|
| `--host` / `ANTIDETECT_API_HOST` | `127.0.0.1` | Адрес привязки. |
| `--port` / `ANTIDETECT_API_PORT` | `18765` | Порт. |
| `--token` / `ANTIDETECT_API_TOKEN` | `secret` | Bearer-токен для `serve`. Если не задан — используется `secret`. Десктоп Qt **не** требует токен (API открыт на localhost), пока вы сами не зададите `ANTIDETECT_API_TOKEN`. |
| `--log-level` | `info` | Уровень логов uvicorn. |
| `--no-access-log` | — | Отключить access log. |

После запуска:

- Веб-UI: `http://127.0.0.1:18765/` (нужен собранный `src/web_dist` или `web/dist`)
- OpenAPI: `http://127.0.0.1:18765/docs`
- В UI токен нужен только если API запущен через `serve` с auth

### Авторизация

- **`python … serve`** — по умолчанию Bearer-токен `secret` (или `--token` / env).
- **Десктоп (Qt)** — фоновый API без токена, zaliver ходит как раньше на `:18765` без Authorization.
- Если задан `ANTIDETECT_API_TOKEN` в окружении — auth включён и для десктопа.

```http
Authorization: Bearer <token>
```

Публичный `GET /health` всегда без токена (удобно для nginx/probes).

### nginx (снаружи)

Обычно Antidetect слушает `127.0.0.1:18765`, а снаружи открывается только nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:18765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    client_max_body_size 2g;  # большие ZIP импорта/экспорта
}
```

Токен по-прежнему проверяет Antidetect; при желании добавьте basic auth на стороне nginx.

### Основные эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/health` | Проверка доступности (без токена). |
| `GET` | `/profiles` | Список профилей (+ поле `running`). |
| `GET` | `/profiles/{id}` | Один профиль. |
| `PATCH` | `/profiles/{id}` | Обновить имя. |
| `POST` | `/profiles/{id}/tags/{tag}` | Добавить тег. |
| `DELETE` | `/profiles/{id}/tags/{tag}` | Удалить тег. |
| `PUT/PATCH` | `/profiles/{id}/custom-data` | Замена / слияние `custom_data`. |
| `POST` | `/profiles/import` | Импорт ZIP (`multipart` поле `file`). |
| `POST` | `/profiles/export` | Экспорт ZIP (`mode`: `full` \| `cookies`). |
| `POST` | `/profiles/cookie-hosts` | Домены cookies для экспорта. |
| `POST` | `/profiles/{id}/launch` | Запустить профиль (фоновая сессия). |
| `POST` | `/profiles/{id}/stop` | Остановить по profile_id. |
| `GET` | `/sessions` | Активные и завершённые сессии. |
| `GET` | `/sessions/{id}` | Одна сессия (в т.ч. CDP WebSocket URL). |
| `POST` | `/sessions/{id}/stop` | Запросить остановку. |
| `DELETE` | `/sessions/{id}` | Удалить запись завершённой сессии. |

Полная схема запросов и тел — в Swagger UI (`/docs`). В Authorize укажите Bearer-токен для «Try it out».

**Требования:** Python 3.10+ (или пакет `eval_type_backport` на 3.8–3.9), `python-multipart` для импорта файлов.

Остановка сервера: **Ctrl+C**.

---

## Коды возврата

| Код | Значение |
|-----|----------|
| `0` | Успех. |
| `2` | Ошибка выполнения (сбой запуска профиля, API, proxy-ip, geoip, миграция и т.д.). |
| `1` | Ошибка argparse / `SystemExit` с сообщением (профиль не найден, файл не найден и т.п.). |

Неперехваченные исключения печатаются в stderr как `ERROR: ...` с кодом `2`.

---

## Примеры сценариев

### Массовый импорт прокси и запуск

```bash
python src/cli_main.py profiles import-proxies proxies.txt --proxy-scheme http
python src/cli_main.py profiles list
python src/cli_main.py run-all --parallel --url "https://2ip.ru"
```

### Бэкап и перенос на другую машину

```bash
python src/cli_main.py profiles export --out-dir ./backups
# или через веб-UI / POST /profiles/export
```

На новой машине:

```bash
python -m pip install -r requirements.txt
python src/cli_main.py install-chromium
python src/cli_main.py profiles import-archive backups/antidetect_profiles_*.zip
python src/cli_main.py profiles list --format json
```

### Автоматизация из скрипта (bash)

```bash
PROFILE_ID=$(python src/cli_main.py profiles new --name "Bot" --quiet --format json | python -c "import sys,json; print(json.load(sys.stdin)['profile_id'])")
python src/cli_main.py run "$PROFILE_ID" --script ./my_bot.py --headless
```

### Проверка прокси перед добавлением в профиль

```bash
IP=$(python src/cli_main.py proxy-ip "http://1.2.3.4:8080" --proxy-username u --proxy-password p)
python src/cli_main.py geoip "$IP"
python src/cli_main.py profiles new --name "Checked" --proxy-server "http://1.2.3.4:8080" --proxy-username u --proxy-password p
```

### Восстановление после потери базы

```bash
# Папки user-data/ на месте, profiles.db удалён
python src/cli_main.py profiles recover
# Либо полное восстановление из архива:
python src/cli_main.py profiles import-archive full_backup.zip
```

---

## См. также

- [README.md](../README.md) — установка и запуск UI.
- `python src/cli_main.py --help` — актуальная справка по всем опциям в установленной версии.
