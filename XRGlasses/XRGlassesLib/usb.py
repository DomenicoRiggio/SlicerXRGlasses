"""List USB devices (vendor id, product id) with each platform's own API.

The glasses' control interface uses USB bulk transfer, not HID, so the
operating system's device registry is the reliable way to see whether they
are plugged in: IOKit on macOS, sysfs on Linux, SetupAPI on Windows.
"""

import ctypes
import ctypes.util
import glob
import os
import platform
import re


def usb_devices():
    """Return a set of (vendor_id, product_id) for connected USB devices."""
    name = platform.system()
    try:
        if name == "Darwin":
            return _macos_devices()
        if name == "Linux":
            return _linux_devices()
        if name == "Windows":
            return _windows_devices()
    except Exception:  # noqa: BLE001 - never break the caller's loop
        pass
    return set()


def find_product_id(vendor_id, is_valid):
    """First connected product id of vendor_id accepted by is_valid(pid)."""
    for vendor, product in sorted(usb_devices()):
        if vendor == vendor_id and is_valid(product):
            return product
    return None


# ── Linux ────────────────────────────────────────────────────────────

def _linux_devices():
    devices = set()
    for folder in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(folder, "idVendor")) as file:
                vendor = int(file.read().strip(), 16)
            with open(os.path.join(folder, "idProduct")) as file:
                product = int(file.read().strip(), 16)
        except (OSError, ValueError):
            continue
        devices.add((vendor, product))
    return devices


# ── Windows ──────────────────────────────────────────────────────────

_HARDWARE_ID = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)


def _windows_devices():
    from ctypes import wintypes

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

    class SP_DEVINFO_DATA(ctypes.Structure):  # noqa: N801 - Win32 name
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("ClassGuid", ctypes.c_byte * 16),
                    ("DevInst", wintypes.DWORD),
                    ("Reserved", ctypes.c_void_p)]

    DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
    SPDRP_HARDWAREID = 0x1
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

    info_set = setupapi.SetupDiGetClassDevsW(
        None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if not info_set or info_set == INVALID_HANDLE:
        return set()
    devices = set()
    try:
        index = 0
        while True:
            data = SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not setupapi.SetupDiEnumDeviceInfo(info_set, index,
                                                  ctypes.byref(data)):
                break
            index += 1
            buffer = ctypes.create_unicode_buffer(1024)
            if not setupapi.SetupDiGetDeviceRegistryPropertyW(
                    info_set, ctypes.byref(data), SPDRP_HARDWAREID, None,
                    buffer, ctypes.sizeof(buffer), None):
                continue
            match = _HARDWARE_ID.search(buffer.value)
            if match:
                devices.add((int(match.group(1), 16), int(match.group(2), 16)))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return devices


# ── macOS ────────────────────────────────────────────────────────────

_mac = {}


def _mac_libraries():
    if not _mac:
        iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        void_p, c_uint = ctypes.c_void_p, ctypes.c_uint32
        iokit.IOServiceMatching.restype = void_p
        iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
        iokit.IOServiceGetMatchingServices.argtypes = [
            c_uint, void_p, ctypes.POINTER(c_uint)]
        iokit.IOIteratorNext.restype = c_uint
        iokit.IOIteratorNext.argtypes = [c_uint]
        iokit.IOObjectRelease.argtypes = [c_uint]
        iokit.IORegistryEntryCreateCFProperty.restype = void_p
        iokit.IORegistryEntryCreateCFProperty.argtypes = [
            c_uint, void_p, void_p, c_uint]
        cf.CFStringCreateWithCString.restype = void_p
        cf.CFStringCreateWithCString.argtypes = [
            void_p, ctypes.c_char_p, c_uint]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [void_p, ctypes.c_int, void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [void_p]
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFRelease.argtypes = [void_p]
        utf8 = 0x08000100
        _mac.update(
            iokit=iokit, cf=cf,
            vendor_key=cf.CFStringCreateWithCString(None, b"idVendor", utf8),
            product_key=cf.CFStringCreateWithCString(None, b"idProduct", utf8),
            number_type=cf.CFNumberGetTypeID())
    return _mac


def _mac_number(service, key):
    m = _mac_libraries()
    value = m["iokit"].IORegistryEntryCreateCFProperty(service, key, None, 0)
    if not value:
        return None
    try:
        if m["cf"].CFGetTypeID(value) != m["number_type"]:
            return None
        result = ctypes.c_int32(0)
        kCFNumberSInt32Type = 3
        if not m["cf"].CFNumberGetValue(value, kCFNumberSInt32Type,
                                        ctypes.byref(result)):
            return None
        return result.value
    finally:
        m["cf"].CFRelease(value)


def _macos_devices():
    m = _mac_libraries()
    devices = set()
    for class_name in (b"IOUSBHostDevice", b"IOUSBDevice"):
        matching = m["iokit"].IOServiceMatching(class_name)
        if not matching:
            continue
        iterator = ctypes.c_uint32(0)
        # The call consumes the matching dictionary.
        if m["iokit"].IOServiceGetMatchingServices(
                0, matching, ctypes.byref(iterator)) != 0:
            continue
        try:
            while True:
                service = m["iokit"].IOIteratorNext(iterator.value)
                if not service:
                    break
                vendor = _mac_number(service, m["vendor_key"])
                product = _mac_number(service, m["product_key"])
                m["iokit"].IOObjectRelease(service)
                if vendor is not None and product is not None:
                    devices.add((vendor, product))
        finally:
            m["iokit"].IOObjectRelease(iterator.value)
        if devices:
            break
    return devices
