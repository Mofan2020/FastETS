"""FastETS desktop application entry point (pywebview)."""

from __future__ import annotations

import http.server
import logging
import os
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional

import webview

from .. import config
from .bridge import FastETSApi

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("fastets")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Windows consoles use cp1252; never let a Chinese log line kill the app.
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter(LOG_FORMAT))
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    logger.addHandler(stream)

    try:
        file_handler = logging.FileHandler(
            config.logs_dir() / "fastets.log", encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(file_handler)
    except OSError:
        pass
    return logger


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def start_ui_server(directory: Path) -> tuple[socketserver.TCPServer, int]:
    """Serve the UI over 127.0.0.1 so the browser API is a secure context."""

    class Handler(_QuietHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

    for _ in range(20):
        try:
            server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            break
        except OSError:
            server = None  # type: ignore[assignment]
    else:  # pragma: no cover
        raise RuntimeError("无法启动本地 UI 服务")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def main(argv: Optional[list] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    if "--reset-disclaimer" in argv:
        config.reset_disclaimer()
        print("免责声明已重置，下次启动会重新显示。")
        return 0

    logger = setup_logging()
    logger.info("FastETS starting, data dir: %s", config.data_dir())

    web_dir = Path(__file__).parent / "web"
    if not web_dir.exists():
        logger.error("UI 资源缺失: %s", web_dir)
        return 1

    server, port = start_ui_server(web_dir)
    url = f"http://127.0.0.1:{port}/index.html"
    logger.info("UI server on %s", url)

    api = FastETSApi(logger=logger)
    window = webview.create_window(
        "FastETS · 易听说作业助手",
        url=url,
        js_api=api,
        width=1180,
        height=820,
        min_size=(940, 640),
        resizable=True,
        confirm_close=False,
        text_select=True,
    )
    api._set_window(window)

    def on_closing():
        try:
            server.shutdown()
        except Exception:
            pass

    window.events.closing += on_closing

    if "--dev" in argv:
        webbrowser.open(url)

    webview.start(debug="--debug" in argv)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
