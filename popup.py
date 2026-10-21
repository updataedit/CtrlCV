"""The search-and-paste popup window."""
import os
import time

from PySide6.QtCore import Qt, QSize, QRect, QPoint, QEvent, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap, QFont, QFontMetrics, QPainter, QCursor, QGuiApplication
from PySide6.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
                               QLabel, QStyledItemDelegate, QStyle, QMenu, QGraphicsDropShadowEffect,
                               QPlainTextEdit, QPushButton, QStackedWidget, QSplitter, QSizeGrip,
                               QToolButton)

import winapi
from storage import Item

ITEM_ROLE = Qt.UserRole + 1
ACCENT = QColor("#2f6feb")


def rel_time(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def preview_text(it: Item) -> str:
    if it.kind == "files":
        paths = it.text.split("\n")
        names = [p.replace("/", "\\").rsplit("\\", 1)[-1] for p in paths]
        head = "、".join(names[:3]) + (f" 等 {len(names)} 项" if len(names) > 3 else "")
        return "📁 " + head
    return " ".join(it.text.split())  # collapse whitespace/newlines


def meta_text(it: Item) -> str:
    if it.kind == "text":
        return f"{len(it.text)} 字 · {rel_time(it.created)}"
    if it.kind == "files":
        return f"文件 · {rel_time(it.created)}"
    return f"{it.text} · {rel_time(it.created)}"


class ItemDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pix_cache: dict[int, QPixmap] = {}

    def pixmap(self, it: Item):
        if it.id not in self._pix_cache and it.thumb:
            pm = QPixmap()
            pm.loadFromData(it.thumb)
            self._pix_cache[it.id] = pm
        return self._pix_cache.get(it.id)

    def sizeHint(self, option, index):
        it: Item = index.data(ITEM_ROLE)
        return QSize(0, 78 if it.kind == "image" else 56)

    def paint(self, p: QPainter, option, index):
        it: Item = index.data(ITEM_ROLE)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(6, 2, -6, -2)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e3ecfd"))
            p.drawRoundedRect(r, 8, 8)
        elif option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#f2f4f7"))
            p.drawRoundedRect(r, 8, 8)

        base = QFont(option.font)
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.85)

        # quick-paste index (Ctrl+1..9)
        row = index.row()
        x = r.left() + 8
        if row < 9:
            p.setFont(small)
            p.setPen(QColor("#9aa1ab"))
            p.drawText(QRect(x, r.top(), 16, r.height()), Qt.AlignVCenter | Qt.AlignHCenter, str(row + 1))
        x += 22

        right = r.right() - 10
        if it.pinned:
            p.setFont(base)
            p.setPen(ACCENT)
            p.drawText(QRect(right - 16, r.top() + 6, 16, 20), Qt.AlignRight | Qt.AlignTop, "★")
            right -= 20

        if it.kind == "image":
            pm = self.pixmap(it)
            if pm and not pm.isNull():
                box = QRect(x, r.top() + 6, 110, r.height() - 12)
                scaled = pm.scaled(box.size() * pm.devicePixelRatio(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                pt = QPoint(box.left(), box.top() + (box.height() - scaled.height()) // 2)
                p.drawPixmap(pt, scaled)
            p.setFont(small)
            p.setPen(QColor("#6b7280"))
            p.drawText(QRect(x + 120, r.top(), right - x - 120, r.height()), Qt.AlignVCenter, meta_text(it))
        else:
            p.setFont(base)
            p.setPen(QColor("#1f2328"))
            fm = QFontMetrics(base)
            title = fm.elidedText(preview_text(it), Qt.ElideRight, right - x)
            p.drawText(QRect(x, r.top() + 7, right - x, fm.height()), Qt.AlignLeft | Qt.AlignVCenter, title)
            p.setFont(small)
            p.setPen(QColor("#8b929c"))
            sfm = QFontMetrics(small)
            p.drawText(QRect(x, r.bottom() - sfm.height() - 6, right - x, sfm.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, meta_text(it))
        p.restore()


class Popup(QWidget):
    """Signals carry the item id. paste(id, plain): paste into previous window; copy(id): clipboard only."""
    pasteRequested = Signal(int, bool)
    pastePathRequested = Signal(int)
    copyRequested = Signal(int)
    deleteRequested = Signal(int)
    pinRequested = Signal(int)
    hidden = Signal()
    editRequested = Signal(int, str)
    stayOnTopChanged = Signal(bool)

    DEFAULT_SIZE = (480, 520)
    DEFAULT_SPLIT = (238, 190)
    EDGE = 8  # px from the frame border that acts as a resize handle

    def __init__(self, hotkey_label: str, payload_fn):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(*self.DEFAULT_SIZE)
        self.setMinimumSize(400, 300)
        self.items: list[Item] = []
        self.prev_hwnd = 0
        # "stay on top" mode: the window isn't hidden when focus leaves it, so we keep
        # tracking which other window was last active to know where to paste
        self.stay_on_top = False
        self._pos_known = False
        self.fg_timer = QTimer(self, interval=250)
        self.fg_timer.timeout.connect(self._track_foreground)
        self._menu_open = False
        self.payload_fn = payload_fn  # id -> (kind, text, html, data)
        self._preview_id = None

        self.header = None
        frame = QFrame(self)
        frame.setObjectName("frame")
        frame.setMouseTracking(True)
        frame.installEventFilter(self)
        self.frame = frame
        shadow = QGraphicsDropShadowEffect(self, blurRadius=24, offset=QPoint(0, 4), color=QColor(0, 0, 0, 70))
        frame.setGraphicsEffect(shadow)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.addWidget(frame)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 4, 10, 8)
        lay.setSpacing(6)
        self.search = QLineEdit(placeholderText="搜索剪贴板历史…（空格分隔多个关键词）")
        self.search.textChanged.connect(self.apply_filter)
        self.search.installEventFilter(self)
        self.list = QListWidget()
        self.list.setItemDelegate(ItemDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.show_menu)
        self.list.itemDoubleClicked.connect(lambda it: self.pasteRequested.emit(it.data(ITEM_ROLE).id, False))
        self.list.currentItemChanged.connect(self.update_preview)
        self.list.installEventFilter(self)

        # preview of the selected item: full text is selectable so part of it can be copied
        self.preview = QFrame(objectName="preview")
        pv = QVBoxLayout(self.preview)
        pv.setContentsMargins(8, 6, 8, 8)
        pv.setSpacing(4)
        head = QHBoxLayout()
        self.preview_meta = QLabel(objectName="hint")
        copy_btn = QPushButton("复制", cursor=Qt.PointingHandCursor)
        copy_btn.clicked.connect(self.copy_current)
        paste_btn = QPushButton("粘贴", cursor=Qt.PointingHandCursor)
        paste_btn.clicked.connect(lambda: self.current_id() is not None
                                  and self.pasteRequested.emit(self.current_id(), False))
        head.addWidget(self.preview_meta, 1)
        head.addWidget(copy_btn)
        head.addWidget(paste_btn)
        pv.addLayout(head)
        self.preview_stack = QStackedWidget()
        self.preview_text = QPlainTextEdit(placeholderText="（空）")
        self.preview_text.textChanged.connect(self._on_preview_edited)
        self.preview_text.installEventFilter(self)
        # text items are editable in place; edits are saved shortly after typing stops
        self._loading = False
        self._dirty = False
        self.edit_timer = QTimer(self, singleShot=True, interval=500)
        self.edit_timer.timeout.connect(self.flush_edit)
        self.preview_image = QLabel(alignment=Qt.AlignCenter)
        self.preview_stack.addWidget(self.preview_text)
        self.preview_stack.addWidget(self.preview_image)
        pv.addWidget(self.preview_stack)

        self.hint_text = (f"{hotkey_label} 呼出 · 单击查看，右侧可直接编辑/选中部分复制 · 双击/Enter 粘贴 · "
                          f"Ctrl+C 复制整条 · Shift+Enter 纯文本 · Ctrl+1~9 快速粘贴 · Ctrl+P 置顶 · Ctrl+D 删除")
        self.hint = QLabel(self.hint_text)
        self.hint.setWordWrap(True)
        self.hint.setMinimumWidth(1)
        self.hint.setObjectName("hint")
        self.hint_timer = QTimer(self, singleShot=True, interval=1500)
        self.hint_timer.timeout.connect(lambda: self.hint.setText(self.hint_text))
        self.empty = QLabel("还没有记录，复制点什么试试", alignment=Qt.AlignCenter)
        self.empty.setObjectName("hint")
        # draggable divider between list and preview
        self.splitter = QSplitter(Qt.Horizontal, childrenCollapsible=False, handleWidth=8)
        self.list.setMinimumWidth(160)
        self.preview.setMinimumWidth(150)
        self.splitter.addWidget(self.list)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes(list(self.DEFAULT_SPLIT))
        foot = QHBoxLayout()
        foot.addWidget(self.hint, 1)
        foot.addWidget(QSizeGrip(self), 0, Qt.AlignBottom | Qt.AlignRight)
        self.pin_btn = QToolButton(text="📌", checkable=True, cursor=Qt.PointingHandCursor,
                                   toolTip="固定在最前：点击别处也不会自动隐藏")
        self.pin_btn.toggled.connect(self.set_stay_on_top)
        close_btn = QToolButton(text="✕", cursor=Qt.PointingHandCursor, toolTip="隐藏 (Esc)",
                                objectName="close")
        close_btn.clicked.connect(self.hide)
        # title bar: blank area to grab and move the window
        self.header = QWidget(objectName="header", cursor=Qt.SizeAllCursor)
        self.header.setFixedHeight(34)
        self.header.installEventFilter(self)
        hb = QHBoxLayout(self.header)
        hb.setContentsMargins(4, 0, 0, 0)
        hb.setSpacing(4)
        title = QLabel("CTRLCV  ·  拖动此处移动", objectName="title")
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        hb.addWidget(title)
        hb.addStretch(1)
        hb.addWidget(self.pin_btn)
        hb.addWidget(close_btn)
        lay.addWidget(self.header)
        lay.addWidget(self.search)
        lay.addWidget(self.splitter, 1)
        lay.addWidget(self.empty, 1)
        lay.addLayout(foot)

        self.setStyleSheet("""
            #frame { background: #ffffff; border: 1px solid #d8dde3; border-radius: 12px; }
            QLineEdit { border: 1px solid #d0d7de; border-radius: 8px; padding: 7px 10px;
                        font-size: 14px; background: #f6f8fa; }
            QLineEdit:focus { border-color: #2f6feb; background: #ffffff; }
            QListWidget { border: none; background: transparent; outline: none; font-size: 13px; }
            QListWidget::item { border: none; }
            #hint { color: #8b929c; font-size: 11px; }
            QSplitter::handle { background: transparent; }
            QSplitter::handle:hover { background: #e3ecfd; border-radius: 3px; }
            #preview { background: #f6f8fa; border: 1px solid #e1e5ea; border-radius: 8px; }
            QPlainTextEdit { border: none; border-radius: 6px; background: transparent; font-size: 13px;
                             selection-background-color: #b6cdfb; selection-color: #1f2328; }
            QPlainTextEdit:focus { background: #ffffff; }
            #title { color: #9aa1ab; font-size: 11px; }
            QToolButton { border: 1px solid transparent; border-radius: 6px; padding: 2px 6px;
                          background: transparent; font-size: 12px; color: #6b7280; }
            QToolButton#close:hover { background: #fde2e2; border-color: #f5b5b5; color: #c62828; }
            QToolButton:hover { border-color: #2f6feb; }
            QToolButton:checked { background: #dbe7fd; border-color: #2f6feb; }
            QPushButton { border: 1px solid #d0d7de; border-radius: 6px; padding: 3px 12px;
                          background: #ffffff; font-size: 12px; }
            QPushButton:hover { border-color: #2f6feb; color: #2f6feb; }
            QScrollBar:vertical { width: 8px; background: transparent; }
            QScrollBar::handle:vertical { background: #d0d7de; border-radius: 4px; min-height: 30px; }
            QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
        """)

    # ---------- data ----------
    def set_items(self, items: list[Item]):
        cur = self.current_id()
        self.items = items
        self.apply_filter(keep_id=cur)

    def apply_filter(self, *_, keep_id=None):
        words = self.search.text().lower().split()
        self.list.clear()
        select_row = 0
        for it in self.items:
            hay = it.text.lower()
            if all(w in hay for w in words):
                li = QListWidgetItem()
                li.setData(ITEM_ROLE, it)
                if it.kind != "image":
                    li.setToolTip(it.text[:2000])
                self.list.addItem(li)
                if it.id == keep_id:
                    select_row = self.list.count() - 1
        has = self.list.count() > 0
        self.splitter.setVisible(has)
        self.empty.setVisible(not has)
        self.empty.setText("没有匹配的记录" if words else "还没有记录，复制点什么试试")
        if has:
            self.list.setCurrentRow(select_row)

    def update_preview(self, li, _prev=None):
        self.flush_edit()
        if not li:
            return
        it: Item = li.data(ITEM_ROLE)
        if it.id == self._preview_id and self.splitter.isVisible():
            return
        self._preview_id = it.id
        payload = self.payload_fn(it.id)
        if not payload:
            return
        kind, text, _html, data = payload
        self.preview_meta.setText(meta_text(it))
        if kind == "image":
            pm = QPixmap()
            pm.loadFromData(data)
            box = self.preview_stack.size()
            self.preview_image.setPixmap(pm.scaled(box, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                                         if pm.width() > box.width() or pm.height() > box.height() else pm)
            self.preview_stack.setCurrentWidget(self.preview_image)
        else:
            self._loading = True
            self.preview_text.setPlainText(text[:200_000])
            self.preview_text.setReadOnly(kind != "text" or len(text) > 200_000)
            self._loading = False
            self._dirty = False
            self.preview_stack.setCurrentWidget(self.preview_text)

    def _on_preview_edited(self):
        if not self._loading and not self.preview_text.isReadOnly():
            self._dirty = True
            self.edit_timer.start()

    def flush_edit(self):
        """Save pending edits now (called before copy/paste/switch/hide)."""
        if not self._dirty:
            return
        self._dirty = False
        self.edit_timer.stop()
        text = self.preview_text.toPlainText()
        if text.strip() and self._preview_id is not None:
            self.preview_meta.setText(f"{len(text)} 字 · 已编辑")
            self.editRequested.emit(self._preview_id, text)

    def copy_current(self):
        """Copy the selected part of the preview if any, otherwise the whole item."""
        cursor = self.preview_text.textCursor()
        if self.preview_stack.currentWidget() is self.preview_text and cursor.hasSelection():
            self.preview_text.copy()
            self.flash("已复制选中内容")
        elif self.current_id() is not None:
            self.copyRequested.emit(self.current_id())

    def flash(self, msg: str):
        self.hint.setText(f"✓ {msg}")
        self.hint_timer.start()

    def current_id(self):
        li = self.list.currentItem()
        return li.data(ITEM_ROLE).id if li else None

    def row_id(self, row: int):
        li = self.list.item(row)
        return li.data(ITEM_ROLE).id if li else None

    # ---------- show / hide ----------
    def popup(self):
        self._remember_target()
        self._preview_id = None
        self.search.clear()
        self.apply_filter()
        if not (self.stay_on_top and self._pos_known):
            self.move(self._anchor_pos())
        self.show()
        self.raise_()
        self.activateWindow()
        winapi.force_foreground(int(self.winId()))
        self.search.setFocus()

    def activate(self):
        """Bring an already-visible (pinned) window to the front and focus the search box."""
        self._remember_target()
        self.raise_()
        self.activateWindow()
        winapi.force_foreground(int(self.winId()))
        self.search.setFocus()
        self.search.selectAll()

    def _remember_target(self):
        fg = winapi.get_foreground()
        if fg and winapi.window_pid(fg) != os.getpid():
            self.prev_hwnd = fg

    def _track_foreground(self):
        if self.isVisible():
            self._remember_target()

    def set_stay_on_top(self, on: bool):
        self.stay_on_top = on
        if self.pin_btn.isChecked() != on:
            self.pin_btn.setChecked(on)
        if on:
            self._pos_known = True
            self.fg_timer.start()
        else:
            self.fg_timer.stop()
        self.stayOnTopChanged.emit(on)

    def _anchor_pos(self) -> QPoint:
        anchor = None
        caret = winapi.caret_screen_pos()
        if caret:  # physical pixels -> Qt logical coordinates
            for s in QGuiApplication.screens():
                g, dpr = s.geometry(), s.devicePixelRatio()
                native = QRect(g.topLeft(), g.size() * dpr)
                if native.contains(*caret):
                    anchor = g.topLeft() + (QPoint(*caret) - g.topLeft()) / dpr
                    break
        if anchor is None:
            anchor = QCursor.pos()
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        x = min(max(anchor.x() - 12, avail.left()), avail.right() - self.width())
        y = anchor.y() + 4
        if y + self.height() > avail.bottom():
            y = max(avail.top(), anchor.y() - self.height() - 24)
        return QPoint(x, y)

    # ---------- size ----------
    def layout_state(self) -> dict:
        return {"window_size": [self.width(), self.height()], "splitter": self.splitter.sizes(),
                "window_pos": [self.x(), self.y()]}

    def restore_layout(self, size=None, split=None, pos=None):
        self.resize(*(size or self.DEFAULT_SIZE))
        self.splitter.setSizes(list(split or self.DEFAULT_SPLIT))
        self._pos_known = False
        if pos and QGuiApplication.screenAt(QPoint(pos[0] + 40, pos[1] + 40)):  # still on a connected screen
            self.move(*pos)
            self._pos_known = True

    def _edges_at(self, pos: QPoint):
        r, m = self.frame.rect(), self.EDGE
        edges = Qt.Edges()
        if pos.x() <= m:
            edges |= Qt.LeftEdge
        elif pos.x() >= r.width() - m:
            edges |= Qt.RightEdge
        if pos.y() <= m:
            edges |= Qt.TopEdge
        elif pos.y() >= r.height() - m:
            edges |= Qt.BottomEdge
        return edges

    _EDGE_CURSORS = {
        Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor,
        Qt.TopEdge: Qt.SizeVerCursor, Qt.BottomEdge: Qt.SizeVerCursor,
        Qt.LeftEdge | Qt.TopEdge: Qt.SizeFDiagCursor, Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor,
        Qt.RightEdge | Qt.TopEdge: Qt.SizeBDiagCursor, Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor,
    }

    def _frame_mouse(self, e) -> bool:
        edges = self._edges_at(e.position().toPoint())
        if e.type() == QEvent.MouseMove and not e.buttons():
            cur = self._EDGE_CURSORS.get(edges)
            if cur is None:
                self.frame.unsetCursor()
            else:
                self.frame.setCursor(cur)
        elif e.type() == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
            if edges:
                self.windowHandle().startSystemResize(edges)
            else:  # drag the window by any blank area
                self.windowHandle().startSystemMove()
                self._pos_known = True
            return True
        return False

    def hideEvent(self, e):
        self.flush_edit()
        super().hideEvent(e)
        self.hidden.emit()

    def changeEvent(self, e):
        if (e.type() == QEvent.ActivationChange and not self.isActiveWindow()
                and not self._menu_open and not self.stay_on_top):
            self.hide()
        super().changeEvent(e)

    # ---------- keyboard ----------
    def eventFilter(self, obj, e):
        if (obj is self.header and e.type() == QEvent.MouseButtonPress and e.button() == Qt.LeftButton):
            self.windowHandle().startSystemMove()
            self._pos_known = True
            return True
        if obj is self.frame and e.type() in (QEvent.MouseMove, QEvent.MouseButtonPress):
            return self._frame_mouse(e)
        if e.type() == QEvent.KeyPress and self.handle_key(obj, e):
            return True
        return super().eventFilter(obj, e)

    def handle_key(self, obj, e) -> bool:
        key, mods = e.key(), e.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        if key == Qt.Key_Escape:
            if self.search.text():
                self.search.clear()
            else:
                self.hide()
            return True
        if ctrl and key == Qt.Key_C:
            if obj is self.search and self.search.hasSelectedText():
                return False  # normal copy inside the search box
            self.copy_current()
            return True
        if obj is self.preview_text:
            return False  # let the preview handle its own selection keys
        if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
            n = self.list.count()
            if n:
                step = {Qt.Key_Up: -1, Qt.Key_Down: 1, Qt.Key_PageUp: -8, Qt.Key_PageDown: 8}[key]
                self.list.setCurrentRow(max(0, min(n - 1, self.list.currentRow() + step)))
            return True
        cid = self.current_id()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if cid is not None:
                if ctrl:
                    self.copyRequested.emit(cid)
                else:
                    self.pasteRequested.emit(cid, bool(mods & Qt.ShiftModifier))
            return True
        if ctrl and Qt.Key_1 <= key <= Qt.Key_9:
            rid = self.row_id(key - Qt.Key_1)
            if rid is not None:
                self.pasteRequested.emit(rid, False)
            return True
        if ctrl and key == Qt.Key_P and cid is not None:
            self.pinRequested.emit(cid)
            return True
        if ctrl and key == Qt.Key_D and cid is not None:
            self.deleteRequested.emit(cid)
            return True
        return False

    # ---------- context menu ----------
    def show_menu(self, pos):
        li = self.list.itemAt(pos)
        if not li:
            return
        it: Item = li.data(ITEM_ROLE)
        m = QMenu(self)
        m.addAction("粘贴", lambda: self.pasteRequested.emit(it.id, False))
        if it.kind == "text":
            m.addAction("粘贴为纯文本", lambda: self.pasteRequested.emit(it.id, True))
        elif it.kind == "image":
            m.addAction("粘贴为文件路径", lambda: self.pastePathRequested.emit(it.id))
        m.addAction("复制", lambda: self.copyRequested.emit(it.id))
        m.addSeparator()
        m.addAction("取消置顶" if it.pinned else "置顶", lambda: self.pinRequested.emit(it.id))
        m.addAction("删除", lambda: self.deleteRequested.emit(it.id))
        self._menu_open = True
        try:
            m.exec(self.list.viewport().mapToGlobal(pos))
        finally:
            self._menu_open = False
