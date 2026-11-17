"""Side-by-side stereo session: XR glasses <-> Slicer 3D view.

An XRGlassesSession owns the connection to the glasses, a frameless window on
the glasses' display holding one 3D view per eye, and a timer that keeps
both eye cameras in step with Slicer's main 3D view and, in head-tracking
mode, with the wearer's head.
"""

import collections
import logging
import os
import threading
import time

import numpy as np
import qt
import slicer
import vtk

from . import cameras as gc
from . import device as gd
from . import displays
from . import sdk as gsdk
from . import settings
from . import stereo_math

UPDATE_INTERVAL_MS = 11         # ~90 Hz poll; rendering only on change
MIN_ROTATION_RAD = 0.0003       # head motion below this is sensor noise
MIN_TRANSLATION_M = 0.00005
REFERENCE_SETTLE_S = 0.3        # ignore the first poses after (re)connecting
PLACEHOLDER_POSE = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
# A head cannot move this much between two samples (~11 ms): it is the SDK
# relocalising. The view is then kept still instead of jumping.
JUMP_ROTATION_RAD = 0.6
JUMP_TRANSLATION_M = 0.25
DISPLAY_MODE_CHECK_S = 3.0      # re-check the 3D mode; a switch takes ~3 s
MODE_SWITCH_SETTLE_S = 3.0      # time the glasses need to change mode
DISPLAY_MODE_RETRY_S = 0.5
DISPLAY_MODE_GIVE_UP_S = 10.0
EYE_VIEW_TAG = "XRGlasses.EyeView"

# Session states reported to the user interface.
OFF = "off"
SEARCHING = "searching"
STARTING = "starting"
RUNNING = "running"
UNSUPPORTED = "unsupported"
SETUP = "setup"                 # the SDK folder is missing or unusable
ERROR = "error"


class XRGlassesSession(qt.QObject):
    """Created once per Slicer process; start() and stop() any number of times."""

    def __init__(self):
        super().__init__()
        self._link = None            # gd.DeviceLink, once the SDK loads
        self._sdk = None
        self._sdk_folder_loaded = None
        self._loader = None          # thread loading the SDK, if any
        self._loader_result = None   # (sdk, error) from that thread
        self.sdk_error = ""
        self._timer = qt.QTimer()
        self._timer.setInterval(UPDATE_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._listeners = []

        self.active = False
        self.status = OFF
        self.status_detail = "Stopped"
        self.device_name = ""
        self.firmware = ""
        self.tracking_quality = 0

        self._generation = 0
        self._display_mode_before = None
        self._display_mode_target = None
        self._display_mode_set = False
        self._display_mode_first_try = 0.0
        self._display_mode_last_try = 0.0
        self._display_mode_set_time = 0.0

        self._window = None
        self._eye_widgets = {}
        self._eye_view_nodes = {}
        self._eye_camera_nodes = {}
        self._main_view_node = None
        self._main_view_observer = None
        self._added_view_ids = []      # (display node id, eye view id)
        self._frame_times = collections.deque(maxlen=240)
        self._frame_ms = 0.0
        self._last_inputs = None
        self._screen = None
        self._last_reveal = 0.0

        self._reference_pose = None
        self._last_delta = None
        self._first_pose_time = None
        self._scene_observers = []

        self._load_settings()
        self.cameras = gc.CameraViews(self)
        # Whatever happens, eye cameras never go into a saved scene.
        slicer.mrmlScene.AddObserver(slicer.mrmlScene.StartSaveEvent,
                                     lambda *_: _exclude_eye_cameras_from_save())

    # ── public API ───────────────────────────────────────────────

    def add_listener(self, callback):
        """callback() runs whenever status or settings change."""
        self._listeners.append(callback)

    def sdk_status(self):
        """(ok, text) describing the SDK selected in the settings."""
        folder = settings.get("sdkFolder")
        if self._sdk is not None:
            if os.path.normcase(os.path.abspath(folder or "")) != \
                    os.path.normcase(os.path.abspath(self._sdk_folder_loaded)):
                return (True, f"SDK {self._sdk.version_text} in use. Restart "
                        "Slicer to switch to the newly selected folder.")
            return True, f"SDK {self._sdk.version_text} ✓"
        problem = gsdk.describe_problem(folder)
        if problem:
            return False, problem
        if self.sdk_error:
            return False, self.sdk_error
        return True, "SDK found ✓ (it loads when 3D starts)"

    def load_sdk(self):
        """Load the SDK now; returns True on success (sets sdk_error)."""
        return self._ensure_link()

    def start(self):
        if self.active:
            return True
        if self._link is None:
            problem = gsdk.describe_problem(settings.get("sdkFolder"))
            if problem:
                self.sdk_error = problem
                self._set_status(SETUP, problem)
                return False
            self._begin_loading_sdk()
        elif not self._link.start():
            self._set_status(ERROR, "The glasses connection could not start.")
            return False
        self.active = True
        self._generation = 0
        self._display_mode_before = None
        self._reference_pose = None
        self._last_delta = None
        self._display_mode_set = False
        self._create_eye_views()
        self._observe_scene(True)
        if self._link is None:
            self._set_status(STARTING, "Loading the glasses SDK…")
        else:
            self._set_status(SEARCHING, "Looking for the glasses…")
        self._timer.start()
        self._tick()
        return True

    def stop(self):
        if not self.active:
            return
        self.active = False
        self._timer.stop()
        self.cameras.stop()
        self._restore_display_mode()
        self._observe_scene(False)
        self._destroy_eye_views()
        try:
            if self._link is not None:
                self._link.stop()
        except Exception:  # noqa: BLE001 - never block shutdown
            logging.exception("XRGlasses: stopping the connection failed")
        self.device_name = ""
        self.firmware = ""
        self.tracking_quality = 0
        self._set_status(OFF, "Stopped")

    def toggle(self):
        if self.active:
            self.stop()
        else:
            self.start()

    def recenter(self):
        """Make the current head pose the neutral pose."""
        self._reference_pose = None
        self._last_delta = None
        self._last_inputs = None
        self._notify()

    def set_option(self, key, value):
        settings.set(key, value)
        self._load_settings()
        if key == "blackBackground":
            self._sync_eye_view_nodes()
        if key == "swapEyes" and self._window:
            self._layout_eyes()
        if key == "displayMode" and self.active and self.status == RUNNING:
            self._display_mode_set = False
            self._display_mode_first_try = time.monotonic()
        if key == "mode":
            self.recenter()
        self._last_inputs = None
        self._notify()

    # ── settings ─────────────────────────────────────────────────

    def _load_settings(self):
        self.mode = settings.get("mode")
        if self.mode not in ("mirror", "head"):
            self.mode = "head"
        self.position_tracking = settings.get("positionTracking")
        self.ipd = min(max(settings.get("ipd"), 50.0), 80.0)
        self.depth = min(max(settings.get("depth"), 0.0), 3.0)
        self.virtual_distance = min(
            max(settings.get("virtualDistance"), 200.0), 3000.0)
        self.prediction_s = min(max(settings.get("prediction"), 0.0),
                                100.0) / 1000.0
        self.display_mode_choice = settings.get("displayMode")
        self.black_background = settings.get("blackBackground")
        self.swap_eyes = settings.get("swapEyes")

    # ── status ───────────────────────────────────────────────────

    def _set_status(self, status, detail):
        if status == self.status and detail == self.status_detail:
            return
        self.status = status
        self.status_detail = detail
        self._notify()
        self.cameras._sync()  # camera views follow the connection

    def _notify(self):
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                logging.exception("XRGlasses: listener failed")

    def _begin_loading_sdk(self):
        """Load the SDK on a worker thread: the first load of a downloaded
        library can take many seconds while the OS verifies it."""
        if self._loader is not None:
            return
        folder = settings.get("sdkFolder")
        self._loader_result = None

        def load():
            try:
                self._loader_result = (gsdk.GlassesSdk(folder), "", folder)
            except gsdk.SdkError as error:
                self._loader_result = (None, str(error), folder)
            except Exception as error:  # noqa: BLE001
                self._loader_result = (None, f"The SDK failed to load: "
                                       f"{error}", folder)

        self._loader = threading.Thread(target=load, name="XRGlassesSdkLoad",
                                        daemon=True)
        self._loader.start()

    def _finish_loading_sdk(self):
        """In the GUI thread: adopt the loaded SDK. True once usable."""
        if self._link is not None:
            return True
        if self._loader is None or self._loader.is_alive():
            return False
        sdk, error, folder = self._loader_result
        self._loader = None
        if sdk is None:
            self.sdk_error = error
            self.stop()
            self._set_status(SETUP, error)
            return False
        self._sdk, self._sdk_folder_loaded = sdk, folder
        self._link = gd.DeviceLink(sdk)
        self.sdk_error = ""
        self._link.start()
        self._set_status(SEARCHING, "Looking for the glasses…")
        return True

    def _ensure_link(self):
        if self._link is not None:
            return True
        folder = settings.get("sdkFolder")
        try:
            self._sdk = gsdk.GlassesSdk(folder)
        except gsdk.SdkError as error:
            self.sdk_error = str(error)
            self._notify()
            return False
        self._sdk_folder_loaded = folder
        self._link = gd.DeviceLink(self._sdk)
        self.sdk_error = ""
        self._notify()
        return True

    # ── main loop ────────────────────────────────────────────────

    def _tick(self):
        if not self.active:
            return
        try:
            self._tick_unguarded()
        except Exception as error:  # noqa: BLE001
            # Never let one bad frame silently kill the loop; report and stop.
            logging.exception("XRGlasses: update failed")
            self.stop()
            self._set_status(ERROR, f"Stopped after an error: {error}")

    def _tick_unguarded(self):
        if self._link is None:
            self._finish_loading_sdk()
            if self._link is None:
                return
        state = self._link.state()
        if state == gd.READY:
            generation = self._link.generation()
            if generation != self._generation:
                self._on_connected(generation)
            self._apply_display_mode()
        else:
            if self._generation and state in (gd.SEARCHING, gd.INITIALIZING):
                # Glasses were unplugged; the link reconnects on its own.
                self._generation = 0
                self._display_mode_set = False
                self._reference_pose = None
                self._last_delta = None
                self._first_pose_time = None
                self.tracking_quality = 0
            if state == gd.SEARCHING:
                self._set_status(SEARCHING, "Looking for the glasses — "
                                 "connect them with USB-C.")
            elif state == gd.INITIALIZING:
                self._set_status(STARTING, "Glasses found, starting tracking…")
            elif state == gd.UNSUPPORTED:
                self._set_status(UNSUPPORTED, "These glasses are not "
                                 "supported by the selected SDK.")
            elif state == gd.ERROR:
                self._set_status(ERROR, "The glasses refused to start "
                                 f"(SDK error {self._link.last_error()}); "
                                 "retrying. Try reconnecting the cable.")

        self._place_window()
        self._update_eyes(state == gd.READY)

    def frame_rate(self):
        """(frames drawn in the last second, ms per frame) for both eyes."""
        now = time.monotonic()
        recent = sum(1 for stamp in self._frame_times if now - stamp <= 1.0)
        return recent, self._frame_ms

    def head_tracking_available(self):
        """False only for connected glasses without 6DoF tracking."""
        return not (self._link and self._generation
                    and not self._link.has_6dof())

    def _on_connected(self, generation):
        self._generation = generation
        self.device_name = self._link.device_name() or "XR glasses"
        self.firmware = self._link.firmware()
        # Remember the 2D mode found at the first connection of the session:
        # glasses plugged in again wake up in their power-on default instead.
        if self._display_mode_before is None:
            mode = self._link.display_mode()
            if mode in gd.SBS_MODES:
                self._display_mode_before = gd.FLAT_MODE_FOR_SBS[mode]
            elif mode >= 0:
                self._display_mode_before = mode
        self._display_mode_set = False
        self._display_mode_first_try = time.monotonic()
        self._display_mode_last_try = 0.0
        self._reference_pose = None
        self._last_delta = None
        self._first_pose_time = None
        self._set_status(STARTING, "Switching the glasses to 3D…")

    # ── display mode ─────────────────────────────────────────────

    def _wanted_display_mode(self):
        if self.display_mode_choice in gd.SBS_MODES:
            return self.display_mode_choice
        return gd.SBS_MODE_FOR_FLAT.get(self._display_mode_before,
                                        gd.MODE_SBS_1080_60)

    def _apply_display_mode(self):
        now = time.monotonic()
        if self._display_mode_set:
            # The glasses apply mode changes late (a 2D request from a session
            # stopped a moment ago can still land), so keep 3D while running.
            if (self._display_mode_target is not None and
                    now - self._display_mode_last_try >= DISPLAY_MODE_CHECK_S):
                self._display_mode_last_try = now
                current = self._link.display_mode()
                if current >= 0 and current != self._display_mode_target:
                    self._link.set_display_mode(self._display_mode_target)
            return
        if now - self._display_mode_last_try < DISPLAY_MODE_RETRY_S:
            return
        if now - _last_restore_time[0] < MODE_SWITCH_SETTLE_S:
            # A return to 2D from a session stopped moments ago is still in
            # progress; asking for 3D now would be undone when it lands.
            return
        self._display_mode_last_try = now
        target = self._wanted_display_mode()
        current = self._link.display_mode()
        if current == target or self._link.set_display_mode(target):
            self._display_mode_set = True
            self._display_mode_target = target
            self._display_mode_set_time = now
            self._set_status(RUNNING, f"{self.device_name} · 3D "
                             f"{gd.SBS_MODES[target]}")
        elif now - self._display_mode_first_try > DISPLAY_MODE_GIVE_UP_S:
            self._display_mode_set = True
            self._set_status(RUNNING, "The glasses did not accept 3D mode. "
                             "Press the 2D/3D button on the glasses.")

    def _restore_display_mode(self):
        if not self._link or self._link.state() != gd.READY:
            return
        if self._link.display_mode() not in gd.SBS_MODES:
            return
        flat = self._display_mode_before
        if flat is None:
            flat = gd.FLAT_MODE_FOR_SBS.get(self._link.display_mode(),
                                            gd.MODE_1080_60)
        for _ in range(10):
            if (self._link.display_mode() == flat or
                    self._link.set_display_mode(flat)):
                if self._link.display_mode() == flat:
                    break
            time.sleep(0.1)
        self._display_mode_target = None
        _last_restore_time[0] = time.monotonic()

    # ── stereo window ────────────────────────────────────────────

    def _glasses_screen(self):
        return displays.glasses_screen(slicer.util.mainWindow(),
                                       bool(self._generation))

    def _create_window(self):
        window = qt.QWidget()
        window.setObjectName("XRGlassesStereoWindow")
        window.setWindowTitle("XR Glasses 3D")
        window.setStyleSheet("background-color: black;")
        self._window = window
        self._screen = None

    def _place_window(self):
        """Keep the stereo window full screen on the glasses, or hidden."""
        if not self._window:
            return
        screen = self._glasses_screen()
        if screen is None and self._generation and self._display_mode_set:
            # In 3D mode macOS may take the glasses off the desktop (it treats
            # them as a headset); put them back so the window can go there.
            # Does nothing on other platforms.
            now = time.monotonic()
            if now - self._last_reveal > 2.0:
                self._last_reveal = now
                displays.reveal_hidden_glasses()
        if screen is None or not (self._generation and self._display_mode_set):
            # Never show the stereo pair on the Mac's own screen, nor on the
            # glasses before they are in 3D (it would look squashed).
            if self._window.visible:
                self._window.hide()
            self._screen = None
            return
        if (screen is self._screen and self._window.visible and
                self._window.geometry == screen.geometry):
            return
        self._screen = screen
        self._window.setWindowFlags(
            qt.Qt.Window | qt.Qt.FramelessWindowHint
            | qt.Qt.WindowDoesNotAcceptFocus)
        self._window.setGeometry(screen.geometry)
        self._window.setCursor(qt.QCursor(qt.Qt.BlankCursor))
        self._window.show()
        self._layout_eyes()

    def _layout_eyes(self):
        if not self._window:
            return
        width = max(2, self._window.width)
        height = max(1, self._window.height)
        half = width // 2
        order = ("right", "left") if self.swap_eyes else ("left", "right")
        for index, eye in enumerate(order):
            widget = self._eye_widgets.get(eye)
            if widget:
                widget.setGeometry(index * half, 0, half, height)
        self._last_inputs = None

    # ── eye views ────────────────────────────────────────────────

    def _main_view(self):
        layout_manager = slicer.app.layoutManager()
        if layout_manager:
            for index in range(layout_manager.threeDViewCount):
                node = layout_manager.threeDWidget(index).mrmlViewNode()
                if node and not node.GetAttribute(EYE_VIEW_TAG):
                    return node
        for node in slicer.util.getNodesByClass("vtkMRMLViewNode"):
            if not node.GetAttribute(EYE_VIEW_TAG):
                return node
        return None

    def _create_eye_views(self):
        if self._eye_widgets:
            return
        self._create_window()
        cameras = slicer.modules.cameras.logic()
        for eye in ("left", "right"):
            node = slicer.vtkMRMLViewNode()
            node.SetName(f"XR glasses {eye} eye")
            node.SetLayoutName(EYE_LAYOUT_NAMES[0 if eye == "left" else 1])
            node.SetLayoutLabel("VL" if eye == "left" else "VR")
            node.SetAttribute(EYE_VIEW_TAG, eye)
            node.SetHideFromEditors(True)
            node.SetSaveWithScene(False)
            slicer.mrmlScene.AddNode(node)
            widget = slicer.qMRMLThreeDWidget(self._window)
            widget.setMRMLScene(slicer.mrmlScene)
            widget.setMRMLViewNode(node)
            widget.threeDController().setVisible(False)
            widget.show()
            self._eye_view_nodes[eye] = node
            self._eye_widgets[eye] = widget
        slicer.app.processEvents()
        for eye, node in self._eye_view_nodes.items():
            self._eye_camera_nodes[eye] = _eye_camera(cameras, node)
        self._set_main_view(self._main_view())
        self._share_restricted_display_nodes()
        self._layout_eyes()
        # Volume renderings created before these views need a nudge.
        for node in slicer.util.getNodesByClass(
                "vtkMRMLVolumeRenderingDisplayNode"):
            node.Modified()

    def _destroy_eye_views(self):
        """Tear down so that no displayable manager outlives its nodes.

        Order matters: the views are removed while their widgets still have
        the scene (they release their pipelines cleanly), then the cameras,
        and only then are the widgets detached and deleted. Detaching first
        leaves camera managers alive without a scene, which logs errors and
        recreates the camera nodes.
        """
        self._unshare_restricted_display_nodes()
        self._set_main_view(None)
        if self._window:
            self._window.hide()
        scene = slicer.mrmlScene
        for node in self._eye_view_nodes.values():
            if scene.IsNodePresent(node):
                scene.RemoveNode(node)
        slicer.app.processEvents()
        _remove_eye_cameras(force=True)
        for widget in self._eye_widgets.values():
            widget.setMRMLScene(None)
        if self._window:
            self._window.deleteLater()
        slicer.app.processEvents()
        self._window = None
        self._eye_widgets = {}
        self._eye_view_nodes = {}
        self._eye_camera_nodes = {}
        self._last_inputs = None

    def _set_main_view(self, node):
        if self._main_view_node is node:
            return
        if self._main_view_node and self._main_view_observer:
            self._main_view_node.RemoveObserver(self._main_view_observer)
        self._main_view_node = node
        self._main_view_observer = None
        if node:
            self._main_view_observer = node.AddObserver(
                vtk.vtkCommand.ModifiedEvent,
                lambda *_: self._sync_eye_view_nodes())
        self._sync_eye_view_nodes()

    def _sync_eye_view_nodes(self):
        """Give the eyes the look of the main 3D view, without decorations."""
        main = self._main_view_node
        for eye, node in self._eye_view_nodes.items():
            with slicer.util.NodeModify(node):
                if main:
                    for name in ("RenderMode", "UseDepthPeeling",
                                 "VolumeRenderingQuality",
                                 "VolumeRenderingSurfaceSmoothing",
                                 "AmbientShadowsVolumeOpacityThreshold",
                                 "AmbientShadowsSizeScale",
                                 "AmbientShadowsIntensityScale",
                                 "AmbientShadowsIntensityShift",
                                 "ShadowsVisibility",
                                 # Decorations, exactly as in Slicer's view:
                                 "BoxVisible", "BoxColor",
                                 "AxisLabelsVisible",
                                 "AxisLabelsCameraDependent", "LetterSize",
                                 "OrientationMarkerType",
                                 "OrientationMarkerSize",
                                 "RulerType", "RulerColor"):
                        getter = getattr(main, "Get" + name, None)
                        setter = getattr(node, "Set" + name, None)
                        if getter and setter:
                            setter(getter())
                    if hasattr(node, "SetFPSVisible"):
                        node.SetFPSVisible(False)
                if self.black_background or not main:
                    node.SetBackgroundColor(0.0, 0.0, 0.0)
                    node.SetBackgroundColor2(0.0, 0.0, 0.0)
                else:
                    node.SetBackgroundColor(main.GetBackgroundColor())
                    node.SetBackgroundColor2(main.GetBackgroundColor2())
                if not main:
                    node.SetBoxVisible(False)
                    node.SetAxisLabelsVisible(False)

    def _share_restricted_display_nodes(self):
        """Show nodes restricted to the main 3D view in the eye views too."""
        main = self._main_view_node
        if not main:
            return
        main_id = main.GetID()
        eye_ids = [node.GetID() for node in self._eye_view_nodes.values()]
        for node in slicer.util.getNodesByClass("vtkMRMLDisplayNode"):
            if node.GetNumberOfViewNodeIDs() == 0:
                continue  # already visible everywhere
            if not node.IsViewNodeIDPresent(main_id):
                continue
            for eye_id in eye_ids:
                if not node.IsViewNodeIDPresent(eye_id):
                    node.AddViewNodeID(eye_id)
                    self._added_view_ids.append((node.GetID(), eye_id))

    def _unshare_restricted_display_nodes(self):
        for node_id, eye_id in self._added_view_ids:
            node = slicer.mrmlScene.GetNodeByID(node_id)
            if node:
                node.RemoveViewNodeID(eye_id)
        self._added_view_ids = []

    # ── scene events ─────────────────────────────────────────────

    def _observe_scene(self, enable):
        scene = slicer.mrmlScene
        for tag in self._scene_observers:
            scene.RemoveObserver(tag)
        self._scene_observers = []
        if enable:
            self._scene_observers = [
                scene.AddObserver(scene.StartCloseEvent,
                                  lambda *_: self._before_scene_close()),
                scene.AddObserver(scene.EndCloseEvent,
                                  lambda *_: self._after_scene_close()),
            ]

    def _before_scene_close(self):
        if self.active:
            self._destroy_eye_views()

    def _after_scene_close(self):
        if self.active:
            qt.QTimer.singleShot(0, self._rebuild_after_close)

    def _rebuild_after_close(self):
        if self.active and not self._eye_widgets:
            self._create_eye_views()

    # ── cameras ──────────────────────────────────────────────────

    def _head_delta(self, ready):
        """Head motion since the reference pose, or None (mirror / no pose)."""
        if self.mode != "head" or not ready or not self._link.has_6dof():
            # Glasses without 6DoF tracking still show the stereo view.
            self.tracking_quality = 0
            return None
        quality, pose = self._link.pose(self.prediction_s)
        if quality and pose == PLACEHOLDER_POSE:
            quality = 0  # the SDK's placeholder before tracking has started
        if quality == 0:
            self._first_pose_time = None
        elif self._first_pose_time is None:
            self._first_pose_time = time.monotonic()
        if (quality and self._reference_pose is None and
                time.monotonic() - self._first_pose_time < REFERENCE_SETTLE_S):
            quality = 0  # let tracking settle before taking the reference
        if quality == 2 and self.tracking_quality != 2:
            # The SDK moves its origin when tracking becomes stable: start
            # again from the neutral pose rather than from a stale reference.
            self._reference_pose = None
            self._last_delta = None
        if quality != self.tracking_quality:
            self.tracking_quality = quality
            self._notify()
        if quality == 0:
            return self._last_delta
        matrix = stereo_math.pose_matrix(pose)
        if matrix is None:
            return self._last_delta
        if self._reference_pose is None:
            self._reference_pose = matrix
        delta = stereo_math.head_delta(self._reference_pose, matrix)
        if self._last_delta is not None and _jumped(self._last_delta, delta):
            # Keep showing the last view: choose the reference that maps the
            # new pose onto the previous head offset.
            self._reference_pose = matrix @ np.linalg.inv(self._last_delta)
            return self._last_delta
        if self._last_delta is not None and not _moved(self._last_delta, delta):
            return self._last_delta
        self._last_delta = delta
        return delta

    def _update_eyes(self, ready):
        if not self._eye_widgets:
            return
        if not all(self._eye_camera_nodes.get(eye) for eye in ("left", "right")):
            cameras = slicer.modules.cameras.logic()
            for eye, node in self._eye_view_nodes.items():
                self._eye_camera_nodes[eye] = _eye_camera(cameras, node)
            if not all(self._eye_camera_nodes.values()):
                return
        if self._window and (self._eye_widgets["left"].width
                             != max(2, self._window.width) // 2
                             or self._eye_widgets["left"].height
                             != self._window.height):
            self._layout_eyes()
        main_view = self._main_view()
        if main_view is not self._main_view_node:
            self._set_main_view(main_view)
        if not main_view:
            return
        main_camera = slicer.modules.cameras.logic().GetViewActiveCameraNode(
            main_view)
        if not main_camera:
            return
        camera = main_camera.GetCamera()
        delta = self._head_delta(ready)

        left = self._eye_widgets["left"]
        aspect = max(left.width, 1) / float(max(left.height, 1))
        inputs = (camera.GetPosition(), camera.GetFocalPoint(),
                  camera.GetViewUp(), camera.GetViewAngle(),
                  camera.GetParallelProjection(), camera.GetParallelScale(),
                  aspect, None if delta is None else delta.tobytes())
        if inputs == self._last_inputs:
            return  # nothing moved: do not re-render
        self._last_inputs = inputs

        parallel = bool(camera.GetParallelProjection())
        eyes = stereo_math.eye_cameras(
            camera.GetPosition(), camera.GetFocalPoint(), camera.GetViewUp(),
            camera.GetViewAngle(), aspect,
            ipd_mm=self.ipd, virtual_distance_mm=self.virtual_distance,
            depth=self.depth, delta=delta,
            translate=self.position_tracking,
            parallel_scale=camera.GetParallelScale() if parallel else None)

        for eye in ("left", "right"):
            position, focal_point, view_up, shift = eyes[eye]
            node = self._eye_camera_nodes[eye]
            eye_camera = node.GetCamera()
            with slicer.util.NodeModify(node):
                node.SetPosition(*position)
                node.SetFocalPoint(*focal_point)
                node.SetViewUp(*view_up)
                eye_camera.SetViewAngle(camera.GetViewAngle())
                eye_camera.SetParallelProjection(parallel)
                eye_camera.SetParallelScale(camera.GetParallelScale())
                eye_camera.SetWindowCenter(float(shift), 0.0)
            node.ResetClippingRange()
        self._render_eyes()

    def _render_eyes(self):
        """Draw both eyes now, in the same tick, so they never show
        different moments of a movement."""
        start = time.perf_counter()
        for widget in self._eye_widgets.values():
            widget.threeDView().forceRender()
        elapsed = (time.perf_counter() - start) * 1000.0
        self._frame_ms = 0.8 * self._frame_ms + 0.2 * elapsed \
            if self._frame_ms else elapsed
        self._frame_times.append(time.monotonic())


def _moved(previous, current):
    """True when the head moved more than sensor noise between two deltas."""
    change = np.linalg.inv(previous) @ current
    cosine = (np.trace(change[:3, :3]) - 1.0) / 2.0
    angle = float(np.arccos(np.clip(cosine, -1.0, 1.0)))
    shift = float(np.linalg.norm(change[:3, 3]))
    return angle > MIN_ROTATION_RAD or shift > MIN_TRANSLATION_M


def _jumped(previous, current):
    """True for a change no head can make between two samples."""
    change = np.linalg.inv(previous) @ current
    cosine = (np.trace(change[:3, :3]) - 1.0) / 2.0
    angle = float(np.arccos(np.clip(cosine, -1.0, 1.0)))
    shift = float(np.linalg.norm(change[:3, 3]))
    return angle > JUMP_ROTATION_RAD or shift > JUMP_TRANSLATION_M


def _eye_camera(cameras_logic, view_node):
    """The camera of an eye view, kept out of saved scenes and node lists."""
    camera = cameras_logic.GetViewActiveCameraNode(view_node)
    if camera:
        camera.SetSaveWithScene(False)
        camera.SetHideFromEditors(True)
    return camera


EYE_LAYOUT_NAMES = ("XRGlassesLeft", "XRGlassesRight")


def _exclude_eye_cameras_from_save():
    for node in slicer.util.getNodesByClass("vtkMRMLCameraNode"):
        if node.GetLayoutName() in EYE_LAYOUT_NAMES:
            node.SetSaveWithScene(False)


def _remove_eye_cameras(force=False):
    session = getattr(slicer.modules, "XRGlassesSession", None)
    if not force and session and session.active:
        return  # a new session started meanwhile and uses them
    for node in list(slicer.util.getNodesByClass("vtkMRMLCameraNode")):
        if node.GetLayoutName() in EYE_LAYOUT_NAMES:
            slicer.mrmlScene.RemoveNode(node)


# When the glasses were last asked to go back to 2D (shared by sessions).
_last_restore_time = [0.0]
