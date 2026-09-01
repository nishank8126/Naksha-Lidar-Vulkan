"""
License security for the distributable DGN conversion package.

This module keeps the same on-disk state shape as the desktop app so existing
licenses continue to work, while lifting the base conversion limit to 30000.
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

BASE_LIMIT    = 30_000   # org-wide total
OFFLINE_LIMIT = 500      # per-machine grace when no LAN peers found

# ── LAN sync internals ────────────────────────────────────────────────────────
_LAN_PORT  = 47291
_PKT_MAGIC = 0x4E4B5354  # "NKST"
_CMD_QUERY = 0x51
_CMD_RESP  = 0x52
_CMD_UPD   = 0x53
_PKT_SIZE  = 13          # 4+1+4+4


def _key_xor_byte(key: str) -> int:
    v = 0
    for c in key:
        v = (v * 31 + ord(c)) & 0xFF
    return v or 0x5A


def _key_fingerprint(key: str) -> int:
    v = 0
    for c in key:
        v = (v * 1000003 + ord(c)) & 0xFFFFFFFF
    return v


def _pack_pkt(cmd: int, count: int, fp: int, xb: int) -> bytes:
    raw = struct.pack("<IBII", _PKT_MAGIC, cmd, count, fp)
    return bytes(b ^ xb for b in raw)


def _unpack_pkt(data: bytes, fp: int, xb: int):
    if len(data) < _PKT_SIZE:
        return None
    dec = bytes(b ^ xb for b in data[:_PKT_SIZE])
    magic, cmd, count, pkt_fp = struct.unpack("<IBII", dec)
    if magic != _PKT_MAGIC or pkt_fp != fp:
        return None
    return cmd, count


class _LanSync:
    def __init__(self) -> None:
        self._lock           = threading.Lock()
        self._count          = 0
        self._xb             = 0x5A
        self._fp             = 0
        self._sock           = None
        self._active         = False
        self._last_peer_time = 0.0

    def start(self, key: str, initial_count: int) -> Tuple[bool, int]:
        """Returns (is_online, synced_count). Blocks ~200 ms for peer query."""
        self._xb = _key_xor_byte(key)
        self._fp = _key_fingerprint(key)
        with self._lock:
            self._count = initial_count

        max_c, found = self._query_peers()
        if found:
            self._last_peer_time = time.monotonic()
            with self._lock:
                if max_c > self._count:
                    self._count = max_c

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(0.1)
            s.bind(("", _LAN_PORT))
            self._sock   = s
            self._active = True
            t = threading.Thread(target=self._run, daemon=True)
            t.start()
        except Exception:
            pass

        return found, self.get_count()

    def is_online(self) -> bool:
        return (time.monotonic() - self._last_peer_time) < 300

    def get_count(self) -> int:
        with self._lock:
            return self._count

    def broadcast_update(self, count: int) -> None:
        with self._lock:
            self._count = count
        if not self._sock or not self._active:
            return
        try:
            self._sock.sendto(
                _pack_pkt(_CMD_UPD, count, self._fp, self._xb),
                ("<broadcast>", _LAN_PORT),
            )
        except Exception:
            pass

    def _run(self) -> None:
        while self._active:
            try:
                data, addr = self._sock.recvfrom(64)
                parsed = _unpack_pkt(data, self._fp, self._xb)
                if parsed is None:
                    continue
                cmd, count = parsed
                if cmd == _CMD_QUERY:
                    with self._lock:
                        cur = self._count
                    self._sock.sendto(
                        _pack_pkt(_CMD_RESP, cur, self._fp, self._xb), addr
                    )
                elif cmd in (_CMD_RESP, _CMD_UPD):
                    self._last_peer_time = time.monotonic()
                    with self._lock:
                        if count > self._count:
                            self._count = count
            except socket.timeout:
                pass
            except Exception:
                pass

    def _query_peers(self) -> Tuple[int, bool]:
        max_c, found = 0, False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(0.05)
            s.bind(("", 0))
            s.sendto(_pack_pkt(_CMD_QUERY, 0, self._fp, self._xb), ("<broadcast>", _LAN_PORT))
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                try:
                    data, _ = s.recvfrom(64)
                    p = _unpack_pkt(data, self._fp, self._xb)
                    if p and p[0] == _CMD_RESP:
                        found = True
                        max_c = max(max_c, p[1])
                except socket.timeout:
                    pass
            s.close()
        except Exception:
            pass
        return max_c, found


_lan = _LanSync()

# ── DRM helpers ───────────────────────────────────────────────────────────────

def _drm_magic() -> int:
    return 0x4E414B53  # "NAKS"


def _drm_xor_key() -> int:
    """Consistent per-machine key — survives process restarts and reinstalls."""
    # Windows: derive from immutable MachineGuid
    try:
        if os.name == "nt":
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            )
            guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            winreg.CloseKey(k)
            h = 0
            for c in guid:
                h = (h * 31 + ord(c)) & 0xFF
            return (h ^ 0xA5) & 0xFF or 0x5A
    except Exception:
        pass
    # Fallback: stable hash of hostname + username
    try:
        import getpass
        seed = socket.gethostname() + getpass.getuser()
        h = 0
        for c in seed:
            h = (h * 31 + ord(c)) & 0xFF
        return (h ^ 0xA5) & 0xFF or 0x5A
    except Exception:
        pass
    return 0x7E


def _xor(data: bytes) -> bytes:
    key = _drm_xor_key()
    return bytes(b ^ key for b in data)


# ── State ─────────────────────────────────────────────────────────────────────

class DgnState:
    _FMT_V1 = "<III16s"
    _FMT_V2 = "<III16sI"
    SIZE_V1  = struct.calcsize(_FMT_V1)   # 28
    SIZE_V2  = struct.calcsize(_FMT_V2)   # 32

    def __init__(self) -> None:
        self.magic          = _drm_magic()
        self.current_count  = 0
        self.current_limit  = BASE_LIMIT
        self.last_key       = "NAKSHA0001"
        self.offline_buffer = 0

    def to_bytes(self) -> bytes:
        return struct.pack(
            self._FMT_V2,
            self.magic,
            self.current_count,
            self.current_limit,
            self.last_key.encode().ljust(16, b"\x00"),
            self.offline_buffer,
        )

    @staticmethod
    def from_bytes(data: bytes) -> "DgnState":
        state = DgnState()
        try:
            if len(data) >= DgnState.SIZE_V2:
                magic, count, limit, key_b, off_buf = struct.unpack(
                    DgnState._FMT_V2, data[: DgnState.SIZE_V2]
                )
                state.offline_buffer = off_buf
            else:
                magic, count, limit, key_b = struct.unpack(
                    DgnState._FMT_V1, data[: DgnState.SIZE_V1]
                )
                state.offline_buffer = 0
            state.magic         = magic
            state.current_count = count
            state.current_limit = max(limit, BASE_LIMIT)
            state.last_key      = key_b.rstrip(b"\x00").decode("utf-8", errors="ignore")
        except Exception:
            pass
        return state

    def valid(self) -> bool:
        return self.magic == _drm_magic()


# ── Multi-location persistence (survives uninstall) ───────────────────────────

def _primary_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) / "NakshaApp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "naksha_license.bin"


def _alt_paths() -> List[Path]:
    """Hidden locations that survive pip uninstall."""
    paths: List[Path] = []
    prog = os.environ.get("PROGRAMDATA", "")
    if prog:
        paths.append(Path(prog) / "Microsoft" / "Windows" / ".fntcache")
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        paths.append(Path(local) / "Microsoft" / "Windows" / "Caches" / ".nkc")
    return paths


def _reg_save(data: bytes) -> None:
    if os.name != "nt":
        return
    try:
        import winreg
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\InputMethod\Settings\CHS",
        )
        winreg.SetValueEx(key, "FontCacheVersion", 0, winreg.REG_BINARY, data)
        winreg.CloseKey(key)
    except Exception:
        pass


def _reg_load() -> Optional[bytes]:
    if os.name != "nt":
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\InputMethod\Settings\CHS",
        )
        data, _ = winreg.QueryValueEx(key, "FontCacheVersion")
        winreg.CloseKey(key)
        return bytes(data)
    except Exception:
        return None


def _load_state() -> DgnState:
    candidates: List[DgnState] = []

    def _try(raw: bytes) -> None:
        try:
            s = DgnState.from_bytes(_xor(raw))
            if s.valid():
                candidates.append(s)
        except Exception:
            pass

    p = _primary_path()
    if p.exists():
        _try(p.read_bytes())

    for ap in _alt_paths():
        if ap.exists():
            try:
                _try(ap.read_bytes())
            except Exception:
                pass

    reg = _reg_load()
    if reg:
        _try(reg)

    if candidates:
        return max(candidates, key=lambda s: s.current_count)
    return DgnState()


def _save_state(state: DgnState) -> None:
    data = _xor(state.to_bytes())

    try:
        _primary_path().write_bytes(data)
    except Exception:
        pass

    for ap in _alt_paths():
        try:
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_bytes(data)
        except Exception:
            pass

    _reg_save(data)


def calculate_next_key(current: str) -> str:
    result = []
    for char in current:
        if "0" <= char <= "9":
            result.append(str((int(char) + 6) % 10))
        elif "A" <= char <= "Z":
            result.append(chr((ord(char) - ord("A") + 1) % 26 + ord("A")))
        elif "a" <= char <= "z":
            result.append(chr((ord(char) - ord("a") + 1) % 26 + ord("a")))
        else:
            result.append(char)
    return "".join(result)


# ── License manager ───────────────────────────────────────────────────────────

class DgnLicenseManager:
    def __init__(self) -> None:
        self._state = _load_state()
        try:
            is_online, net_count = _lan.start(
                self._state.last_key, self._state.current_count
            )
            if is_online:
                self._flush_offline_buffer(net_count)
        except Exception:
            pass

    def get_remaining_conversions(self) -> int:
        if _lan.is_online():
            return max(0, self._state.current_limit - self._state.current_count)
        return max(0, OFFLINE_LIMIT - self._state.offline_buffer)

    def get_status_message(self) -> str:
        if self.validate_conversion():
            return f"{self.get_remaining_conversions():,} conversions remaining"
        return "License limit reached - activate a new key"

    def validate_conversion(self) -> bool:
        try:
            if _lan.is_online():
                self._sync_org_count()
                return self._state.current_count < self._state.current_limit
            else:
                return self._state.offline_buffer < OFFLINE_LIMIT
        except Exception:
            return self._state.current_count < self._state.current_limit

    def record_conversion(self) -> bool:
        if not self.validate_conversion():
            return False
        try:
            if _lan.is_online():
                self._state.current_count += 1
                _save_state(self._state)
                _lan.broadcast_update(self._state.current_count)
            else:
                self._state.offline_buffer += 1
                _save_state(self._state)
        except Exception:
            self._state.current_count += 1
            _save_state(self._state)
        return True

    def activate_key(self, key: str) -> Tuple[bool, str]:
        clean    = "".join(char for char in key if char > " ")[:15]
        expected = calculate_next_key(self._state.last_key)
        if clean == expected:
            self._state.current_limit += 1000
            self._state.last_key       = clean
            _save_state(self._state)
            return True, f"Key activated. {self.get_remaining_conversions():,} conversions available."
        return False, "Invalid key. Please check and try again."

    @property
    def state(self) -> DgnState:
        return self._state

    def _sync_org_count(self) -> None:
        net = _lan.get_count()
        if self._state.offline_buffer > 0:
            self._flush_offline_buffer(net)
        elif net > self._state.current_count:
            self._state.current_count = net
            _save_state(self._state)

    def _flush_offline_buffer(self, net_count: int) -> None:
        merged = max(net_count, self._state.current_count) + self._state.offline_buffer
        self._state.current_count  = merged
        self._state.offline_buffer = 0
        _save_state(self._state)
        _lan.broadcast_update(merged)


# ── UI status helper (no new manager instance — reuses existing LAN singleton) ─

def get_dot_status() -> int:
    """Returns 0, 1, or 2 for UI indicator. Lightweight — no LAN init."""
    try:
        state = _load_state()
        if _lan.is_online():
            return 1 if state.current_count < state.current_limit else 2
        return 0 if state.offline_buffer < OFFLINE_LIMIT else 2
    except Exception:
        return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="NakshaApp DGN license tool")
    parser.add_argument("--activate-key", default="", help="Activate license with key")
    args = parser.parse_args()

    manager = DgnLicenseManager()
    if args.activate_key:
        ok, message = manager.activate_key(args.activate_key)
        print(message)
        raise SystemExit(0 if ok else 1)

    print(manager.get_status_message())
    print(f"Org limit : {manager.state.current_limit:,}")
    print(f"Org used  : {manager.state.current_count:,}")
    if manager.state.offline_buffer:
        print(f"Offline   : {manager.state.offline_buffer} (pending sync)")
    print(f"Next key hint: {calculate_next_key(manager.state.last_key)[0]}********")


if __name__ == "__main__":
    main()
