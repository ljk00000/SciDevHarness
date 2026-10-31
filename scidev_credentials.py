"""Windows Credential Manager access for provider API keys.

Secrets never enter the project settings file or the event ledger.  On
non-Windows hosts the wrapper reports that secure storage is unavailable
instead of silently writing a plaintext key.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

TARGET_PREFIX = "SciDevHarness/provider"
USER_NAME = "scidev"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CredentialError(RuntimeError):
    pass


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTE)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def credential_target(profile: str = "default") -> str:
    cleaned = "".join(char for char in profile if char.isalnum() or char in "-_")[:60]
    return f"{TARGET_PREFIX}/{cleaned or 'default'}"


def secure_storage_available() -> bool:
    return os.name == "nt"


def _advapi32():  # noqa: ANN202 - ctypes function bundle
    if not secure_storage_available():
        raise CredentialError("Windows 凭据管理器只在 Windows 上可用")
    global _ADVAPI32
    if _ADVAPI32 is None:
        _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _ADVAPI32.CredFree.argtypes = [ctypes.c_void_p]
        _ADVAPI32.CredFree.restype = None
    return _ADVAPI32


_ADVAPI32: ctypes.WinDLL | None = None


def set_api_key(api_key: str, profile: str = "default") -> None:
    if not api_key:
        raise CredentialError("API Key 不能为空")
    advapi = _advapi32()
    target = credential_target(profile)
    blob = api_key.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _CREDENTIAL(
        Flags=0,
        Type=CRED_TYPE_GENERIC,
        TargetName=target,
        Comment="SciDevHarness provider API key",
        LastWritten=_FILETIME(),
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=USER_NAME,
    )
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    if not advapi.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialError(f"写入 Windows 凭据管理器失败（WinError {ctypes.get_last_error()}）")


def get_api_key(profile: str = "default") -> str:
    advapi = _advapi32()
    target = credential_target(profile)
    pointer = ctypes.POINTER(_CREDENTIAL)()
    advapi.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    advapi.CredReadW.restype = wintypes.BOOL
    if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return ""
        raise CredentialError(f"读取 Windows 凭据管理器失败（WinError {error}）")
    try:
        credential = pointer.contents
        if not credential.CredentialBlob or not credential.CredentialBlobSize:
            return ""
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
    finally:
        advapi.CredFree(pointer)


def delete_api_key(profile: str = "default") -> bool:
    advapi = _advapi32()
    target = credential_target(profile)
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    if advapi.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"删除 Windows 凭据失败（WinError {error}）")
