"""Connection to the glasses: find, start, watch, and release them.

A background thread waits for the glasses on USB, starts the SDK, watches
the cable and shuts the SDK down cleanly when the glasses are unplugged,
then waits again. Slicer's GUI thread reads the pose and changes the display
mode; a lock shared with the teardown keeps those calls from ever touching a
device that is being destroyed.

ctypes releases Python's interpreter lock during every SDK call, so slow USB
operations in the thread never stall Slicer.
"""

import ctypes
import threading
import time

import numpy as np

from . import usb
from .sdk import DEVICE_TYPE_6DOF

GLASSES_VENDOR_ID = 0x35CA

# Values of DeviceLink.state().
STOPPED, SEARCHING, INITIALIZING, READY, UNSUPPORTED, ERROR = range(6)

# Display modes (from the SDK's protocol header).
MODE_1080_60, MODE_SBS_1080_60 = 0x31, 0x32
MODE_1080_90, MODE_1080_120, MODE_SBS_1080_90 = 0x33, 0x34, 0x35
MODE_1200_60, MODE_SBS_1200_60 = 0x41, 0x42
MODE_1200_90, MODE_1200_120, MODE_SBS_1200_90 = 0x43, 0x44, 0x45

SBS_MODES = {
    MODE_SBS_1080_60: "3840x1080 @ 60 Hz",
    MODE_SBS_1080_90: "3840x1080 @ 90 Hz",
    MODE_SBS_1200_60: "3840x1200 @ 60 Hz",
    MODE_SBS_1200_90: "3840x1200 @ 90 Hz",
}
FLAT_MODE_FOR_SBS = {
    MODE_SBS_1080_60: MODE_1080_60, MODE_SBS_1080_90: MODE_1080_90,
    MODE_SBS_1200_60: MODE_1200_60, MODE_SBS_1200_90: MODE_1200_90,
}
SBS_MODE_FOR_FLAT = {
    MODE_1080_60: MODE_SBS_1080_60, MODE_1080_90: MODE_SBS_1080_60,
    MODE_1080_120: MODE_SBS_1080_60, MODE_1200_60: MODE_SBS_1200_60,
    MODE_1200_90: MODE_SBS_1200_60, MODE_1200_120: MODE_SBS_1200_60,
}

POLL_S = 0.25
RETRY_S = 1.0

# Tracking cameras of 6DoF glasses, as delivered by the SDK's camera callback.
CAMERA_LEFT, CAMERA_RIGHT = 0, 1

_CAMERA_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_double, ctypes.c_int, ctypes.c_int)


class DeviceLink:
    def __init__(self, sdk):
        self._sdk = sdk
        self._thread = None
        self._running = threading.Event()
        self._lifecycle = threading.Lock()   # start()/stop()
        self._device = threading.Lock()      # the handle and calls on it
        self._handle = None
        self._state = STOPPED
        self._generation = 0
        self._last_error = 0
        self._product_id = 0
        self._device_type = -1
        self._name = ""
        self._firmware = ""
        self._pose = (ctypes.c_float * 7)()
        self._pose_status = ctypes.c_int(1)
        # Tracking-camera frames. The callback runs on an SDK thread; it only
        # copies when someone is watching. Its ctypes object must outlive
        # every device handle, so it is created once and kept here.
        self._camera_lock = threading.Lock()
        self._camera_wanted = set()
        self._camera_frames = {}          # channel -> uint8 array (h, w)
        self._camera_counts = {}          # channel -> frames received
        self._camera_callback = _CAMERA_CALLBACK(self._on_camera_frame)

    # ── lifecycle ────────────────────────────────────────────────

    def start(self):
        with self._lifecycle:
            if self._running.is_set():
                return True
            if self._thread is not None:
                self._thread.join()
            self._sdk.set_log_level(1)
            self._running.set()
            self._state = SEARCHING
            self._thread = threading.Thread(
                target=self._run, name="XRGlassesDevice", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        with self._lifecycle:
            self._running.clear()
            if self._thread is not None:
                self._thread.join(timeout=10.0)
                self._thread = None
            self._state = STOPPED

    def _sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while self._running.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _find(self):
        return usb.find_product_id(
            GLASSES_VENDOR_ID,
            lambda pid: self._sdk.is_product_id_valid(pid) == 1)

    def _run(self):
        sdk = self._sdk
        while self._running.is_set():
            product_id = self._find()
            if product_id is None:
                self._product_id = 0
                self._state = SEARCHING
                self._sleep(POLL_S)
                continue
            self._product_id = product_id
            self._state = INITIALIZING
            handle = sdk.create(product_id)
            if not handle:
                self._fail(-1000)
                continue
            self._device_type = sdk.get_device_type(handle)
            result = sdk.initialize(handle, None, None)
            if result == 0 and self._device_type == DEVICE_TYPE_6DOF:
                # Only the camera callback is wanted; pose is polled.
                sdk.register_callbacks_carina(
                    handle, None, None, None,
                    ctypes.cast(self._camera_callback, ctypes.c_void_p))
            if result == 0:
                result = sdk.start(handle)
            if result != 0:
                _destroy(sdk, handle)
                self._fail(result)
                continue

            name = sdk.market_name(product_id)
            firmware = sdk.firmware(handle)
            with self._device:
                self._handle = handle
                self._name, self._firmware = name, firmware
            self._last_error = 0
            self._generation += 1
            self._state = READY

            # Stay connected until the cable is removed or we are stopped.
            # A pose that is briefly "unstable" never ends a session.
            while self._running.is_set() and self._find() == product_id:
                self._sleep(POLL_S)

            self._state = SEARCHING if self._running.is_set() else STOPPED
            with self._device:
                self._handle = None
                _destroy(sdk, handle)
        self._product_id = 0
        self._state = STOPPED

    def _fail(self, code):
        self._last_error = code
        self._state = ERROR
        self._sleep(RETRY_S)

    # ── queries (GUI thread) ─────────────────────────────────────

    def state(self):
        return self._state

    def generation(self):
        return self._generation

    def last_error(self):
        return self._last_error

    def device_name(self):
        return self._name

    def firmware(self):
        return self._firmware

    def has_6dof(self):
        return self._device_type == DEVICE_TYPE_6DOF

    def pose(self, predict_seconds=0.0):
        """Return (quality, [px, py, pz, qw, qx, qy, qz]).

        quality: 2 stable, 1 tracking still settling, 0 no pose. Position in
        metres; axes x right, y up, z backward. Never blocks: while the
        device is being torn down it answers 0.
        """
        if not self._device.acquire(blocking=False):
            return 0, list(self._pose)
        try:
            if self._handle is None or self._device_type != DEVICE_TYPE_6DOF:
                return 0, list(self._pose)
            if self._sdk.get_gl_pose_carina(
                    self._handle, self._pose, float(predict_seconds),
                    ctypes.byref(self._pose_status)) != 0:
                return 0, list(self._pose)
            return (2 if self._pose_status.value == 0 else 1), list(self._pose)
        finally:
            self._device.release()

    # ── tracking cameras ─────────────────────────────────────────

    def has_tracking_cameras(self):
        return self._device_type == DEVICE_TYPE_6DOF

    def watch_camera(self, channel, wanted):
        """Start or stop keeping frames of a tracking camera."""
        with self._camera_lock:
            if wanted:
                self._camera_wanted.add(channel)
            else:
                self._camera_wanted.discard(channel)
                self._camera_frames.pop(channel, None)

    def camera_frame(self, channel):
        """(frame count, copy of the latest frame or None)."""
        with self._camera_lock:
            frame = self._camera_frames.get(channel)
            return (self._camera_counts.get(channel, 0),
                    None if frame is None else frame.copy())

    def _on_camera_frame(self, left0, right0, _left1, _right1, _timestamp,
                         width, height):
        # Runs on an SDK thread. Keep it short and never raise.
        try:
            if not self._camera_wanted or width <= 0 or height <= 0:
                return
            with self._camera_lock:
                for channel, pointer in ((CAMERA_LEFT, left0),
                                         (CAMERA_RIGHT, right0)):
                    if channel not in self._camera_wanted or not pointer:
                        continue
                    source = np.ctypeslib.as_array(
                        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint8)),
                        shape=(height, width))
                    frame = self._camera_frames.get(channel)
                    if frame is None or frame.shape != source.shape:
                        frame = np.empty_like(source)
                        self._camera_frames[channel] = frame
                    np.copyto(frame, source)
                    self._camera_counts[channel] = \
                        self._camera_counts.get(channel, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    def display_mode(self):
        with self._device:
            if self._handle is None:
                return -1
            return self._sdk.get_display_mode(self._handle)

    def set_display_mode(self, mode):
        with self._device:
            if self._handle is None:
                return False
            return self._sdk.set_display_mode(self._handle, int(mode)) == 0


def _destroy(sdk, handle):
    sdk.stop(handle)
    sdk.shutdown(handle)
    sdk.destroy(handle)
