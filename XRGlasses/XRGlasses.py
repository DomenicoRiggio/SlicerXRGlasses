"""XRGlasses: show Slicer's 3D view in stereo in XR glasses.

The module adds an "XR Glasses" toolbar that stays visible in every module.
The module panel holds the one-time setup (the glasses SDK folder) and the
rarely changed settings. Nothing is compiled: the vendor SDK selected by the
user is loaded directly.
"""

import os

import ctk
import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule, ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget)

from XRGlassesLib import device as gd
from XRGlassesLib import sdk as gsdk
from XRGlassesLib import session as gs
from XRGlassesLib import settings
from XRGlassesLib.toolbar import XRGlassesToolBar, icon_path, shortcut_text

DISPLAY_MODE_CHOICES = [
    (0, "Automatic (keep the glasses' resolution)"),
    (gd.MODE_SBS_1080_60, gd.SBS_MODES[gd.MODE_SBS_1080_60]),
    (gd.MODE_SBS_1080_90, gd.SBS_MODES[gd.MODE_SBS_1080_90]),
    (gd.MODE_SBS_1200_60, gd.SBS_MODES[gd.MODE_SBS_1200_60]),
    (gd.MODE_SBS_1200_90, gd.SBS_MODES[gd.MODE_SBS_1200_90]),
]

STATUS_HEADLINE = {
    gs.OFF: "3D is off",
    gs.SEARCHING: "Waiting for the glasses",
    gs.STARTING: "Starting…",
    gs.RUNNING: "3D is on",
    gs.UNSUPPORTED: "Glasses not supported",
    gs.ERROR: "Something went wrong",
    gs.SETUP: "Setup needed",
}


def xr_session():
    """The single XRGlassesSession of this Slicer process."""
    session = getattr(slicer.modules, "XRGlassesSession", None)
    if session is None:
        session = gs.XRGlassesSession()
        slicer.modules.XRGlassesSession = session
    return session


def install_toolbar():
    if not slicer.util.mainWindow():
        return
    old = getattr(slicer.modules, "XRGlassesToolBar", None)
    if old:
        old.remove()
    slicer.modules.XRGlassesToolBar = XRGlassesToolBar(xr_session())


def _shutdown():
    session = getattr(slicer.modules, "XRGlassesSession", None)
    if session:
        session.stop()


class XRGlasses(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "XR Glasses"
        parent.categories = ["Virtual Reality"]
        parent.contributors = ["Domenico Riggio", "Steve Pieper", "Edoardo De Rose"]
        parent.helpText = (
            "Shows the Slicer 3D view in stereo in XR glasses, with optional "
            "6DoF head tracking. Use the XR Glasses toolbar from any module: "
            f"{shortcut_text('toggle')} starts or stops 3D, "
            f"{shortcut_text('mode')} switches head tracking, "
            f"{shortcut_text('recenter')} recenters.")
        parent.acknowledgementText = (
            "Developed by Domenico Riggio, Steve Pieper and Edoardo De Rose. "
            "Thanks to Ron Kikinis, Steve Pieper and Maria Francesca Spadea "
            "for their supervision and support. "
            "Developed for VITURE XR glasses; the VITURE XR Glasses SDK is "
            "downloaded separately and is not part of this extension. "
            "VITURE is a trademark of its owner; this project is not "
            "affiliated with VITURE. Research software, not for clinical use.")
        parent.icon = qt.QIcon(icon_path())
        if slicer.app.commandOptions().noMainWindow:
            return
        slicer.app.connect("startupCompleted()", install_toolbar)
        slicer.app.connect("aboutToQuit()", _shutdown)


class XRGlassesWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self.session = xr_session()
        if not getattr(slicer.modules, "XRGlassesToolBar", None):
            install_toolbar()
        self._updating = False
        layout = self.layout

        layout.addWidget(self._status_card())
        layout.addWidget(self._sdk_section())
        layout.addWidget(self._view_section())
        layout.addWidget(self._stereo_section())
        layout.addWidget(self._cameras_section())
        layout.addWidget(self._advanced_section())
        hint = qt.QLabel(
            f"Shortcuts: {shortcut_text('toggle')} start/stop · "
            f"{shortcut_text('mode')} head tracking · "
            f"{shortcut_text('recenter')} recenter")
        hint.wordWrap = True
        hint.setStyleSheet("color: gray; padding: 6px 2px;")
        layout.addWidget(hint)
        layout.addStretch(1)

        self.session.add_listener(self._refresh)
        self._refresh()

    def cleanup(self):
        if hasattr(self, "_cameraTimer"):
            self._cameraTimer.stop()
        try:
            self.session._listeners.remove(self._refresh)
        except ValueError:
            pass

    # ── sections ─────────────────────────────────────────────────

    def _status_card(self):
        card = qt.QFrame()
        card.setObjectName("XRGlassesCard")
        card.setStyleSheet(
            "QFrame#XRGlassesCard { border: 1px solid palette(mid);"
            " border-radius: 10px; padding: 6px; }")
        grid = qt.QGridLayout(card)
        grid.setHorizontalSpacing(10)
        self.statusDot = qt.QLabel("●")
        self.statusDot.setStyleSheet("font-size: 22px;")
        self.statusHeadline = qt.QLabel()
        self.statusHeadline.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.statusDetail = qt.QLabel()
        self.statusDetail.wordWrap = True
        self.statusDetail.setStyleSheet("color: gray;")
        self.deviceLabel = qt.QLabel()
        self.deviceLabel.setStyleSheet("color: gray;")
        grid.addWidget(self.statusDot, 0, 0, 2, 1, qt.Qt.AlignTop)
        grid.addWidget(self.statusHeadline, 0, 1)
        grid.addWidget(self.statusDetail, 1, 1)
        grid.addWidget(self.deviceLabel, 2, 1)
        self.startButton = qt.QPushButton("Start 3D")
        self.startButton.checkable = True
        self.startButton.minimumHeight = 38
        self.startButton.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.startButton.toolTip = shortcut_text("toggle")
        self.startButton.connect("toggled(bool)", self._on_start_toggled)
        grid.addWidget(self.startButton, 3, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        return card

    def _sdk_section(self):
        ok, _ = self.session.sdk_status()
        box = _collapsible("Glasses SDK", collapsed=ok)
        form = qt.QFormLayout(box)
        explanation = qt.QLabel(
            "Select the SDK folder you downloaded from your glasses' "
            "vendor for this computer. It is needed once.")
        explanation.wordWrap = True
        form.addRow(explanation)
        self.sdkPath = ctk.ctkPathLineEdit()
        self.sdkPath.filters = ctk.ctkPathLineEdit.Dirs
        self.sdkPath.showHistoryButton = False
        self.sdkPath.currentPath = settings.get("sdkFolder")
        self.sdkPath.connect("currentPathChanged(QString)",
                             self._on_sdk_path_changed)
        form.addRow("Folder:", self.sdkPath)
        self.sdkStatus = qt.QLabel()
        self.sdkStatus.wordWrap = True
        form.addRow(self.sdkStatus)
        self.allowButton = qt.QPushButton("Allow this SDK")
        self.allowButton.toolTip = (
            "macOS quarantined the downloaded SDK. This removes the "
            "quarantine flag from the SDK library folder only.")
        self.allowButton.connect("clicked()", self._allow_sdk)
        form.addRow(self.allowButton)
        self.sdkBox = box
        return box

    def _view_section(self):
        box = _collapsible("View", collapsed=False)
        form = qt.QFormLayout(box)
        self.mirrorRadio = qt.QRadioButton("Mirror the Slicer 3D view")
        self.headRadio = qt.QRadioButton(
            "Head tracking (the mouse still rotates the model)")
        group = qt.QButtonGroup(box)
        group.addButton(self.mirrorRadio)
        group.addButton(self.headRadio)
        self.headRadio.connect(
            "toggled(bool)",
            lambda on: self._set("mode", "head" if on else "mirror"))
        form.addRow(self.mirrorRadio)
        form.addRow(self.headRadio)
        self.positionCheck = qt.QCheckBox(
            "Follow head position, not only rotation")
        self.positionCheck.connect(
            "toggled(bool)", lambda on: self._set("positionTracking", on))
        form.addRow(self.positionCheck)
        self.toolbarCheck = qt.QCheckBox("Show the XR Glasses toolbar")
        self.toolbarCheck.toolTip = (
            "The toolbar can also be shown or hidden from View > Toolbars.")
        self.toolbarCheck.connect("toggled(bool)", self._on_toolbar_toggled)
        form.addRow(self.toolbarCheck)
        self.recenterButton = qt.QPushButton("Recenter")
        self.recenterButton.toolTip = shortcut_text("recenter")
        self.recenterButton.connect("clicked()", self.session.recenter)
        form.addRow(self.recenterButton)
        return box

    def _stereo_section(self):
        box = _collapsible("Stereo comfort", collapsed=False)
        form = qt.QFormLayout(box)
        self.depthSlider = _slider(0.0, 3.0, 0.05, 2, "×")
        self.depthSlider.toolTip = (
            "Strength of the 3D effect. 1× is natural; lower it if the image "
            "doubles.")
        self.depthSlider.connect(
            "valueChanged(double)", lambda v: self._set("depth", v))
        form.addRow("Depth:", self.depthSlider)
        self.distanceSlider = _slider(200, 3000, 50, 0, " mm")
        self.distanceSlider.toolTip = (
            "How far away the 3D view's centre of rotation appears. Larger "
            "values make the model feel further away and flatter.")
        self.distanceSlider.connect(
            "valueChanged(double)", lambda v: self._set("virtualDistance", v))
        form.addRow("Viewing distance:", self.distanceSlider)
        self.ipdSlider = _slider(50, 80, 0.5, 1, " mm")
        self.ipdSlider.toolTip = "The distance between your pupils."
        self.ipdSlider.connect("valueChanged(double)",
                               lambda v: self._set("ipd", v))
        form.addRow("Eye distance:", self.ipdSlider)
        self.backgroundCheck = qt.QCheckBox(
            "Black background (see the room through the glasses)")
        self.backgroundCheck.connect(
            "toggled(bool)", lambda on: self._set("blackBackground", on))
        form.addRow(self.backgroundCheck)
        self.swapCheck = qt.QCheckBox("Swap left and right eyes")
        self.swapCheck.connect("toggled(bool)",
                               lambda on: self._set("swapEyes", on))
        form.addRow(self.swapCheck)
        return box

    def _cameras_section(self):
        from XRGlassesLib import cameras as gc
        box = _collapsible("Cameras", collapsed=True)
        form = qt.QFormLayout(box)
        explanation = qt.QLabel(
            "Live views of the glasses' cameras, next to the 3D view. They "
            "run while 3D is on.")
        explanation.wordWrap = True
        form.addRow(explanation)
        self.cameraChecks = {}
        for channel, (label, _, _) in sorted(gc.CAMERAS.items()):
            check = qt.QCheckBox(label)
            check.connect("toggled(bool)",
                          lambda on, c=channel: self._on_camera_toggled(c, on))
            form.addRow(check)
            self.cameraChecks[channel] = check
        rgb = qt.QCheckBox("Center RGB")
        rgb.enabled = False
        rgb.toolTip = ("Not available yet: on macOS the colour camera needs "
                       "a camera permission that Slicer does not have.")
        form.addRow(rgb)
        self.cameraRotateCheck = qt.QCheckBox("Rotate images 180°")
        self.cameraRotateCheck.connect(
            "toggled(bool)",
            lambda on: settings.set("cameraRotate180", bool(on))
            if not self._updating else None)
        form.addRow(self.cameraRotateCheck)
        self.cameraStatus = qt.QLabel()
        self.cameraStatus.wordWrap = True
        self.cameraStatus.setStyleSheet("color: gray;")
        form.addRow(self.cameraStatus)
        self._cameraTimer = qt.QTimer()
        self._cameraTimer.setInterval(500)
        self._cameraTimer.timeout.connect(
            lambda: setattr(self.cameraStatus, "text",
                            self.session.cameras.state_text()))
        self._cameraTimer.start()
        return box

    def _on_camera_toggled(self, channel, on):
        if not self._updating:
            self.session.cameras.set_wanted(channel, bool(on))

    def _advanced_section(self):
        box = _collapsible("Advanced", collapsed=True)
        form = qt.QFormLayout(box)
        self.displayModeCombo = qt.QComboBox()
        for value, label in DISPLAY_MODE_CHOICES:
            self.displayModeCombo.addItem(label, value)
        self.displayModeCombo.connect("currentIndexChanged(int)",
                                      self._on_display_mode_changed)
        form.addRow("Glasses 3D mode:", self.displayModeCombo)
        self.predictionSlider = _slider(0, 80, 5, 0, " ms")
        self.predictionSlider.toolTip = (
            "Predicts head motion to hide display latency. Too low: the model "
            "lags when you turn; too high: it overshoots when you stop.")
        self.predictionSlider.connect(
            "valueChanged(double)", lambda v: self._set("prediction", v))
        form.addRow("Motion prediction:", self.predictionSlider)
        reset = qt.QPushButton("Restore default settings")
        reset.connect("clicked()", self._reset_settings)
        form.addRow(reset)
        return box

    # ── reactions ────────────────────────────────────────────────

    def _set(self, key, value):
        if not self._updating:
            self.session.set_option(key, value)

    def _on_start_toggled(self, checked):
        if self._updating or checked == self.session.active:
            return
        if checked:
            if not self.session.start():
                qt.QTimer.singleShot(0, self._explain_start_failure)
        else:
            self.session.stop()
        self._refresh()

    def _explain_start_failure(self):
        ok, _ = self.session.sdk_status()
        if not ok:
            self.show_sdk_section()
        slicer.util.mainWindow().statusBar().showMessage(
            "XR Glasses: " + self.session.status_detail, 10000)

    def show_sdk_section(self):
        """Open the SDK section and put the cursor in the folder field."""
        self.sdkBox.collapsed = False
        self.sdkPath.setFocus()

    def _on_sdk_path_changed(self, path):
        if self._updating:
            return
        settings.set("sdkFolder", path.strip())
        self.session.sdk_error = ""
        self._refresh()

    def _allow_sdk(self):
        folder = settings.get("sdkFolder")
        if gsdk.remove_quarantine(folder):
            self.session.sdk_error = ""
        self._refresh()

    def _on_toolbar_toggled(self, visible):
        if self._updating:
            return
        toolbar = getattr(slicer.modules, "XRGlassesToolBar", None)
        if toolbar:
            toolbar.set_visible(visible)

    def _on_display_mode_changed(self, index):
        self._set("displayMode", int(DISPLAY_MODE_CHOICES[index][0]))

    def _reset_settings(self):
        settings.reset_all()
        self.session.set_option("mode", settings.get("mode"))

    # ── display ──────────────────────────────────────────────────

    def _refresh(self):
        session = self.session
        self._updating = True
        try:
            color = {gs.RUNNING: "#2fb36d", gs.SEARCHING: "#e3a21a",
                     gs.STARTING: "#e3a21a", gs.ERROR: "#e05252",
                     gs.UNSUPPORTED: "#e05252",
                     gs.SETUP: "#e3a21a"}.get(session.status, "#8a8f98")
            self.statusDot.setStyleSheet(f"font-size: 22px; color: {color};")
            self.statusHeadline.text = STATUS_HEADLINE.get(session.status,
                                                           session.status)
            self.statusDetail.text = ("" if session.status == gs.OFF
                                      else session.status_detail)
            device = session.device_name
            if device and session.firmware:
                device += f" · firmware {session.firmware}"
            self.deviceLabel.text = device
            self.deviceLabel.visible = bool(device)
            self.startButton.checked = session.active
            self.startButton.text = "Stop 3D" if session.active else "Start 3D"

            ok, text = session.sdk_status()
            self.sdkStatus.text = text
            self.sdkStatus.setStyleSheet(
                "color: #2fb36d;" if ok else "color: #e3a21a;")
            # Only offer to lift the download quarantine when it blocked loading.
            self.allowButton.visible = bool(session.sdk_error) and \
                gsdk.is_quarantined(settings.get("sdkFolder"))

            head = session.mode == "head"
            self.mirrorRadio.checked = not head
            self.headRadio.checked = head
            self.positionCheck.checked = session.position_tracking
            self.toolbarCheck.checked = settings.get("showToolbar")
            for channel, check in self.cameraChecks.items():
                check.checked = channel in session.cameras.wanted
            self.cameraRotateCheck.checked = settings.get("cameraRotate180")
            self.cameraStatus.text = session.cameras.state_text()
            self.positionCheck.enabled = head
            self.recenterButton.enabled = head
            self.depthSlider.value = session.depth
            self.distanceSlider.value = session.virtual_distance
            self.ipdSlider.value = session.ipd
            self.backgroundCheck.checked = session.black_background
            self.swapCheck.checked = session.swap_eyes
            self.predictionSlider.value = session.prediction_s * 1000.0
            for index, (value, _) in enumerate(DISPLAY_MODE_CHOICES):
                if value == session.display_mode_choice:
                    self.displayModeCombo.currentIndex = index
        finally:
            self._updating = False


def _collapsible(title, collapsed):
    box = ctk.ctkCollapsibleButton()
    box.text = title
    box.collapsed = collapsed
    return box


def _slider(minimum, maximum, step, decimals, suffix):
    widget = ctk.ctkSliderWidget()
    widget.minimum = minimum
    widget.maximum = maximum
    widget.singleStep = step
    widget.pageStep = step * 5
    widget.decimals = decimals
    widget.suffix = suffix
    widget.tracking = True
    return widget


class XRGlassesTest(ScriptedLoadableModuleTest):
    """Self tests; the session test runs only with an SDK selected."""

    def runTest(self):
        self.setUp()
        self.test_stereo_math()
        self.test_toolbar_present()
        self.test_usb_listing()
        self.test_session_cycle()

    def setUp(self):
        slicer.mrmlScene.Clear()

    def test_stereo_math(self):
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), "Testing",
                            "test_stereo_math.py")
        spec = importlib.util.spec_from_file_location("xr_math_tests", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in dir(module):
            if name.startswith("test_"):
                getattr(module, name)()
        self.delayDisplay("Stereo geometry OK")

    def test_toolbar_present(self):
        toolbars = slicer.util.mainWindow().findChildren(
            qt.QToolBar, "XRGlassesToolBar")
        self.assertEqual(len(toolbars), 1)
        self.delayDisplay("Toolbar OK")

    def test_usb_listing(self):
        from XRGlassesLib import usb
        devices = usb.usb_devices()
        self.assertIsInstance(devices, set)
        self.delayDisplay(f"USB listing OK ({len(devices)} devices)")

    def test_session_cycle(self):
        session = xr_session()
        ok, _ = session.sdk_status()
        if not ok or not session.load_sdk():
            self.delayDisplay("No SDK selected; session test skipped")
            return
        views_before = len(slicer.util.getNodesByClass("vtkMRMLViewNode"))
        for _ in range(2):
            self.assertTrue(session.start())
            self.delayDisplay("Session running", 1500)
            self.assertEqual(
                len(slicer.util.getNodesByClass("vtkMRMLViewNode")),
                views_before + 2)
            session.stop()
            self.assertEqual(
                len(slicer.util.getNodesByClass("vtkMRMLViewNode")),
                views_before)
        self.delayDisplay("Start/stop cycle OK")
