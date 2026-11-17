"""Persistent user settings of the XRGlasses module (stored with QSettings)."""

import qt

_PREFIX = "XRGlasses/"

DEFAULTS = {
    "sdkFolder": "",
    "mode": "head",             # "mirror" or "head"
    "positionTracking": True,   # head translation moves the view (head mode)
    "ipd": 63.0,                # interpupillary distance, mm
    "depth": 1.0,               # stereo strength multiplier
    "virtualDistance": 600.0,   # apparent distance of the focal point, mm
    "prediction": 30.0,         # pose prediction, ms
    "displayMode": 0,           # 0 = automatic, else an SBS display mode
    "blackBackground": True,    # black = see-through on optical glasses
    "swapEyes": False,
    "showToolbar": True,
    "cameraRotate180": False,   # turn the camera images upside down
}


def get(key):
    default = DEFAULTS[key]
    value = qt.QSettings().value(_PREFIX + key, default)
    if value is None:
        return default
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return str(value)


def set(key, value):  # noqa: A001 - mirrors QSettings naming
    qt.QSettings().setValue(_PREFIX + key, value)


def reset_all(keep=("sdkFolder",)):
    for key, value in DEFAULTS.items():
        if key not in keep:
            set(key, value)
