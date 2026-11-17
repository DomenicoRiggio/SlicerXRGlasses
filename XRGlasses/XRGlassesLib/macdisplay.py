"""Make the glasses part of the macOS desktop while they are in 3D mode.

When the glasses switch to a side-by-side mode they re-announce themselves
to macOS as a head-mounted display. macOS then treats them as an "independent
output": the display is powered and receives a signal, but it is removed from
the desktop, so no window can be placed on it and it disappears from every
screen list. The stereo window needs an ordinary desktop display, so this
module asks the window server to turn the independent-output flag off.

The call lives in SkyLight, a private macOS framework; there is no public
equivalent. Everything here fails soft: if the symbols are missing on a future
macOS, reveal_hidden_glasses() returns 0 and the caller reports the problem.
"""

import ctypes
import ctypes.util
import logging

LOG = logging.getLogger("XRGlasses")

GLASSES_EDID_VENDOR = 0x0ED4
_SESSION_ONLY = 1  # kCGConfigureForSession: nothing is written to disk

_libraries = {}


def _load():
    if not _libraries:
        try:
            cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
            sl = ctypes.CDLL("/System/Library/PrivateFrameworks/"
                             "SkyLight.framework/SkyLight")
            sl.SLSConfigureDisplayIndependentOutput.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_bool]
            sl.SLSConfigureDisplayIndependentOutput.restype = ctypes.c_int
            sl.SLSDisplaySupportsIndependentOutput.argtypes = [ctypes.c_uint32]
            sl.SLSDisplaySupportsIndependentOutput.restype = ctypes.c_bool
            sl.SLSGetDisplayList.restype = ctypes.c_int
            cg.CGGetOnlineDisplayList.restype = ctypes.c_int
            cg.CGDisplayVendorNumber.argtypes = [ctypes.c_uint32]
            cg.CGDisplayVendorNumber.restype = ctypes.c_uint32
            cg.CGBeginDisplayConfiguration.argtypes = [
                ctypes.POINTER(ctypes.c_void_p)]
            cg.CGCompleteDisplayConfiguration.argtypes = [
                ctypes.c_void_p, ctypes.c_int]
            cg.CGCancelDisplayConfiguration.argtypes = [ctypes.c_void_p]
            _libraries["cg"], _libraries["sl"] = cg, sl
        except (OSError, AttributeError, TypeError) as error:
            LOG.warning("XRGlasses: display helper unavailable: %s", error)
            _libraries["cg"] = _libraries["sl"] = None
    return _libraries["cg"], _libraries["sl"]


def _display_ids(function):
    ids = (ctypes.c_uint32 * 32)()
    count = ctypes.c_uint32(0)
    if function(32, ids, ctypes.byref(count)) != 0:
        return []
    return [ids[index] for index in range(count.value)]


def hidden_glasses_displays():
    """IDs of displays that macOS keeps off the desktop as HMD outputs."""
    cg, sl = _load()
    if not cg or not sl:
        return []
    online = set(_display_ids(cg.CGGetOnlineDisplayList))
    hidden = []
    for display in _display_ids(sl.SLSGetDisplayList):
        if display in online:
            continue
        if not sl.SLSDisplaySupportsIndependentOutput(display):
            continue
        vendor = cg.CGDisplayVendorNumber(display)
        if vendor in (0, 0xFFFFFFFF, GLASSES_EDID_VENDOR):
            hidden.append(display)
    return hidden


def reveal_hidden_glasses():
    """Put hidden glasses displays back on the desktop. Returns how many."""
    cg, sl = _load()
    displays = hidden_glasses_displays()
    if not displays:
        return 0
    config = ctypes.c_void_p()
    if cg.CGBeginDisplayConfiguration(ctypes.byref(config)) != 0:
        return 0
    revealed = 0
    for display in displays:
        if sl.SLSConfigureDisplayIndependentOutput(
                config, display, False) == 0:
            revealed += 1
    if revealed == 0:
        cg.CGCancelDisplayConfiguration(config)
        return 0
    if cg.CGCompleteDisplayConfiguration(config, _SESSION_ONLY) != 0:
        return 0
    LOG.info("XRGlasses: added the glasses display to the macOS desktop")
    return revealed
