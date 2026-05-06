#!/usr/bin/env python3
"""
Проверка канала «CDP → кадры вкладки» через Page.startScreencast.

Режим «прямая трансляция в HTML»: локальный HTTP-сервер отдаёт страницу
с полноэкранным <img src="/mjpeg"> и multipart JPEG (как у IP-камер).

Запуск (после старта профиля с expose_cdp и появления cdp_ws_url):

  python tools/test_cdp_vnc_stream.py --cdp-ws "ws://127.0.0.1:PORT/devtools/browser/..." --html-port 8099

Откройте в браузере: http://127.0.0.1:8099/

Только счётчик кадров (без HTML):

  python tools/test_cdp_vnc_stream.py --cdp-ws "ws://..."

Переменная окружения: CDP_WS_URL

Сохранение JPEG на диск:

  python tools/test_cdp_vnc_stream.py --cdp-ws "..." --jpeg-dir ./cdp_frames --save-every 30

При --duration 0 трансляция идёт до Ctrl+C (удобно с --html-port).

После обрыва WebSocket/CDP или закрытия вкладки скрипт снова подключается
(--reconnect-delay / --reconnect-max-delay). Отключить: --no-reconnect.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


def _pick_page(browser):
    for ctx in browser.contexts:
        for page in ctx.pages:
            if not page.is_closed():
                return ctx, page
    for ctx in browser.contexts:
        try:
            return ctx, ctx.new_page()
        except Exception:
            continue
    raise RuntimeError("Нет контекста/страницы: подключитесь к уже запущенному браузеру с открытой вкладкой.")


_INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CDP трансляция</title>
  <style>
    html, body { margin: 0; height: 100%; background: #111; }
    img { display: block; width: 100%; height: 100%; object-fit: contain; }
  </style>
</head>
<body>
  <img src="/mjpeg" alt="трансляция вкладки" />
</body>
</html>
""".encode("utf-8")


class _StreamState:
    __slots__ = ("lock", "latest_jpeg")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latest_jpeg: bytes | None = None


def _make_http_handler(state: _StreamState):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args) -> None:
            pass

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body = _INDEX_HTML
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/mjpeg":
                self.send_response(200)
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                boundary = b"--frame\r\n"
                try:
                    while True:
                        with state.lock:
                            jpg = state.latest_jpeg
                        if jpg:
                            self.wfile.write(boundary)
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii"))
                            self.wfile.write(jpg)
                            self.wfile.flush()
                        time.sleep(1.0 / 60.0)
                except (BrokenPipeError, ConnectionResetError, ValueError):
                    return
            self.send_error(404, "Not found")

    return _Handler


def main() -> int:
    p = argparse.ArgumentParser(description="CDP screencast; опционально трансляция в HTML (MJPEG).")
    p.add_argument(
        "--cdp-ws",
        default=(os.environ.get("CDP_WS_URL") or "").strip(),
        help="webSocketDebuggerUrl (или CDP_WS_URL в окружении).",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=12.0,
        help="Секунд работы (0 = до Ctrl+C).",
    )
    p.add_argument("--quality", type=int, default=72, help="JPEG quality 0–100.")
    p.add_argument("--max-width", type=int, default=1280)
    p.add_argument("--max-height", type=int, default=720)
    p.add_argument("--every-nth-frame", type=int, default=1)
    p.add_argument("--jpeg-dir", type=str, default="", help="Каталог для сохранения JPEG.")
    p.add_argument("--save-every", type=int, default=0, help="Сохранять каждый N-й кадр (0 — не сохранять).")
    p.add_argument(
        "--html-port",
        type=int,
        default=0,
        metavar="PORT",
        help="Поднять HTTP: HTML + MJPEG на http://<bind>:PORT/ (0 — выкл.).",
    )
    p.add_argument(
        "--html-bind",
        type=str,
        default="127.0.0.1",
        help="Адрес привязки HTTP (по умолчанию только localhost).",
    )
    p.add_argument(
        "--reconnect-delay",
        type=float,
        default=1.0,
        help="Начальная пауза перед повторным подключением к CDP (сек).",
    )
    p.add_argument(
        "--reconnect-max-delay",
        type=float,
        default=30.0,
        help="Максимум экспоненциальной задержки между попытками (сек).",
    )
    p.add_argument(
        "--no-reconnect",
        action="store_true",
        help="Не переподключаться после обрыва (одна попытка сессии).",
    )
    args = p.parse_args()

    if not args.cdp_ws:
        print("Нужен --cdp-ws или переменная CDP_WS_URL.", file=sys.stderr)
        return 2

    jpeg_dir = Path(args.jpeg_dir).resolve() if args.jpeg_dir.strip() else None
    if jpeg_dir and args.save_every <= 0:
        print("При --jpeg-dir задайте --save-every > 0.", file=sys.stderr)
        return 2
    if jpeg_dir:
        jpeg_dir.mkdir(parents=True, exist_ok=True)

    stream_state = _StreamState()
    http_server: ThreadingHTTPServer | None = None
    if int(args.html_port) > 0:
        handler = _make_http_handler(stream_state)
        try:
            http_server = ThreadingHTTPServer((args.html_bind, int(args.html_port)), handler)
        except OSError as e:
            print(f"Не удалось открыть порт {args.html_port}: {e}", file=sys.stderr)
            return 2
        http_th = threading.Thread(target=http_server.serve_forever, name="cdp-mjpeg-http", daemon=True)
        http_th.start()
        url = f"http://{args.html_bind}:{int(args.html_port)}/"
        print(f"Трансляция в браузере: {url} (долго без обрыва: --duration 0)")

    frames = 0
    last_meta: dict | None = None
    t0 = time.monotonic()
    save_idx = 0
    infinite = float(args.duration) <= 0
    deadline = None if infinite else t0 + max(0.5, float(args.duration))

    cdp_holder: list = [None]

    def on_screencast_frame(msg: dict) -> None:
        nonlocal frames, last_meta, save_idx
        frames += 1
        last_meta = msg.get("metadata") if isinstance(msg, dict) else None
        sid = msg.get("sessionId") if isinstance(msg, dict) else None
        data_b64 = msg.get("data") if isinstance(msg, dict) else None
        if isinstance(data_b64, str):
            raw = base64.b64decode(data_b64)
            if http_server is not None:
                with stream_state.lock:
                    stream_state.latest_jpeg = raw
        if sid is not None and cdp_holder[0] is not None:
            try:
                cdp_holder[0].send("Page.screencastFrameAck", {"sessionId": sid})
            except Exception:
                pass
        if jpeg_dir and args.save_every > 0 and frames % args.save_every == 0 and isinstance(data_b64, str):
            raw = base64.b64decode(data_b64)
            save_idx += 1
            out = jpeg_dir / f"frame_{save_idx:06d}.jpg"
            out.write_bytes(raw)

    def _cdp_cleanup(cdp) -> None:
        if cdp is None:
            return
        try:
            cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            cdp.detach()
        except Exception:
            pass
        cdp_holder[0] = None

    reconnect = not bool(args.no_reconnect)
    backoff = max(0.1, float(args.reconnect_delay))
    backoff_max = max(backoff, float(args.reconnect_max_delay))
    user_interrupt = False

    try:
        with sync_playwright() as pw:
            while not user_interrupt:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                cdp = None
                session_hit_deadline = False
                try:
                    browser = pw.chromium.connect_over_cdp(args.cdp_ws, timeout=60_000)
                    _ctx, page = _pick_page(browser)
                    cdp = _ctx.new_cdp_session(page)
                    cdp_holder[0] = cdp

                    cdp.on("Page.screencastFrame", on_screencast_frame)

                    cdp.send("Page.enable")
                    cdp.send(
                        "Page.startScreencast",
                        {
                            "format": "jpeg",
                            "quality": int(args.quality),
                            "maxWidth": int(args.max_width),
                            "maxHeight": int(args.max_height),
                            "everyNthFrame": max(1, int(args.every_nth_frame)),
                        },
                    )

                    print(f"CDP подключён, screencast: {page.url!r}")
                    backoff = max(0.1, float(args.reconnect_delay))
                    while not user_interrupt:
                        if deadline is not None and time.monotonic() >= deadline:
                            session_hit_deadline = True
                            break
                        try:
                            page.wait_for_timeout(400)
                        except KeyboardInterrupt:
                            print("Останов по Ctrl+C.")
                            user_interrupt = True
                            break
                        except Exception as e:
                            if reconnect:
                                print(f"Сессия CDP оборвана ({type(e).__name__}: {e}); переподключение…")
                            else:
                                print(f"Сессия CDP оборвана ({type(e).__name__}: {e}).", file=sys.stderr)
                            break
                except KeyboardInterrupt:
                    print("Останов по Ctrl+C.")
                    user_interrupt = True
                except Exception as e:
                    if reconnect:
                        print(f"CDP недоступен ({type(e).__name__}: {e}); повтор через {backoff:.1f} с…")
                    else:
                        print(f"CDP недоступен: {e}", file=sys.stderr)
                        user_interrupt = True
                finally:
                    _cdp_cleanup(cdp)

                if user_interrupt:
                    break
                if session_hit_deadline:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if not reconnect:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2.0, backoff_max)
    finally:
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()

    elapsed = time.monotonic() - t0
    fps = frames / elapsed if elapsed > 0 else 0.0
    print(f"Кадров: {frames} за {elapsed:.2f} с (~{fps:.1f} fps)")
    if last_meta is not None:
        print(f"Последние metadata: {last_meta}")
    if jpeg_dir:
        print(f"Сохранено снимков в {jpeg_dir} (каждые {args.save_every} кадров).")
    if frames == 0:
        print(
            "Кадров не было: проверьте ws URL, что вкладка видима и не headless-offscreen без композитинга.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
