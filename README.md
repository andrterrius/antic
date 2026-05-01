# antidetect (PyQt6 + Playwright UI)

Этот проект содержит **PyQt6 UI** для управления **Playwright профилями**:
- хранение профилей в `%APPDATA%/AntidetectUI/profiles.json`
- запуск Chromium persistent context (каждый профиль хранит свои cookies/storage)
- поддержка прокси (server/username/password)
- опциональный python‑скрипт автоматизации

> Примечание: этот UI предназначен для **тестирования/автоматизации**. Функции “уникальный отпечаток/stealth/обход детекта” здесь не реализуются.

## Установка

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Запуск

```bash
python src/qt_main.py
```

## CLI (командная строка)

CLI даёт тот же основной функционал, что и UI: управление профилями + запуск профилей.

```bash
python src/cli_main.py --help
```

### Профили

```bash
python src/cli_main.py profiles list
python src/cli_main.py profiles new --name "My Profile"
python src/cli_main.py profiles show <profile_id>
python src/cli_main.py profiles set <profile_id> --proxy-server "http://host:port" --sync-proxy-geo
python src/cli_main.py profiles delete <profile_id> --purge-data
```

### Запуск

```bash
python src/cli_main.py run <profile_id> --url "https://2ip.ru"
python src/cli_main.py run <id1> <id2> --parallel
python src/cli_main.py run-all --parallel
```

Остановка: `Ctrl+C` (CLI попросит закрыть контексты).

## Скрипты автоматизации

Можно указать путь к `.py` файлу, который содержит функцию:

```python
def run(page, log=None):
    if log:
        log("hello from script")
    page.click("text=Login")
```

