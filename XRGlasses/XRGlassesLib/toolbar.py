"""The XR Glasses toolbar: always visible, whatever module is open."""

import os

import qt
import slicer

from . import session as gs
from . import settings

TOOLBAR_NAME = "XRGlassesToolBar"

# Toolbars taken out of the window are kept alive here instead of deleted:
# destroying a widget whose signals PythonQt still routes to Python crashes
# Slicer (EXC_BAD_ACCESS in ~PythonQtSignalReceiver).
_retired = []

STATUS_COLORS = {
    gs.OFF: "#8a8f98",
    gs.SEARCHING: "#e3a21a",
    gs.STARTING: "#e3a21a",
    gs.RUNNING: "#2fb36d",
    gs.UNSUPPORTED: "#e05252",
    gs.ERROR: "#e05252",
    gs.SETUP: "#e3a21a",
}

STATUS_TEXT = {
    gs.OFF: "Off",
    gs.SEARCHING: "Connect glasses",
    gs.STARTING: "Starting…",
    gs.RUNNING: "3D on",
    gs.UNSUPPORTED: "Not supported",
    gs.ERROR: "Problem",
    gs.SETUP: "Setup needed",
}

# Qt maps Ctrl to ⌘ and Alt to ⌥ on macOS.
SHORTCUTS = {
    "toggle": "Ctrl+Alt+V",
    "recenter": "Ctrl+Alt+R",
    "mode": "Ctrl+Alt+T",
}


def shortcut_text(key):
    return qt.QKeySequence(SHORTCUTS[key]).toString(
        qt.QKeySequence.NativeText)


def icon_path(name="XRGlasses.png"):
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "Resources", "Icons", name)




class _ThemeWatcher(qt.QObject):
    """Re-styles the toolbar when the light/dark theme changes.

    Style sheets freeze the colours they were given, so the toolbar's few
    styled widgets are restyled from the new palette on every theme change.
    """

    def __init__(self, callback, parent):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, _obj, event):
        if event.type() in (qt.QEvent.PaletteChange,
                            qt.QEvent.ApplicationPaletteChange,
                            qt.QEvent.StyleChange):
            qt.QTimer.singleShot(0, self._callback)
        return False


class XRGlassesToolBar:
    def __init__(self, session):
        self.session = session
        self.toolbar = None
        self._shortcuts = []
        self._updating = False
        self._create()
        session.add_listener(self.refresh)
        self.refresh()

    # ── construction ─────────────────────────────────────────────

    def _create(self):
        main_window = slicer.util.mainWindow()
        for old in main_window.findChildren(qt.QToolBar, TOOLBAR_NAME):
            _retire(old)

        toolbar = qt.QToolBar("XR Glasses", main_window)
        toolbar.setObjectName(TOOLBAR_NAME)
        toolbar.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        # Listed in View > Toolbars and in the toolbar area's context menu.
        toolbar.setWindowTitle("XR Glasses")
        # A row of its own, so the controls never hide behind the "»" menu.
        main_window.addToolBarBreak(qt.Qt.TopToolBarArea)
        main_window.addToolBar(qt.Qt.TopToolBarArea, toolbar)
        self.toolbar = toolbar

        self.startAction = qt.QAction("Start 3D", toolbar)
        self.startAction.setCheckable(True)
        self.startAction.setIcon(qt.QIcon(icon_path()))
        self.startAction.setToolTip(
            "Show the 3D view in the glasses "
            f"({shortcut_text('toggle')})")
        # toggled, not triggered: it also fires for accessibility presses.
        self.startAction.toggled.connect(self._on_start_toggled)
        toolbar.addAction(self.startAction)

        self.statusLabel = qt.QLabel()
        self.statusLabel.setObjectName("XRGlassesStatus")
        self.statusLabel.setTextFormat(qt.Qt.RichText)
        toolbar.addWidget(self.statusLabel)

        self.fpsLabel = qt.QLabel()
        self.fpsLabel.setObjectName("XRGlassesFps")
        self.fpsLabel.setMinimumWidth(64)
        self.fpsLabel.setAlignment(qt.Qt.AlignCenter)
        toolbar.addWidget(self.fpsLabel)
        self._fpsTimer = qt.QTimer()
        self._fpsTimer.setInterval(500)
        self._fpsTimer.timeout.connect(self._update_frame_rate)
        self._fpsTimer.start()
        toolbar.addSeparator()

        # Segmented control: Mirror | Head tracking.
        segment = qt.QWidget()
        segment_layout = qt.QHBoxLayout(segment)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        segment_layout.setSpacing(0)
        self.mirrorButton = qt.QToolButton()
        self.mirrorButton.setText("Mirror")
        self.mirrorButton.setToolTip(
            "The glasses show exactly the Slicer 3D view")
        self.headButton = qt.QToolButton()
        self.headButton.setText("Head tracking")
        self.headButton.setToolTip(
            "The model stays in place while you move your head; the mouse "
            f"still rotates it ({shortcut_text('mode')})")
        self._modeGroup = qt.QButtonGroup(segment)
        self._modeGroup.setExclusive(True)
        for button in (self.mirrorButton, self.headButton):
            button.setCheckable(True)
            self._modeGroup.addButton(button)
            segment_layout.addWidget(button)
        self.headButton.toggled.connect(self._on_head_toggled)
        toolbar.addWidget(segment)

        self.recenterAction = qt.QAction("Recenter", toolbar)
        self.recenterAction.setToolTip(
            "Use the current head position as straight ahead "
            f"({shortcut_text('recenter')})")
        self.recenterAction.triggered.connect(self.session.recenter)
        toolbar.addAction(self.recenterAction)
        toolbar.addSeparator()

        toolbar.addWidget(qt.QLabel(" Depth "))
        self.depthSlider = qt.QSlider(qt.Qt.Horizontal)
        self.depthSlider.setRange(0, 200)
        self.depthSlider.setFixedWidth(100)
        self.depthSlider.setToolTip(
            "Strength of the 3D effect. Lower it if the image doubles or "
            "tires the eyes.")
        self.depthSlider.valueChanged.connect(self._on_depth_changed)
        toolbar.addWidget(self.depthSlider)

        self.moreButton = qt.QToolButton()
        self.moreButton.setText("⚙")
        self.moreButton.setToolTip("More settings")
        self.moreButton.setPopupMode(qt.QToolButton.InstantPopup)
        menu = qt.QMenu(self.moreButton)
        self.positionAction = menu.addAction("Follow head position")
        self.positionAction.setCheckable(True)
        self.positionAction.toggled.connect(
            lambda on: self._set("positionTracking", bool(on)))
        self.swapAction = menu.addAction("Swap left and right eyes")
        self.swapAction.setCheckable(True)
        self.swapAction.toggled.connect(
            lambda on: self._set("swapEyes", bool(on)))
        self.backgroundAction = menu.addAction(
            "Black background (see-through)")
        self.backgroundAction.setCheckable(True)
        self.backgroundAction.toggled.connect(
            lambda on: self._set("blackBackground", bool(on)))
        menu.addSeparator()
        menu.addAction("All settings…").triggered.connect(
            lambda: slicer.util.selectModule("XRGlasses"))
        self.moreButton.setMenu(menu)
        toolbar.addWidget(self.moreButton)

        spacer = qt.QWidget()
        spacer.setSizePolicy(qt.QSizePolicy.Expanding,
                             qt.QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        self.closeButton = qt.QToolButton()
        self.closeButton.setText("✕")
        self.closeButton.setAutoRaise(True)
        self.closeButton.setToolTip(
            "Hide this toolbar. Bring it back from View > Toolbars > "
            "XR Glasses, or from the XR Glasses module.")
        self.closeButton.clicked.connect(lambda: self.set_visible(False))
        toolbar.addWidget(self.closeButton)

        # View > Toolbars entry (the user's own show/hide is remembered).
        self.viewAction = toolbar.toggleViewAction()
        self.viewAction.setText("XR Glasses")
        self.viewAction.triggered.connect(
            lambda on: self.set_visible(bool(on)))
        toolbars_menu = _toolbars_menu(main_window)
        if toolbars_menu is not None:
            for action in toolbars_menu.actions():
                if action.text == "XR Glasses":
                    toolbars_menu.removeAction(action)
            toolbars_menu.addAction(self.viewAction)

        self._themeWatcher = _ThemeWatcher(self._apply_styles, toolbar)
        toolbar.installEventFilter(self._themeWatcher)
        self._apply_styles()

        for key, method in (("toggle", self._toggle_from_shortcut),
                            ("recenter", self.session.recenter),
                            ("mode", self._toggle_mode)):
            shortcut = qt.QShortcut(qt.QKeySequence(SHORTCUTS[key]),
                                    main_window)
            shortcut.setContext(qt.Qt.ApplicationShortcut)
            shortcut.activated.connect(method)
            self._shortcuts.append(shortcut)

        toolbar.setVisible(settings.get("showToolbar"))

    def set_visible(self, visible):
        """Show or hide the toolbar and remember the choice."""
        settings.set("showToolbar", bool(visible))
        if self.toolbar:
            self.toolbar.setVisible(bool(visible))
        self.session._notify()

    def _apply_styles(self):
        """Style the pill and segmented control from the current palette."""
        if not self.toolbar:
            return
        palette = slicer.app.palette()
        text = palette.color(qt.QPalette.WindowText).name()
        mid = palette.color(qt.QPalette.Mid).name()
        highlight = palette.color(qt.QPalette.Highlight).name()
        highlighted = palette.color(qt.QPalette.HighlightedText).name()
        muted = palette.color(qt.QPalette.Disabled,
                              qt.QPalette.WindowText).name()
        segment = (
            "QToolButton {{ padding: 2px 10px; border: 1px solid {mid};"
            " background: transparent; color: {text}; {radius} }}"
            " QToolButton:checked {{ background: {hl}; color: {hlt};"
            " border-color: {hl}; }}")
        self.mirrorButton.setStyleSheet(segment.format(
            mid=mid, text=text, hl=highlight, hlt=highlighted,
            radius="border-top-left-radius: 9px;"
                   " border-bottom-left-radius: 9px;"))
        self.headButton.setStyleSheet(segment.format(
            mid=mid, text=text, hl=highlight, hlt=highlighted,
            radius="border-top-right-radius: 9px;"
                   " border-bottom-right-radius: 9px; border-left: none;"))
        self.statusLabel.setStyleSheet(
            f"QLabel {{ padding: 2px 10px; border-radius: 10px;"
            f" background: rgba(127, 127, 127, 45); color: {text}; }}")
        self.fpsLabel.setStyleSheet(
            f"QLabel {{ color: {muted}; padding: 0 6px; }}")

    def remove(self):
        self._fpsTimer.stop()
        try:
            self.session._listeners.remove(self.refresh)
        except ValueError:
            pass
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            _retired.append(shortcut)
        self._shortcuts = []
        if self.toolbar:
            _retire(self.toolbar)
            self.toolbar = None

    # ── reactions ────────────────────────────────────────────────

    def _set(self, key, value):
        if not self._updating:
            self.session.set_option(key, value)

    def _on_start_toggled(self, checked):
        if self._updating or checked == self.session.active:
            return
        if checked:
            if not self.session.start():
                # Never open dialogs inside this slot: a native message box
                # run from a button's own signal can crash Qt on macOS when
                # the press comes from accessibility. Act once it returns.
                qt.QTimer.singleShot(0, self._explain_start_failure)
        else:
            self.session.stop()
        self.refresh()

    def _toggle_from_shortcut(self):
        self.startAction.setChecked(not self.session.active)

    def _explain_start_failure(self):
        ok, _ = self.session.sdk_status()
        if not ok:
            # Missing or unusable SDK: take the user straight to the setting.
            slicer.util.selectModule("XRGlasses")
            widget = getattr(slicer.modules, "xrglasses", None)
            if widget:
                representation = widget.widgetRepresentation()
                if representation and hasattr(representation, "self"):
                    representation.self().show_sdk_section()
        slicer.util.mainWindow().statusBar().showMessage(
            "XR Glasses: " + self.session.status_detail, 10000)

    def _on_head_toggled(self, on):
        self._set("mode", "head" if on else "mirror")

    def _toggle_mode(self):
        if self.session.mode == "head":
            self.mirrorButton.setChecked(True)
        else:
            self.headButton.setChecked(True)

    def _on_depth_changed(self, value):
        self._set("depth", value / 100.0)

    # ── display ──────────────────────────────────────────────────

    def _update_frame_rate(self):
        if not self.toolbar:
            return
        session = self.session
        running = session.active and session.status == gs.RUNNING
        self.fpsLabel.setVisible(running)
        if not running:
            return
        frames, _ = session.frame_rate()
        self.fpsLabel.setText(_frame_rate_text(frames))
        self.fpsLabel.setToolTip(
            "Frames shown in the glasses during the last second. The view is "
            "only redrawn when something moves, so it reads 'idle' when "
            "nothing does.")

    def refresh(self):
        if not self.toolbar:
            return
        session = self.session
        head = session.mode == "head"
        self._updating = True
        try:
            self.startAction.setChecked(session.active)
            self.startAction.setText("Stop 3D" if session.active
                                     else "Start 3D")
            self.headButton.setChecked(head)
            self.mirrorButton.setChecked(not head)
            self.recenterAction.setEnabled(head)
            self.depthSlider.setValue(int(round(session.depth * 100)))
            self.positionAction.setChecked(session.position_tracking)
            self.positionAction.setEnabled(head)
            self.swapAction.setChecked(session.swap_eyes)
            self.backgroundAction.setChecked(session.black_background)
        finally:
            self._updating = False

        status = session.status
        text = STATUS_TEXT.get(status, status)
        if status == gs.RUNNING and head:
            if not session.head_tracking_available():
                text = "3D · no head tracking on these glasses"
            elif session.tracking_quality:
                text = "3D · head tracking"
            else:
                text = "3D · finding tracking…"
        color = STATUS_COLORS.get(status, "#8a8f98")
        self.statusLabel.setText(
            f'<span style="color:{color};">●</span>&nbsp;{text}')
        self.statusLabel.setToolTip(session.status_detail)


def _frame_rate_text(frames):
    """Toolbar text for the eyes' frame rate; 'idle' while nothing moves."""
    if frames == 0:
        return "idle"
    return f"{frames} fps"


def _toolbars_menu(main_window):
    """Slicer's View > Toolbars menu, or None."""
    for menu in main_window.findChildren(qt.QMenu):
        title = menu.title.replace("&", "")
        if title == "Toolbars":
            return menu
    return None


def _retire(toolbar):
    """Take a toolbar out of the main window without destroying it."""
    for widget in toolbar.findChildren(qt.QWidget):
        widget.blockSignals(True)
    for action in toolbar.actions():
        action.blockSignals(True)
        action.setEnabled(False)
    toolbar.setObjectName(TOOLBAR_NAME + "Retired")
    slicer.util.mainWindow().removeToolBar(toolbar)
    toolbar.hide()
    _retired.append(toolbar)
