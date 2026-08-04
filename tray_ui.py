"""System tray + console show/hide helpers (Windows)."""
from __future__ import annotations

import ctypes
import sys
import threading
import webbrowser
from typing import Callable

SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9

_console_visible = False


def console_hwnd() -> int:
    return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)


def set_console_visible(visible: bool) -> None:
    global _console_visible
    hwnd = console_hwnd()
    if not hwnd:
        return
    ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)
    if visible:
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    _console_visible = visible


def hide_console() -> None:
    set_console_visible(False)


def toggle_console() -> bool:
    set_console_visible(not _console_visible)
    return _console_visible


def is_console_visible() -> bool:
    return _console_visible


def make_icon_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(196, 92, 38, 255))
    d.rectangle((16, 30, 48, 50), fill=(26, 28, 24, 255))
    d.rectangle((22, 18, 42, 30), fill=(47, 111, 78, 255))
    d.rectangle((28, 12, 36, 18), fill=(47, 111, 78, 255))
    return img


def run_tray(
    *,
    url: str,
    on_exit: Callable[[], None],
    title: str = "PC Yer Acma",
) -> None:
    """Block on tray message loop (call from main thread)."""
    import pystray
    from pystray import MenuItem as Item

    hide_console()

    def open_ui(icon=None, item=None) -> None:
        webbrowser.open(url)

    def show_console(icon=None, item=None) -> None:
        set_console_visible(True)

    def hide_con(icon=None, item=None) -> None:
        set_console_visible(False)

    def quit_app(icon, item=None) -> None:
        try:
            icon.stop()
        finally:
            on_exit()

    menu = pystray.Menu(
        Item("Tarayici arayuzunu ac", open_ui, default=True),
        Item("Konsolu goster", show_console),
        Item("Konsolu gizle", hide_con),
        pystray.Menu.SEPARATOR,
        Item("Cikis", quit_app),
    )

    icon = pystray.Icon(
        "pc_yer_acma",
        make_icon_image(),
        title,
        menu,
    )

    def _notify() -> None:
        try:
            icon.notify("Arka planda calisiyor. Sag tik: tarayici / konsol.", title)
        except Exception:
            pass

    threading.Timer(1.0, _notify).start()
    # Double-click / default action opens browser
    icon.run()
