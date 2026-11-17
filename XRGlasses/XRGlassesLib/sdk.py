"""Load the glasses vendor SDK from a folder chosen by the user.

The SDK is a plain C library with the same API on every platform; only the
file names differ. It is loaded with ctypes, so nothing has to be compiled:
the user downloads the SDK for their platform and points the module at it.

Expected layout (the user may select either level):

    <sdk>/include/...
    <sdk>/aarch64/libglasses.dylib  libcarina_vio.dylib         (macOS)
    <sdk>/x86_64/glasses.dll        carina_vio.dll  ...         (Windows)
    <sdk>/x86_64|aarch64/libglasses.so  ...                     (Linux)
"""

import ctypes
import glob
import os
import platform

MINIMUM_VERSION = (2, 4, 2)

# Device families reported by xr_device_provider_get_device_type().
DEVICE_TYPE_GEN1, DEVICE_TYPE_GEN2, DEVICE_TYPE_6DOF = 0, 1, 2


class SdkError(RuntimeError):
    """The SDK folder cannot be used; the message is shown to the user."""


def system():
    name = platform.system()
    return {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(
        name, name.lower())


def architecture():
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return machine


def library_file_name():
    return {"macos": "libglasses.dylib", "windows": "glasses.dll",
            "linux": "libglasses.so"}.get(system(), "libglasses.so")


def find_library_folder(sdk_folder):
    """Folder holding the main SDK library for this machine, or None."""
    if not sdk_folder:
        return None
    sdk_folder = os.path.abspath(os.path.expanduser(sdk_folder))
    name = library_file_name()
    candidates = [
        sdk_folder,
        os.path.join(sdk_folder, architecture()),
        os.path.join(sdk_folder, "lib"),
        os.path.join(sdk_folder, "lib", architecture()),
    ]
    for folder in candidates:
        if os.path.isfile(os.path.join(folder, name)):
            return folder
    return None


def describe_problem(sdk_folder):
    """A user-facing explanation of why sdk_folder is not usable, or ''."""
    if not sdk_folder:
        return "Select the glasses SDK folder."
    if not os.path.isdir(sdk_folder):
        return "The selected SDK folder does not exist."
    if find_library_folder(sdk_folder) is None:
        return (f"No {library_file_name()} for {system()} {architecture()} "
                "was found in this folder. Select the SDK downloaded for "
                "this computer.")
    return ""


def _preload_dependencies(folder):
    """Load the SDK's companion libraries so the main one can find them."""
    if system() == "windows":
        # Windows searches this folder for the DLLs glasses.dll depends on.
        os.add_dll_directory(folder)
        return
    pattern = "*.dylib" if system() == "macos" else "*.so*"
    pending = [path for path in sorted(glob.glob(os.path.join(folder, pattern)))
               if os.path.basename(path) != library_file_name()
               and os.path.isfile(path)]
    # Load in whatever order works: retry while progress is being made.
    while pending:
        failed = []
        for path in pending:
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                failed.append(path)
        if len(failed) == len(pending):
            break
        pending = failed


class GlassesSdk:
    """The loaded SDK with typed function prototypes."""

    def __init__(self, sdk_folder):
        problem = describe_problem(sdk_folder)
        if problem:
            raise SdkError(problem)
        folder = find_library_folder(sdk_folder)
        self.folder = folder
        try:
            _preload_dependencies(folder)
            mode = getattr(ctypes, "RTLD_GLOBAL", 0)
            lib = ctypes.CDLL(os.path.join(folder, library_file_name()),
                              mode=mode)
        except OSError as error:
            raise SdkError(_load_error_message(folder, error)) from error
        self._lib = lib
        self._declare(lib)
        self.version_text = self._version_text()
        self.version = _parse_version(self.version_text)
        if self.version and self.version < MINIMUM_VERSION:
            raise SdkError(
                f"SDK {self.version_text} is too old; version "
                f"{'.'.join(map(str, MINIMUM_VERSION))} or newer is needed.")

    @staticmethod
    def _declare(lib):
        c_int, c_void_p, c_char_p = ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p
        handle = c_void_p
        signatures = {
            "xr_device_provider_is_product_id_valid": (c_int, [c_int]),
            "xr_device_provider_create": (handle, [c_int]),
            "xr_device_provider_initialize": (c_int, [handle, c_char_p, c_char_p]),
            "xr_device_provider_start": (c_int, [handle]),
            "xr_device_provider_stop": (c_int, [handle]),
            "xr_device_provider_shutdown": (c_int, [handle]),
            "xr_device_provider_destroy": (None, [handle]),
            "xr_device_provider_get_device_type": (c_int, [handle]),
            "xr_device_provider_get_gl_pose_carina": (
                c_int, [handle, ctypes.POINTER(ctypes.c_float),
                        ctypes.c_double, ctypes.POINTER(c_int)]),
            "xr_device_provider_get_display_mode": (c_int, [handle]),
            "xr_device_provider_set_display_mode": (c_int, [handle, c_int]),
            "xr_device_provider_get_market_name": (
                c_int, [c_int, c_char_p, ctypes.POINTER(c_int)]),
            "xr_device_provider_get_glasses_version": (
                c_int, [handle, c_char_p, ctypes.POINTER(c_int)]),
            "xr_device_provider_set_log_level": (None, [c_int]),
            # Callbacks are passed as plain pointers (None = not wanted).
            "xr_device_provider_register_callbacks_carina": (
                c_int, [handle, c_void_p, c_void_p, c_void_p, c_void_p]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(lib, name)
            function.restype = restype
            function.argtypes = argtypes
        version = getattr(lib, "GetVersionString", None)
        if version is not None:
            version.restype = c_char_p
            version.argtypes = []

    def _version_text(self):
        function = getattr(self._lib, "GetVersionString", None)
        if function is None:
            return "unknown"
        value = function()
        return value.decode("ascii", "replace") if value else "unknown"

    def __getattr__(self, name):
        # sdk.create(...) -> xr_device_provider_create(...)
        return getattr(self._lib, "xr_device_provider_" + name)

    def market_name(self, product_id):
        buffer = ctypes.create_string_buffer(128)
        length = ctypes.c_int(len(buffer) - 1)
        if self._lib.xr_device_provider_get_market_name(
                product_id, buffer, ctypes.byref(length)) != 0:
            return ""
        return buffer.value.decode("utf-8", "replace").strip()

    def firmware(self, handle):
        buffer = ctypes.create_string_buffer(128)
        length = ctypes.c_int(len(buffer) - 1)
        if self._lib.xr_device_provider_get_glasses_version(
                handle, buffer, ctypes.byref(length)) != 0:
            return ""
        return buffer.value.decode("utf-8", "replace").strip()


def _parse_version(text):
    try:
        return tuple(int(part) for part in text.split(".")[:3])
    except ValueError:
        return None


def _load_error_message(folder, error):
    message = f"The glasses SDK could not be loaded: {error}"
    if system() == "macos" and _is_quarantined(folder):
        message += ("\n\nmacOS has quarantined the downloaded SDK. Use "
                    "'Allow this SDK' in the module panel, then try again.")
    elif system() == "linux":
        message += ("\n\nCheck that the SDK matches this computer "
                    f"({architecture()}).")
    return message


def _is_quarantined(folder):
    try:
        import subprocess
        result = subprocess.run(
            ["/usr/bin/xattr", "-p", "com.apple.quarantine",
             os.path.join(folder, library_file_name())],
            capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def is_quarantined(sdk_folder):
    folder = find_library_folder(sdk_folder)
    return bool(folder) and system() == "macos" and _is_quarantined(folder)


def remove_quarantine(sdk_folder):
    """macOS only: clear the download quarantine of the SDK library folder."""
    import subprocess
    folder = find_library_folder(sdk_folder)
    if not folder or system() != "macos":
        return False
    result = subprocess.run(
        ["/usr/bin/xattr", "-dr", "com.apple.quarantine", folder],
        capture_output=True, text=True, timeout=30)
    return result.returncode == 0
