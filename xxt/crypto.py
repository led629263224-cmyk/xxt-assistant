"""Windows DPAPI 封装。

用当前 Windows 用户的密钥加密本地凭据：密文只在这台机器、这个用户下能解开，
复制到别的机器或别的用户下都解不开，因此 config.json 即使被看到也拿不到密码。
额外绑了一段固定的附加熵（entropy），进一步避免被同机器上其他程序直接解密。
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

_ENTROPY = b"xxt-assistant/credential/v1"
_DESCR = "学习通小助手凭据"

if sys.platform == "win32":  # pragma: no cover - 平台相关
    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_BLOB)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_BLOB)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL
else:  # pragma: no cover
    _BLOB = None
    _crypt32 = None
    _kernel32 = None


class CryptoError(RuntimeError):
    """DPAPI 调用失败。"""


def _make_blob(data: bytes):
    """构造 DATA_BLOB，同时返回底层 buffer 以保持引用（防被 GC 回收）。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _read_blob(blob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            _kernel32.LocalFree(blob.pbData)


def available() -> bool:
    """当前平台是否支持 DPAPI。"""
    return _crypt32 is not None


def protect(plaintext: str) -> str:
    """加密明文，返回 base64 字符串（可直接写进 JSON）。"""
    if not available():
        raise CryptoError("DPAPI 仅在 Windows 上可用")
    blob_in, _keep1 = _make_blob(plaintext.encode("utf-8"))
    entropy, _keep2 = _make_blob(_ENTROPY)
    blob_out = _BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in), _DESCR, ctypes.byref(entropy),
        None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise CryptoError(f"CryptProtectData 失败（错误码 {ctypes.get_last_error()}）")
    return base64.b64encode(_read_blob(blob_out)).decode("ascii")


def unprotect(token: str) -> str:
    """解密 protect() 产生的 base64 字符串。"""
    if not available():
        raise CryptoError("DPAPI 仅在 Windows 上可用")
    try:
        raw = base64.b64decode(token)
    except Exception as exc:
        raise CryptoError("凭据格式损坏（base64 解码失败）") from exc
    blob_in, _keep1 = _make_blob(raw)
    entropy, _keep2 = _make_blob(_ENTROPY)
    blob_out = _BLOB()
    descr = wintypes.LPWSTR()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), ctypes.byref(descr), ctypes.byref(entropy),
        None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise CryptoError(
            f"CryptUnprotectData 失败（错误码 {ctypes.get_last_error()}）"
            "：凭据可能是在另一台机器/另一个 Windows 用户下加密的")
    if descr:
        _kernel32.LocalFree(descr)
    return _read_blob(blob_out).decode("utf-8")
