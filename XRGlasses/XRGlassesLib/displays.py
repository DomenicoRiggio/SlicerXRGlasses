"""Find the glasses among the computer's displays, on any platform.

Display names differ by platform: macOS uses the product name,
Windows a generic device path ("\\\\.\\DISPLAY2"), Linux the connector
("DP-2"). The EDID manufacturer and model strings that Qt exposes are
checked as well, and as a last resort a single extra display is assumed to
be the glasses once they are connected over USB.
"""

import platform

import qt

# Substrings of display name, manufacturer or model that identify the glasses.
GLASSES_DISPLAY_HINTS = ("viture", "luma")


def _text(screen, attribute):
    value = getattr(screen, attribute, "")
    if callable(value):
        try:
            value = value()
        except Exception:  # noqa: BLE001
            value = ""
    return str(value or "").lower()


def describe(screen):
    return " / ".join(part for part in (
        _text(screen, "name"), _text(screen, "manufacturer"),
        _text(screen, "model")) if part)


def glasses_screen(main_window, glasses_connected):
    """The QScreen of the glasses, or None."""
    screens = list(qt.QGuiApplication.screens())
    for screen in screens:
        text = describe(screen)
        if any(hint in text for hint in GLASSES_DISPLAY_HINTS):
            return screen
    main_screen = main_window.screen() if main_window else None
    others = [screen for screen in screens if screen != main_screen]
    # Only guess when it cannot be the user's ordinary second monitor.
    if glasses_connected and len(others) == 1:
        return others[0]
    return None


def reveal_hidden_glasses():
    """macOS: put glasses that macOS hid as a headset back on the desktop."""
    if platform.system() != "Darwin":
        return 0
    from . import macdisplay
    return macdisplay.reveal_hidden_glasses()
