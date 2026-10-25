"""Native auxiliary windows and dock chrome; media stays in the one viewer."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget)
from shiboken6 import isValid

from .touch_filmstrip import FilmstripResizeHandle
from .ui_theme import bind_theme, tokens_for


def accessible_geometry(rect, available):
    """Keep a native title bar reachable, including gaps between mixed-DPI screens."""
    available = [QRect(area) for area in available if area.isValid()]
    if not available:
        return QRect(rect)
    overlaps = [(rect.intersected(area).width() * rect.intersected(area).height(), area)
                for area in available]
    area = max(overlaps, key=lambda value: value[0])[1]
    if not any(score > 0 for score, _ in overlaps):
        area = available[0]
    width = min(max(240, rect.width()), area.width())
    height = min(max(180, rect.height()), area.height())
    return QRect(max(area.left(), min(rect.left(), area.right() - width + 1)),
                 max(area.top(), min(rect.top(), area.bottom() - height + 1)), width, height)


class AuxiliaryWindow(QWidget):
    geometryRemembered = Signal(str, str)

    def __init__(self, title):
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle(title)
        self._shutdown = False
        self._restoring = False
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(180)
        self._geometry_timer.timeout.connect(self._remember_geometry)
        bind_theme(self)

    def restore_saved_geometry(self, saved, default_size):
        self._restoring = True
        try:
            self.resize(default_size)
            if saved:
                try:
                    self.restoreGeometry(QByteArray.fromHex(saved.encode('ascii')))
                except (ValueError, UnicodeError):
                    pass
            # Restoring a previous fullscreen state must never reopen pure mode
            # without an ordinary title bar on the next explicit detach.
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)
            self.recover_to_screens()
        finally:
            self._restoring = False

    def recover_to_screens(self):
        if getattr(self, '_shutdown', False) or not isValid(self):
            return
        if self.isFullScreen():
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                self.setGeometry(screen.geometry())
            return
        if self.isMaximized():
            # The window manager owns its maximized geometry. Applying a
            # screen-sized rect here would overwrite the normal restore rect.
            return
        target = accessible_geometry(self.geometry(), [s.availableGeometry() for s in QApplication.screens()])
        if target.isValid() and target != self.geometry():
            self.setGeometry(target)

    def _remember_geometry(self):
        if not self._shutdown and isValid(self) and not self._restoring and self.isVisible() and not self.isFullScreen():
            self.geometryRemembered.emit(bytes(self.saveGeometry().toHex()).decode('ascii'),
                                         self.screen().name() if self.screen() else '')

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, '_geometry_timer'):
            self._geometry_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_geometry_timer'):
            self._geometry_timer.start()

    def event(self, event):
        if event.type() in (QEvent.Type.ScreenChangeInternal, QEvent.Type.DevicePixelRatioChange):
            QTimer.singleShot(0, self.recover_to_screens)
        return super().event(event)

    def shutdown(self):
        self._shutdown = True
        if not isValid(self):
            return
        self._geometry_timer.stop()
        self.hide()
        self.close()


class PreviewWindow(AuxiliaryWindow):
    returnRequested = Signal()
    initialGeometryReady = Signal()

    def __init__(self):
        super().__init__('LiveViewer · 独立预览窗')
        self.setMinimumSize(320, 240)
        self._fullscreen_normal_geometry = None
        self._fullscreen_maximized = False
        self._initial_show_pending = False
        self._initial_geometry_timer = QTimer(self)
        self._initial_geometry_timer.setSingleShot(True)
        self._initial_geometry_timer.timeout.connect(self._initial_geometry_ready)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.content = QStackedWidget()
        layout.addWidget(self.content)
        self.empty = QLabel('正在载入照片…')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.content.addWidget(self.empty)
        self.viewer = None
        self.live_badge = QLabel('◉ LIVE', self)
        self.live_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.live_badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.live_badge.hide()
        bind_theme(self, self._apply_theme)

    def _apply_theme(self, _scheme):
        colors = tokens_for(self)
        self.live_badge.setStyleSheet(
            f"color:{colors['badge_text']};background:{colors['badge_background']};"
            f"border:1px solid {colors['badge_border']};border-radius:5px;padding:5px 8px;font-weight:600;")
        self._fit_badge()

    def _fit_badge(self):
        if getattr(self, '_shutdown', False) or not isValid(self):
            return
        if hasattr(self, 'live_badge') and isValid(self.live_badge):
            self.live_badge.ensurePolished()
            self.live_badge.resize(self.live_badge.sizeHint().expandedTo(self.live_badge.minimumSizeHint()))
            self.live_badge.move(16, 16)
            self.live_badge.raise_()

    def attach_viewer(self, viewer):
        self.viewer = viewer
        self.content.addWidget(viewer)
        self.content.setCurrentWidget(viewer)
        viewer.show()

    def show_initial_maximized(self, preferred_screen=''):
        """Every explicit detach starts framed and maximized, never pure mode."""
        self._initial_show_pending = True
        self.setWindowState(Qt.WindowState.WindowNoState)
        screen = next((screen for screen in QApplication.screens() if screen.name() == preferred_screen), None)
        if screen is not None:
            self.setScreen(screen)
            self.setGeometry(accessible_geometry(self.geometry(), [screen.availableGeometry()]))
        else:
            self.recover_to_screens()
        self.showMaximized()

    @Slot()
    def _initial_geometry_ready(self):
        if not self._shutdown and isValid(self):
            self.initialGeometryReady.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._initial_show_pending:
            self._initial_show_pending = False
            # Native maximization can resize the viewport after the initial
            # show's zero timer. Restore the photo once at its presented size.
            self._initial_geometry_timer.start(0)

    def take_viewer(self):
        viewer, self.viewer = self.viewer, None
        if viewer is not None:
            self.content.removeWidget(viewer)
            viewer.setParent(None)
        return viewer

    def show_photo_state(self, available, message='', *, live=False, filename=''):
        self.empty.setText(message or '正在载入照片…')
        if self.viewer is not None and available:
            self.content.setCurrentWidget(self.viewer)
        else:
            self.content.setCurrentWidget(self.empty)
        self.live_badge.setVisible(bool(available and live))
        self.setWindowTitle((filename + ' · ' if filename else '') + 'LiveViewer · 独立预览窗')
        self._fit_badge()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            if self._fullscreen_maximized:
                # Avoid a transient normal state: its asynchronous native
                # resize can replace the remembered restore rect with the
                # maximized size after the next state change.
                self.showMaximized()
            else:
                self.showNormal()
                if self._fullscreen_normal_geometry is not None:
                    self.setGeometry(accessible_geometry(self._fullscreen_normal_geometry,
                        [screen.availableGeometry() for screen in QApplication.screens()]))
        else:
            self._fullscreen_normal_geometry = self.normalGeometry()
            self._fullscreen_maximized = self.isMaximized()
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_badge()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            QTimer.singleShot(0, self._fit_badge)

    def closeEvent(self, event):
        if self._shutdown:
            event.accept()
        else:
            event.ignore()
            self.returnRequested.emit()


class TouchMapPanel(QFrame):
    dockRequested = Signal(str)
    floatRequested = Signal()
    redockRequested = Signal()
    returnPreviewRequested = Signal()
    hideRequested = Signal()
    thicknessRequested = Signal(int)

    def __init__(self, surface):
        super().__init__()
        self.setObjectName('film')
        self.surface = surface
        self.edge = 'right'
        self._floating = False
        self._outer = QGridLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self.resize_handle = FilmstripResizeHandle(self)
        self.resize_handle.thicknessRequested.connect(self.thicknessRequested)
        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(6, 5, 6, 6)
        body.setSpacing(5)
        top = QHBoxLayout()
        self.location = QComboBox()
        for text, value in (('右侧', 'right'), ('左侧', 'left'), ('底部', 'bottom'), ('独立窗口', 'floating')):
            self.location.addItem(text, value)
        self.location.activated.connect(self._location_selected)
        top.addWidget(QLabel('触控区'))
        top.addWidget(self.location, 1)
        self.hide_button = QPushButton('×')
        self.hide_button.setToolTip('关闭触控区，保留独立预览')
        self.hide_button.clicked.connect(self.hideRequested)
        top.addWidget(self.hide_button)
        body.addLayout(top)
        actions = QHBoxLayout()
        self.redock_button = QPushButton('收回触控区')
        self.redock_button.clicked.connect(self.redockRequested)
        actions.addWidget(self.redock_button)
        self.return_button = QPushButton('收回预览窗')
        self.return_button.clicked.connect(self.returnPreviewRequested)
        actions.addWidget(self.return_button)
        body.addLayout(actions)
        body.addWidget(surface, 1)
        self.set_mode('right')

    def _location_selected(self):
        value = self.location.currentData()
        if value == 'floating':
            self.floatRequested.emit()
        else:
            self.dockRequested.emit(value)

    def set_mode(self, edge, *, floating=False):
        self.surface.cancel_interaction()
        self.resize_handle.cancel_gesture()
        self.edge, self._floating = edge, floating
        self.location.setCurrentIndex(self.location.findData('floating' if floating else edge))
        self.redock_button.setVisible(floating)
        self._outer.removeWidget(self.body)
        self._outer.removeWidget(self.resize_handle)
        for index in (0, 1):
            self._outer.setRowStretch(index, 0)
            self._outer.setColumnStretch(index, 0)
        self.resize_handle.setVisible(not floating)
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        if floating:
            self._outer.addWidget(self.body, 0, 0)
            self._outer.setRowStretch(0, 1)
            self._outer.setColumnStretch(0, 1)
        elif edge == 'bottom':
            self.resize_handle.set_edge('top')
            self._outer.addWidget(self.resize_handle, 0, 0)
            self._outer.addWidget(self.body, 1, 0)
            self._outer.setRowStretch(1, 1)
            self._outer.setColumnStretch(0, 1)
        else:
            self.resize_handle.set_edge('right' if edge == 'left' else 'left')
            body_column = 0 if edge == 'left' else 1
            self._outer.addWidget(self.body, 0, body_column)
            self._outer.addWidget(self.resize_handle, 0, 1 - body_column)
            self._outer.setRowStretch(0, 1)
            self._outer.setColumnStretch(body_column, 1)


class FloatingMapWindow(AuxiliaryWindow):
    hideRequested = Signal()

    def __init__(self):
        super().__init__('LiveViewer · 触控区')
        self.setMinimumSize(280, 240)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.panel = None

    def attach_panel(self, panel):
        self.panel = panel
        self._layout.addWidget(panel)
        panel.show()

    def take_panel(self):
        panel, self.panel = self.panel, None
        if panel is not None:
            self._layout.removeWidget(panel)
            panel.setParent(None)
        return panel

    def closeEvent(self, event):
        if self._shutdown:
            event.accept()
        else:
            event.ignore()
            self.hideRequested.emit()
