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
  - [recover](#profiles-recover)
- [Запуск браузера](#запуск-браузера)
  - [run](#run)
  - [run-all](#run-all)
- [Скрипты автоматизации](#скрипты-автоматизации)
- [Утилиты](#утилиты)
  - [install-chromium](#install-chromium)
  - [proxy-ip](#proxy-ip)
  - [geoip](#geoip)
- [HTTP API (serve)](#http-api-serve)
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

> **Экспорт** архива доступен только в графическом UI. В CLI есть только импорт.

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

## HTTP API (serve)

Запуск локального HTTP API (FastAPI + uvicorn) — альтернатива прямому `run` для интеграций.

```bash
python src/cli_main.py serve
python src/cli_main.py serve --host 0.0.0.0 --port 9000
```

| Опция / переменная | По умолчанию | Описание |
|------------------|--------------|----------|
| `--host` / `ANTIDETECT_API_HOST` | `127.0.0.1` | Адрес привязки. |
| `--port` / `ANTIDETECT_API_PORT` | `18765` | Порт. |
| `--log-level` | `info` | Уровень логов uvicorn. |
| `--no-access-log` | — | Отключить access log. |

После запуска:

- Документация OpenAPI: `http://127.0.0.1:18765/docs`
- Корень: `GET /` — ссылки на основные эндпоинты

### Основные эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/health` | Проверка доступности. |
| `GET` | `/profiles` | Список профилей. |
| `GET` | `/profiles/{id}` | Один профиль. |
| `PATCH` | `/profiles/{id}` | Обновить имя. |
| `POST` | `/profiles/{id}/tags/{tag}` | Добавить тег. |
| `DELETE` | `/profiles/{id}/tags/{tag}` | Удалить тег. |
| `PUT/PATCH` | `/profiles/{id}/custom-data` | Замена / слияние `custom_data`. |
| `POST` | `/profiles/{id}/launch` | Запустить профиль (фоновая сессия). |
| `GET` | `/sessions` | Активные и завершённые сессии. |
| `GET` | `/sessions/{id}` | Одна сессия (в т.ч. CDP WebSocket URL). |
| `POST` | `/sessions/{id}/stop` | Запросить остановку. |
| `DELETE` | `/sessions/{id}` | Удалить запись завершённой сессии. |

Полная схема запросов и тел — в Swagger UI (`/docs`).

**Требования:** Python 3.10+ (или пакет `eval_type_backport` на 3.8–3.9).

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

На исходной машине — экспорт через UI → `antidetect_profiles_*.zip`.

На новой машине:

```bash
python -m pip install -r requirements.txt
python src/cli_main.py install-chromium
python src/cli_main.py profiles import-archive antidetect_profiles_20260101_120000.zip
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
