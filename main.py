"""CTRLCV - a lightweight clipboard history manager for Windows."""
import json
import os
import sys
import time
import winreg

from PySide6.QtCore import Qt, QTimer, QMimeData, QUrl, QBuffer, QIODevice, QAbstractNativeEventFilter
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QImage, QAction, QDesktopServices
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

import winapi
from popup import Popup
from storage import Store

APP_NAME = "CTRLCV"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
# Window classes of terminals: they only accept text, so images are pasted there as a saved file's path
# (e.g. for Claude Code to read). Find a window's class with Spy++ / AutoHotkey Window Spy to add more.
TERMINAL_CLASSES = ["ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty",
                    "org.wezfurlong.wezterm", "VirtualConsoleClass"]
DEFAULT_CONFIG = {"hotkey": "Alt+V", "max_items": 500, "max_image_mb": 20,
                  "image_as_path_windows": TERMINAL_CLASSES, "image_dir": ""}
DEFAULT_IMAGE_DIR = os.path.join(DATA_DIR, "images")
IMAGE_KEEP_DAYS = 7
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
HOTKEY_ID = 0xC7C7
# Password managers mark sensitive clipboard data with these formats; never record it.
IGNORE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")


def load_config() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    save_config(cfg)
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def make_icon(paused=False) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#9aa1ab" if paused else "#2f6feb"))
    p.drawRoundedRect(10, 8, 44, 52, 8, 8)
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(22, 3, 20, 12, 4, 4)
    p.drawRect(18, 26, 28, 4)
    p.drawRect(18, 36, 28, 4)
    p.drawRect(18, 46, 18, 4)
    p.end()
    return QIcon(pm)


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG" and winapi.is_hotkey_msg(int(message)) == HOTKEY_ID:
            QTimer.singleShot(0, self.callback)  # leave the native handler before touching UI
            return True, 0
        return False, 0


def autostart_command() -> str:
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    if getattr(sys, "frozen", False):  # packaged with PyInstaller
        return f'"{sys.executable}"'
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


class App:
    def __init__(self, qapp: QApplication):
        self.qapp = qapp
        self.cfg = load_config()
        self.store = Store(os.path.join(DATA_DIR, "history.db"), int(self.cfg["max_items"]))
        self.clipboard = qapp.clipboard()
        self.paused = False
        self.ignore_until = 0.0

        self.popup = Popup(self.cfg["hotkey"], self.store.get_payload)
        self.popup.pasteRequested.connect(self.paste)
        self.popup.pastePathRequested.connect(lambda i: self.paste(i, False, as_path=True))
        self.popup.copyRequested.connect(self.copy_only)
        self.popup.deleteRequested.connect(lambda i: (self.store.delete(i), self.refresh()))
        self.popup.pinRequested.connect(lambda i: (self.store.toggle_pin(i), self.refresh()))
        self.popup.restore_layout(self.cfg.get("window_size"), self.cfg.get("splitter"), self.cfg.get("window_pos"))
        self.popup.set_stay_on_top(bool(self.cfg.get("stay_on_top")))
        self.popup.stayOnTopChanged.connect(self.save_stay_on_top)
        self.popup.hidden.connect(self.save_layout)
        self.popup.editRequested.connect(lambda i, t: (self.store.update_text(i, t), self.refresh()))

        self.capture_timer = QTimer(singleShot=True, interval=80)  # let the source app finish writing
        self.capture_timer.timeout.connect(self.capture)
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

        self.build_tray()
        self.hotkey_filter = HotkeyFilter(self.toggle_popup)
        qapp.installNativeEventFilter(self.hotkey_filter)
        self.register_hotkey()
        self.capture()  # record whatever is on the clipboard right now
        self.cleanup_image_files()

    # ---------- tray ----------
    def build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self.qapp)
        self.tray.setToolTip(f"{APP_NAME} 剪贴板 ({self.cfg['hotkey']})")
        menu = QMenu()
        menu.addAction(f"打开  ({self.cfg['hotkey']})", self.show_popup)
        self.pause_action = QAction("暂停记录", menu, checkable=True)
        self.pause_action.toggled.connect(self.set_paused)
        menu.addAction(self.pause_action)
        auto = QAction("开机自启", menu, checkable=True, checked=autostart_enabled())
        auto.toggled.connect(set_autostart)
        menu.addAction(auto)
        menu.addSeparator()
        menu.addAction("重置窗口尺寸", self.reset_layout)
        menu.addAction("清空历史（保留置顶）", lambda: (self.store.clear(), self.refresh()))
        menu.addAction("打开配置文件", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(CONFIG_PATH)))
        menu.addAction("打开数据目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(DATA_DIR)))
        menu.addSeparator()
        menu.addAction("退出", self.quit)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_popup() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def set_paused(self, on: bool):
        self.paused = on
        self.tray.setIcon(make_icon(on))
        self.tray.setToolTip(f"{APP_NAME} 剪贴板 ({'已暂停' if on else self.cfg['hotkey']})")

    def register_hotkey(self):
        hk = self.cfg["hotkey"]
        try:
            ok = winapi.register_hotkey(int(self.popup.winId()), HOTKEY_ID, hk)
        except ValueError as e:
            ok = False
            hk = f"{hk}（{e}）"
        if not ok:
            self.tray.showMessage(APP_NAME, f"快捷键 {hk} 注册失败，可能被其他程序占用。\n"
                                            f"请在托盘菜单 → 打开配置文件 中修改 hotkey 后重启。",
                                  QSystemTrayIcon.Warning, 8000)
        else:
            self.tray.showMessage(APP_NAME, f"已在后台运行，按 {hk} 打开剪贴板历史", QSystemTrayIcon.Information, 3000)

    def save_layout(self):
        state = self.popup.layout_state()
        if any(self.cfg.get(k) != v for k, v in state.items()):
            self.cfg.update(state)
            save_config(self.cfg)

    def reset_layout(self):
        self.popup.restore_layout()
        for k in ("window_size", "splitter", "window_pos"):
            self.cfg.pop(k, None)
        save_config(self.cfg)

    def save_stay_on_top(self, on: bool):
        self.cfg["stay_on_top"] = on
        self.save_layout()
        save_config(self.cfg)

    def toggle_popup(self):
        if self.popup.isVisible() and self.popup.isActiveWindow():
            self.popup.hide()
        elif self.popup.isVisible():  # pinned but behind focus: bring it forward
            self.popup.activate()
        else:
            self.show_popup()

    def show_popup(self):
        self.refresh()
        self.popup.popup()

    def refresh(self):
        self.popup.set_items(self.store.list())

    # ---------- capture ----------
    def on_clipboard_changed(self):
        if not self.paused and time.monotonic() >= self.ignore_until:
            self.capture_timer.start()

    def capture(self):
        try:
            md = self.clipboard.mimeData()
            if md is None or any(f in fmt for fmt in md.formats() for f in IGNORE_FORMATS):
                return
            urls = md.urls() if md.hasUrls() else []
            if urls and all(u.isLocalFile() for u in urls):
                self.store.add_files([os.path.normpath(u.toLocalFile()) for u in urls])
            elif md.hasText() and md.text().strip():
                html = md.html() if md.hasHtml() else None
                if html and len(html) > 2_000_000:
                    html = None
                self.store.add_text(md.text(), html)
            elif md.hasImage():
                self.capture_image(self.clipboard.image())
            else:
                return
        except Exception as e:  # clipboard can be locked by another process; never crash the app
            print("capture failed:", e, file=sys.stderr)
            return
        if self.popup.isVisible():
            self.refresh()

    def capture_image(self, img: QImage):
        if img.isNull():
            return
        img = img.convertToFormat(QImage.Format_ARGB32)
        pixels = bytes(img.constBits())
        png = self._png(img)
        if len(png) > self.cfg["max_image_mb"] * 1024 * 1024:
            return
        thumb = self._png(img.scaled(220, 132, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.store.add_image(pixels, png, thumb, f"图片 {img.width()}×{img.height()}")

    @staticmethod
    def _png(img: QImage) -> bytes:
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())

    # ---------- output ----------
    @property
    def image_dir(self) -> str:
        return os.path.expandvars(self.cfg.get("image_dir") or DEFAULT_IMAGE_DIR)

    def image_file(self, item_id: int, png: bytes) -> str:
        os.makedirs(self.image_dir, exist_ok=True)
        path = os.path.join(self.image_dir, f"clip_{item_id}.png")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(png)
        return path

    def cleanup_image_files(self):
        cutoff = time.time() - IMAGE_KEEP_DAYS * 86400
        try:
            for name in os.listdir(self.image_dir):
                p = os.path.join(self.image_dir, name)
                if name.startswith("clip_") and name.endswith(".png") and os.path.getmtime(p) < cutoff:
                    os.remove(p)
        except OSError:
            pass

    def put_on_clipboard(self, item_id: int, plain: bool, as_path: bool = False) -> bool:
        self.popup.flush_edit()  # use the edited text, not the stale stored one
        row = self.store.get_payload(item_id)
        if not row:
            return False
        kind, text, html, data = row
        md = QMimeData()
        if kind == "text":
            md.setText(text)
            if html and not plain:
                md.setHtml(html)
        elif kind == "image" and as_path:
            path = self.image_file(item_id, data)
            md.setText(f'"{path}"' if " " in path else path)
        elif kind == "image":
            md.setImageData(QImage.fromData(data))
        elif kind == "files":
            md.setUrls([QUrl.fromLocalFile(p) for p in text.split("\n")])
            md.setText(text)
        self.ignore_until = time.monotonic() + 0.8  # don't re-capture our own write
        self.clipboard.setMimeData(md)
        self.store.touch(item_id)
        return True

    def paste(self, item_id: int, plain: bool, as_path: bool | None = None):
        target = self.popup.prev_hwnd
        if as_path is None:
            as_path = winapi.window_class(target) in self.cfg["image_as_path_windows"]
        if not self.put_on_clipboard(item_id, plain, as_path):
            return
        if not self.popup.stay_on_top:
            self.popup.hide()
        winapi.force_foreground(target)
        QTimer.singleShot(120, winapi.send_ctrl_v)

    def copy_only(self, item_id: int):
        if not self.put_on_clipboard(item_id, False):
            return
        if self.popup.stay_on_top:
            self.popup.flash("已复制")
        else:
            self.popup.hide()
            self.tray.showMessage(APP_NAME, "已复制到剪贴板", QSystemTrayIcon.NoIcon, 1200)

    def quit(self):
        self.save_layout()
        winapi.unregister_hotkey(int(self.popup.winId()), HOTKEY_ID)
        self.tray.hide()
        self.qapp.quit()


def main():
    mutex = winapi.single_instance("Local\\CTRLCV_single_instance")
    if mutex is None:
        return  # already running
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setApplicationName(APP_NAME)
    app = App(qapp)  # noqa: F841 (keep reference alive)
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
