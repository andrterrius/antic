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

## Скрипты автоматизации

Можно указать путь к `.py` файлу, который содержит функцию:

```python
def run(page, log=None):
    if log:
        log("hello from script")
    page.click("text=Login")
```

