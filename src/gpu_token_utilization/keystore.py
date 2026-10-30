"""The API key in Windows Credential Manager, saved by `gtu setkey`.

Credential Manager keeps it encrypted and readable only by the current Windows account,
and it never sits in an environment variable, so subprocesses (model-written tests, the
BharatCode CLI) can't see it. Talks to advapi32 through ctypes; no extra dependency.
"""

from __future__ import annotations

import ctypes
import os
import sys

TARGET_ENV = "GTU_CREDENTIAL_TARGET"  # tests point this at a throwaway entry
DEFAULT_TARGET = "spare-gpu/BHARATCODE_API_KEY"
LOCATION = "Windows Credential Manager"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # survives logoff and reboot; this user, this machine only
ERROR_NOT_FOUND = 1168


class KeystoreError(Exception):
    """Credential Manager refused a read, write or delete."""


def supported() -> bool:
    return sys.platform == "win32"


def target() -> str:
    return os.environ.get(TARGET_ENV, "").strip() or DEFAULT_TARGET


if sys.platform == "win32":
    from ctypes import wintypes

    class _Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_Credential))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _advapi.CredFree.restype = None


def load() -> str | None:
    """The saved key, or None if there isn't one (or this isn't Windows)."""
    if not supported():
        return None
    found = ctypes.POINTER(_Credential)()
    if not _advapi.CredReadW(target(), CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
        err = ctypes.get_last_error()
        if err == ERROR_NOT_FOUND:
            return None
        raise KeystoreError(f"could not read the saved key from {LOCATION} (Windows error {err})")
    try:
        cred = found.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        _advapi.CredFree(found)
    return blob.decode("utf-8").strip() or None


def save(key: str) -> None:
    if not supported():
        raise KeystoreError(f"{LOCATION} is only available on Windows")
    data = key.encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = _Credential()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target()
    cred.Comment = "BharatCode API key, saved by spare-gpu (gtu setkey)"
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "spare-gpu"
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise KeystoreError(f"could not save the key in {LOCATION} (Windows error {ctypes.get_last_error()})")


def delete() -> bool:
    """Remove the saved key. False if there was none."""
    if not supported():
        return False
    if _advapi.CredDeleteW(target(), CRED_TYPE_GENERIC, 0):
        return True
    err = ctypes.get_last_error()
    if err == ERROR_NOT_FOUND:
        return False
    raise KeystoreError(f"could not delete the saved key from {LOCATION} (Windows error {err})")
