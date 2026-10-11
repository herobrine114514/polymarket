"""Local model selection and per-user encrypted DeepSeek credential storage."""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


PROVIDERS = ("codex", "deepseek")
DEEPSEEK_MODEL = os.getenv("POLYCAST_DEEPSEEK_MODEL", "deepseek-v4-pro")


def active_provider(data_dir: Path) -> str:
    path = data_dir / "provider.json"
    if not path.exists():
        return "codex"
    try:
        provider = json.loads(path.read_text(encoding="utf-8"))["provider"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid local provider selection") from exc
    if provider not in PROVIDERS:
        raise ValueError("Unsupported provider in local configuration")
    return provider


def select_provider(data_dir: Path, provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("Unsupported model provider")
    if provider == "deepseek" and not has_deepseek_key(data_dir):
        raise ValueError("DeepSeek API key is not configured")
    data_dir.mkdir(parents=True, exist_ok=True)
    destination = data_dir / "provider.json"
    temporary = data_dir / "provider.json.tmp"
    temporary.write_text(json.dumps({"provider": provider}), encoding="utf-8")
    temporary.replace(destination)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Encrypted key storage requires Windows; use POLYCAST_DEEPSEEK_API_KEY")
    source_buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    crypt.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.restype = wintypes.BOOL
    ok = crypt(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result))
    if not ok:
        raise RuntimeError("Windows could not access the encrypted DeepSeek key")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(result.pbData)


def save_deepseek_key(data_dir: Path, key: str) -> None:
    if not key.startswith("sk-") or len(key) < 20 or any(ch.isspace() for ch in key):
        raise ValueError("Invalid DeepSeek API key format")
    data_dir.mkdir(parents=True, exist_ok=True)
    destination = data_dir / "deepseek.key.dpapi"
    temporary = data_dir / "deepseek.key.dpapi.tmp"
    temporary.write_bytes(_protect(key.encode("utf-8")))
    temporary.replace(destination)


def has_deepseek_key(data_dir: Path) -> bool:
    return bool(os.getenv("POLYCAST_DEEPSEEK_API_KEY") or
                (data_dir / "deepseek.key.dpapi").is_file())


def deepseek_key(data_dir: Path) -> str:
    from_env = os.getenv("POLYCAST_DEEPSEEK_API_KEY")
    if from_env:
        return from_env
    try:
        return _protect((data_dir / "deepseek.key.dpapi").read_bytes(), decrypt=True).decode("utf-8")
    except OSError as exc:
        raise RuntimeError("DeepSeek API key is not configured") from exc
