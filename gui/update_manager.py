"""Secure client-side update plumbing for NakshaAI-LiDAR.

The service is deliberately configuration-driven.  Production builds must set
``updates/api_url`` in QSettings (or ``NAKSHA_UPDATE_API_URL``) and publish
signed Inno Setup installers from an HTTPS endpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import requests
from PySide6.QtCore import QSettings, QStandardPaths, QThread, Signal


APP_VERSION = "2.0.4"
PRODUCT_NAME = "NakshaAI-LiDAR"
API_URL_ENV = "NAKSHA_UPDATE_API_URL"
EXPECTED_PUBLISHER_ENV = "NAKSHA_UPDATE_PUBLISHER"
DEVICE_TOKEN_ENV = "NAKSHA_UPDATE_DEVICE_TOKEN"
SYSTEM_NUMBER_ENV = "NAKSHA_SYSTEM_NUMBER"


class UpdateError(RuntimeError):
    """A safe, user-displayable update failure."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    download_url: str
    sha256: str
    size: int
    release_notes: str
    mandatory: bool = False
    installer_args: str = (
        "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS "
        "/RESTARTAPPLICATIONS /RESTARTAFTERUPDATE"
    )


def _version_key(value: str) -> tuple[int, ...]:
    clean = str(value).strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    try:
        return tuple(int(part) for part in clean.split("."))
    except (TypeError, ValueError):
        raise UpdateError(f"Invalid update version: {value!r}") from None


def _machine_guid() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except OSError:
        return ""


class UpdateManager:
    """Checks, downloads, verifies, and launches update installers."""

    def __init__(self, settings: Optional[QSettings] = None):
        self.settings = settings or QSettings("NakshaAI", "LidarApp")
        self._last_update: Optional[UpdateInfo] = None
        self._downloaded_installer: Optional[Path] = None

    @property
    def api_url(self) -> str:
        value = os.getenv(API_URL_ENV) or self.settings.value(
            "updates/api_url", "", type=str
        )
        return str(value or "").strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.device_token)

    @property
    def device_token(self) -> str:
        return (
            os.getenv(DEVICE_TOKEN_ENV)
            or self.settings.value("updates/device_token", "", type=str)
            or ""
        ).strip()

    @property
    def device_id(self) -> str:
        value = self.settings.value("updates/device_id", "", type=str).strip()
        if not value:
            value = str(uuid.uuid4())
            self.settings.setValue("updates/device_id", value)
            self.settings.sync()
        return value

    @property
    def system_number(self) -> str:
        return (
            os.getenv(SYSTEM_NUMBER_ENV)
            or self.settings.value("updates/system_number", "", type=str)
            or ""
        ).strip()

    @property
    def hardware_fingerprint(self) -> str:
        # Only the hash leaves the device; raw hardware values are never sent.
        source = f"{_machine_guid()}|{socket.gethostname()}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @property
    def channel(self) -> str:
        value = self.settings.value("updates/channel", "stable", type=str)
        return value if value in {"stable", "testing"} else "stable"

    def set_preferences(self, auto_check: bool, auto_download: bool, channel: str):
        self.settings.setValue("updates/auto_check", bool(auto_check))
        self.settings.setValue("updates/auto_download", bool(auto_download))
        self.settings.setValue(
            "updates/channel", channel if channel in {"stable", "testing"} else "stable"
        )
        self.settings.sync()

    def check_for_update(self) -> Optional[UpdateInfo]:
        if not self.configured:
            raise UpdateError("The update service URL has not been configured.")
        parsed_api = urlparse(self.api_url)
        if parsed_api.scheme != "https" and parsed_api.hostname not in {
            "localhost", "127.0.0.1"
        }:
            raise UpdateError("The update service must use HTTPS.")

        endpoint = urljoin(self.api_url + "/", "api/v1/updates/check")
        payload = {
            "product": PRODUCT_NAME,
            "current_version": APP_VERSION,
            "device_id": self.device_id,
            "system_number": self.system_number,
            "hardware_fingerprint": self.hardware_fingerprint,
            "channel": self.channel,
        }
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.device_token}"},
                timeout=(5, 20),
            )
        except requests.RequestException as exc:
            raise UpdateError(f"Could not contact the update service: {exc}") from exc

        if response.status_code == 204:
            self._last_update = None
            return None
        if response.status_code != 200:
            raise UpdateError(
                f"Update service returned HTTP {response.status_code}."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise UpdateError("The update service returned invalid JSON.") from exc

        if not data.get("update_available", True):
            self._last_update = None
            return None
        info = self._parse_manifest(data)
        if _version_key(info.version) <= _version_key(APP_VERSION):
            self._last_update = None
            return None
        self._last_update = info
        self._downloaded_installer = None
        return info

    def _parse_manifest(self, data: dict) -> UpdateInfo:
        try:
            version = str(data["version"]).strip()
            download_url = str(data["download_url"]).strip()
            digest = str(data["sha256"]).strip().lower()
            size = int(data["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdateError("The update manifest is missing required fields.") from exc
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise UpdateError("The update manifest contains an invalid SHA-256 hash.")
        if size <= 0:
            raise UpdateError("The update manifest contains an invalid package size.")
        parsed = urlparse(download_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise UpdateError("The installer download must use HTTPS.")
        return UpdateInfo(
            version=version,
            download_url=download_url,
            sha256=digest,
            size=size,
            release_notes=str(data.get("release_notes", "No release notes provided.")),
            mandatory=bool(data.get("mandatory", False)),
        )

    def download_update(
        self, info: UpdateInfo, progress: Optional[Callable[[int], None]] = None
    ) -> Path:
        base = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
        target_dir = base / "updates" / info.version
        target_dir.mkdir(parents=True, exist_ok=True)
        partial = target_dir / "NakshaAI-LiDAR_Setup.exe.part"
        final = target_dir / "NakshaAI-LiDAR_Setup.exe"
        digest = hashlib.sha256()
        received = 0
        try:
            with requests.get(info.download_url, stream=True, timeout=(10, 120)) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > info.size:
                            raise UpdateError("Downloaded package is larger than expected.")
                        output.write(chunk)
                        digest.update(chunk)
                        if progress:
                            progress(min(100, int(received * 100 / info.size)))
        except (OSError, requests.RequestException) as exc:
            partial.unlink(missing_ok=True)
            raise UpdateError(f"Update download failed: {exc}") from exc
        if received != info.size or digest.hexdigest() != info.sha256:
            partial.unlink(missing_ok=True)
            raise UpdateError("Update verification failed: size or SHA-256 mismatch.")
        partial.replace(final)
        self.verify_authenticode(final)
        self._downloaded_installer = final
        if progress:
            progress(100)
        return final

    def verify_authenticode(self, installer: Path):
        if os.name != "nt":
            raise UpdateError("Installer signature verification requires Windows.")
        script = (
            "$s=Get-AuthenticodeSignature -LiteralPath $args[0];"
            "$o=[ordered]@{Status=[string]$s.Status;"
            "Subject=[string]$s.SignerCertificate.Subject};"
            "$o|ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, str(installer)],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            signature = json.loads(result.stdout.strip())
        except (ValueError, TypeError) as exc:
            raise UpdateError("Could not verify the installer signature.") from exc
        if signature.get("Status") != "Valid":
            installer.unlink(missing_ok=True)
            raise UpdateError("The installer does not have a valid Windows signature.")
        expected = os.getenv(EXPECTED_PUBLISHER_ENV) or self.settings.value(
            "updates/expected_publisher", "", type=str
        )
        if expected and expected.casefold() not in signature.get("Subject", "").casefold():
            installer.unlink(missing_ok=True)
            raise UpdateError("The installer publisher does not match NakshaAI.")

    def launch_installer(self, installer: Optional[Path] = None) -> None:
        path = Path(installer or self._downloaded_installer or "")
        if not path.is_file():
            raise UpdateError("No verified update installer is ready.")
        self.verify_authenticode(path)
        if os.name != "nt":
            raise UpdateError("Updates can currently be installed only on Windows.")
        result = __import__("ctypes").windll.shell32.ShellExecuteW(
            None,
            "runas",
            str(path),
            self._last_update.installer_args if self._last_update else "",
            str(path.parent),
            1,
        )
        if result <= 32:
            raise UpdateError("Windows did not start the update installer.")


class UpdateWorker(QThread):
    """Run update network and disk operations away from the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)
    progress_changed = Signal(int)

    def __init__(self, manager: UpdateManager, operation: str, info=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.operation = operation
        self.info = info

    def run(self):
        try:
            if self.operation == "check":
                result = self.manager.check_for_update()
            elif self.operation == "download":
                if self.info is None:
                    raise UpdateError("No update has been selected for download.")
                result = self.manager.download_update(
                    self.info, self.progress_changed.emit
                )
            else:
                raise UpdateError(f"Unsupported update operation: {self.operation}")
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
