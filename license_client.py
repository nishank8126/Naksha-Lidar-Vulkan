# # import os
# # import json
# # import uuid
# # import socket
# # import getpass
# # import hashlib
# # import platform
# # from pathlib import Path
# # from typing import Optional

# # import requests
# # import tkinter as tk
# # from tkinter import simpledialog, messagebox


# # SERVER_URL = "http://192.168.10.34:8000"
# # CURRENT_VERSION = "1.0.2"

# # APP_DATA_DIR = Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "NakshaTech" / "NakshaAI-LiDAR"
# # LICENSE_FILE = APP_DATA_DIR / "license.json"


# # def get_windows_machine_guid() -> str:
# #     try:
# #         import winreg

# #         key = winreg.OpenKey(
# #             winreg.HKEY_LOCAL_MACHINE,
# #             r"SOFTWARE\Microsoft\Cryptography"
# #         )
# #         value, _ = winreg.QueryValueEx(key, "MachineGuid")
# #         return value
# #     except Exception:
# #         return ""


# # def get_mac_address() -> str:
# #     mac = uuid.getnode()
# #     return ":".join(
# #         f"{(mac >> shift) & 0xff:02x}"
# #         for shift in range(40, -1, -8)
# #     )


# # def get_local_ip() -> str:
# #     try:
# #         sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
# #         sock.connect(("8.8.8.8", 80))
# #         local_ip = sock.getsockname()[0]
# #         sock.close()
# #         return local_ip
# #     except Exception:
# #         return "unknown"


# # def get_machine_id() -> str:
# #     computer_name = socket.gethostname()
# #     machine_guid = get_windows_machine_guid()
# #     mac_address = get_mac_address()

# #     raw_value = f"{machine_guid}|{mac_address}|{computer_name}"
# #     return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


# # def get_machine_info(license_key: str) -> dict:
# #     return {
# #         "license_key": license_key,
# #         "machine_id": get_machine_id(),
# #         "computer_name": socket.gethostname(),
# #         "windows_user": getpass.getuser(),
# #         "os_version": platform.platform(),
# #         "local_ip": get_local_ip(),
# #         "mac_address": get_mac_address(),
# #         "software_version": CURRENT_VERSION
# #     }


# # def save_license_data(data: dict) -> None:
# #     APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

# #     with LICENSE_FILE.open("w", encoding="utf-8") as file:
# #         json.dump(data, file, indent=4)


# # def load_license_data() -> Optional[dict]:
# #     if not LICENSE_FILE.exists():
# #         return None

# #     try:
# #         with LICENSE_FILE.open("r", encoding="utf-8") as file:
# #             return json.load(file)
# #     except Exception:
# #         return None


# # def activate_license(license_key: str) -> bool:
# #     try:
# #         machine_info = get_machine_info(license_key)

# #         response = requests.post(
# #             f"{SERVER_URL}/api/activate",
# #             json=machine_info,
# #             timeout=20
# #         )

# #         if response.status_code != 200:
# #             messagebox.showerror(
# #                 "License Activation Failed",
# #                 response.text
# #             )
# #             return False

# #         result = response.json()

# #         save_license_data({
# #             "license_key": license_key,
# #             "machine_id": machine_info["machine_id"],
# #             "activation_token": result.get("activation_token")
# #         })

# #         messagebox.showinfo(
# #             "License Activated",
# #             "NakshaAI-LiDAR activated successfully."
# #         )

# #         return True

# #     except Exception as error:
# #         messagebox.showerror(
# #             "Server Connection Failed",
# #             f"Could not connect to license server.\n\n{error}"
# #         )
# #         return False


# # def check_license() -> bool:
# #     license_data = load_license_data()

# #     if not license_data:
# #         return False

# #     license_key = license_data.get("license_key")

# #     if not license_key:
# #         return False

# #     try:
# #         machine_info = get_machine_info(license_key)

# #         response = requests.post(
# #             f"{SERVER_URL}/api/check-license",
# #             json=machine_info,
# #             timeout=20
# #         )

# #         return response.status_code == 200

# #     except Exception:
# #         return False


# # def ask_for_license_key() -> Optional[str]:
# #     root = tk.Tk()
# #     root.withdraw()

# #     license_key = simpledialog.askstring(
# #         "NakshaAI-LiDAR Activation",
# #         "Enter your NakshaAI-LiDAR activation key:"
# #     )

# #     root.destroy()

# #     if not license_key:
# #         return None

# #     return license_key.strip()


# # def require_valid_license() -> bool:
# #     if check_license():
# #         return True

# #     license_key = ask_for_license_key()

# #     if not license_key:
# #         messagebox.showerror(
# #             "Activation Required",
# #             "Activation key is required to open NakshaAI-LiDAR."
# #         )
# #         return False

# #     return activate_license(license_key)


# #### OM DUM DURGAYE NAMAHA ####

# import os
# import json
# import uuid
# import time
# import socket
# import getpass
# import hashlib
# import platform
# import threading
# from pathlib import Path
# from typing import Optional

# import requests
# import tkinter as tk
# from tkinter import simpledialog, messagebox


# SERVER_URL = "https://192.168.10.34:8443"
# CURRENT_VERSION = "1.0.3"

# APP_DATA_DIR = Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "NakshaTech" / "NakshaAI-LiDAR"
# LICENSE_FILE = APP_DATA_DIR / "license.json"

# _HEARTBEAT_STARTED = False


# def get_windows_machine_guid() -> str:
#     try:
#         import winreg

#         key = winreg.OpenKey(
#             winreg.HKEY_LOCAL_MACHINE,
#             r"SOFTWARE\Microsoft\Cryptography"
#         )
#         value, _ = winreg.QueryValueEx(key, "MachineGuid")
#         return value
#     except Exception:
#         return ""


# def get_mac_address() -> str:
#     mac = uuid.getnode()
#     return ":".join(
#         f"{(mac >> shift) & 0xff:02x}"
#         for shift in range(40, -1, -8)
#     )


# def get_local_ip() -> str:
#     try:
#         sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         sock.connect(("8.8.8.8", 80))
#         local_ip = sock.getsockname()[0]
#         sock.close()
#         return local_ip
#     except Exception:
#         return "unknown"


# def get_machine_id() -> str:
#     computer_name = socket.gethostname()
#     machine_guid = get_windows_machine_guid()
#     mac_address = get_mac_address()

#     raw_value = f"{machine_guid}|{mac_address}|{computer_name}"
#     return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


# def get_machine_info(
#     license_key: str,
#     user_name: Optional[str] = None,
#     user_email: Optional[str] = None,
#     organization: Optional[str] = None
# ) -> dict:
#     return {
#         "license_key": license_key,
#         "machine_id": get_machine_id(),
#         "computer_name": socket.gethostname(),
#         "windows_user": getpass.getuser(),
#         "os_version": platform.platform(),
#         "local_ip": get_local_ip(),
#         "mac_address": get_mac_address(),
#         "software_version": CURRENT_VERSION,
#         "user_name": user_name,
#         "user_email": user_email,
#         "organization": organization
#     }


# def save_license_data(data: dict) -> None:
#     APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

#     with LICENSE_FILE.open("w", encoding="utf-8") as file:
#         json.dump(data, file, indent=4)


# def load_license_data() -> Optional[dict]:
#     if not LICENSE_FILE.exists():
#         return None

#     try:
#         with LICENSE_FILE.open("r", encoding="utf-8") as file:
#             return json.load(file)
#     except Exception:
#         return None


# def ask_required_text(title: str, prompt: str) -> Optional[str]:
#     root = tk.Tk()
#     root.withdraw()

#     value = simpledialog.askstring(title, prompt)

#     root.destroy()

#     if not value:
#         return None

#     return value.strip()


# def ask_for_license_key() -> Optional[str]:
#     return ask_required_text(
#         "NakshaAI-LiDAR Activation",
#         "Enter your NakshaAI-LiDAR activation key:"
#     )


# def activate_license(license_key: str) -> bool:
#     try:
#         user_name = ask_required_text("User Details", "Enter your name:")
#         if not user_name:
#             messagebox.showerror("Required", "Name is required.")
#             return False

#         user_email = ask_required_text("User Details", "Enter your email:")
#         if not user_email:
#             messagebox.showerror("Required", "Email is required.")
#             return False

#         organization = ask_required_text("User Details", "Enter your organization:")
#         if not organization:
#             messagebox.showerror("Required", "Organization is required.")
#             return False

#         machine_info = get_machine_info(
#             license_key=license_key,
#             user_name=user_name,
#             user_email=user_email,
#             organization=organization
#         )

#         response = requests.post(
#             f"{SERVER_URL}/api/activate",
#             json=machine_info,
#             timeout=20
#         )

#         if response.status_code == 426:
#             messagebox.showerror(
#                 "Update Required",
#                 response.json().get("detail", response.text)
#             )
#             return False

#         if response.status_code != 200:
#             messagebox.showerror(
#                 "License Activation Failed",
#                 response.text
#             )
#             return False

#         result = response.json()

#         save_license_data({
#             "license_key": license_key,
#             "machine_id": machine_info["machine_id"],
#             "activation_token": result.get("activation_token"),
#             "user_name": user_name,
#             "user_email": user_email,
#             "organization": organization
#         })

#         messagebox.showinfo(
#             "License Activated",
#             "NakshaAI-LiDAR activated successfully."
#         )

#         return True

#     except Exception as error:
#         messagebox.showerror(
#             "Server Connection Failed",
#             f"Could not connect to license server.\n\n{error}"
#         )
#         return False


# def check_license_status() -> str:
#     license_data = load_license_data()

#     if not license_data:
#         return "not_activated"

#     license_key = license_data.get("license_key")

#     if not license_key:
#         return "not_activated"

#     try:
#         machine_info = get_machine_info(
#             license_key=license_key,
#             user_name=license_data.get("user_name"),
#             user_email=license_data.get("user_email"),
#             organization=license_data.get("organization")
#         )

#         response = requests.post(
#             f"{SERVER_URL}/api/check-license",
#             json=machine_info,
#             timeout=20
#         )

#         if response.status_code == 200:
#             return "valid"

#         if response.status_code == 426:
#             try:
#                 error_message = response.json().get("detail", response.text)
#             except Exception:
#                 error_message = response.text

#             messagebox.showerror(
#                 "Update Required",
#                 error_message
#             )
#             return "blocked"

#         return "not_activated"

#     except Exception as error:
#         messagebox.showerror(
#             "License Server Required",
#             f"Could not verify license with server.\n\n{error}"
#         )
#         return "blocked"


# def require_valid_license() -> bool:
#     status = check_license_status()

#     if status == "valid":
#         return True

#     if status == "blocked":
#         return False

#     license_key = ask_for_license_key()

#     if not license_key:
#         messagebox.showerror(
#             "Activation Required",
#             "Activation key is required to open NakshaAI-LiDAR."
#         )
#         return False

#     return activate_license(license_key)


# def _heartbeat_loop(interval_seconds: int) -> None:
#     while True:
#         time.sleep(interval_seconds)

#         license_data = load_license_data()

#         if not license_data:
#             continue

#         license_key = license_data.get("license_key")

#         if not license_key:
#             continue

#         try:
#             machine_info = get_machine_info(
#                 license_key=license_key,
#                 user_name=license_data.get("user_name"),
#                 user_email=license_data.get("user_email"),
#                 organization=license_data.get("organization")
#             )

#             response = requests.post(
#                 f"{SERVER_URL}/api/check-license",
#                 json=machine_info,
#                 timeout=20
#             )

#             if response.status_code == 426:
#                 try:
#                     error_message = response.json().get("detail", response.text)
#                 except Exception:
#                     error_message = response.text

#                 messagebox.showerror(
#                     "Update Required",
#                     error_message
#                 )

#                 os._exit(0)

#             if response.status_code not in (200, 426):
#                 os._exit(0)

#         except Exception:
#             # Do not close app immediately on temporary network drop.
#             # Next heartbeat will retry.
#             pass


# def start_license_heartbeat(interval_seconds: int = 300) -> None:
#     global _HEARTBEAT_STARTED

#     if _HEARTBEAT_STARTED:
#         return

#     _HEARTBEAT_STARTED = True

#     thread = threading.Thread(
#         target=_heartbeat_loop,
#         args=(interval_seconds,),
#         daemon=True
#     )
#     thread.start()

#### om dum durgaye namaha ####

# import os
# import sys
# import json
# import uuid
# import time
# import socket
# import getpass
# import hashlib
# import platform
# import threading
# from pathlib import Path
# from typing import Optional, Union

# import requests
# import tkinter as tk
# from tkinter import simpledialog, messagebox


# # For secure server use HTTPS.
# # If you have not created HTTPS certificate yet, temporarily use:
# # SERVER_URL = "http://192.168.10.34:8000"
# SERVER_URL = "https://192.168.10.34:8443"

# CURRENT_VERSION = "1.0.4"

# APP_DATA_DIR = Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "NakshaTech" / "NakshaAI-LiDAR"
# LICENSE_FILE = APP_DATA_DIR / "license.json"

# _HEARTBEAT_STARTED = False


# def resource_path(relative_path: str) -> Path:
#     if hasattr(sys, "_MEIPASS"):
#         return Path(sys._MEIPASS) / relative_path

#     return Path(__file__).parent / relative_path


# def get_ssl_verify_setting() -> Union[bool, str]:
#     """
#     If using public trusted certificate, return True.
#     If using internal self-signed certificate, include naksha.crt in package.
#     """
#     if not SERVER_URL.lower().startswith("https://"):
#         return True

#     cert_path = resource_path("naksha.crt")

#     if cert_path.exists():
#         return str(cert_path)

#     return True


# def get_error_text(response) -> str:
#     try:
#         data = response.json()
#         return str(data.get("detail", response.text))
#     except Exception:
#         return response.text


# def get_windows_machine_guid() -> str:
#     try:
#         import winreg

#         key = winreg.OpenKey(
#             winreg.HKEY_LOCAL_MACHINE,
#             r"SOFTWARE\Microsoft\Cryptography"
#         )
#         value, _ = winreg.QueryValueEx(key, "MachineGuid")
#         return value
#     except Exception:
#         return ""


# def get_mac_address() -> str:
#     mac = uuid.getnode()
#     return ":".join(
#         f"{(mac >> shift) & 0xff:02x}"
#         for shift in range(40, -1, -8)
#     )


# def get_local_ip() -> str:
#     try:
#         sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         sock.connect(("8.8.8.8", 80))
#         local_ip = sock.getsockname()[0]
#         sock.close()
#         return local_ip
#     except Exception:
#         return "unknown"


# def get_machine_id() -> str:
#     computer_name = socket.gethostname()
#     machine_guid = get_windows_machine_guid()
#     mac_address = get_mac_address()

#     raw_value = f"{machine_guid}|{mac_address}|{computer_name}"
#     return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


# def get_machine_info(
#     license_key: str,
#     user_name: Optional[str] = None,
#     user_email: Optional[str] = None,
#     organization: Optional[str] = None
# ) -> dict:
#     return {
#         "license_key": license_key,
#         "machine_id": get_machine_id(),
#         "computer_name": socket.gethostname(),
#         "windows_user": getpass.getuser(),
#         "os_version": platform.platform(),
#         "local_ip": get_local_ip(),
#         "mac_address": get_mac_address(),
#         "software_version": CURRENT_VERSION,
#         "user_name": user_name,
#         "user_email": user_email,
#         "organization": organization
#     }


# def save_license_data(data: dict) -> None:
#     APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

#     with LICENSE_FILE.open("w", encoding="utf-8") as file:
#         json.dump(data, file, indent=4)


# def load_license_data() -> Optional[dict]:
#     if not LICENSE_FILE.exists():
#         return None

#     try:
#         with LICENSE_FILE.open("r", encoding="utf-8") as file:
#             return json.load(file)
#     except Exception:
#         return None


# def ask_required_text(title: str, prompt: str) -> Optional[str]:
#     root = tk.Tk()
#     root.withdraw()

#     value = simpledialog.askstring(title, prompt)

#     root.destroy()

#     if not value:
#         return None

#     return value.strip()


# def ask_for_license_key() -> Optional[str]:
#     return ask_required_text(
#         "NakshaAI-LiDAR Activation",
#         "Enter your NakshaAI-LiDAR activation key:"
#     )


# def is_valid_email(email: str) -> bool:
#     return "@" in email and "." in email


# def activate_license(license_key: str) -> bool:
#     try:
#         user_name = ask_required_text("User Details", "Enter your name:")
#         if not user_name:
#             messagebox.showerror("Required", "Name is required.")
#             return False

#         user_email = ask_required_text("User Details", "Enter your email:")
#         if not user_email:
#             messagebox.showerror("Required", "Email is required.")
#             return False

#         if not is_valid_email(user_email):
#             messagebox.showerror("Invalid Email", "Please enter a valid email address.")
#             return False

#         organization = ask_required_text("User Details", "Enter your organization:")
#         if not organization:
#             messagebox.showerror("Required", "Organization is required.")
#             return False

#         machine_info = get_machine_info(
#             license_key=license_key,
#             user_name=user_name,
#             user_email=user_email,
#             organization=organization
#         )

#         response = requests.post(
#             f"{SERVER_URL}/api/activate",
#             json=machine_info,
#             timeout=20,
#             verify=get_ssl_verify_setting()
#         )

#         if response.status_code == 426:
#             messagebox.showerror(
#                 "Update Required",
#                 get_error_text(response)
#             )
#             return False

#         if response.status_code != 200:
#             messagebox.showerror(
#                 "License Activation Failed",
#                 get_error_text(response)
#             )
#             return False

#         result = response.json()

#         save_license_data({
#             "license_key": license_key,
#             "machine_id": machine_info["machine_id"],
#             "activation_token": result.get("activation_token"),
#             "user_name": user_name,
#             "user_email": user_email,
#             "organization": organization,
#             "software_version": CURRENT_VERSION
#         })

#         messagebox.showinfo(
#             "License Activated",
#             "NakshaAI-LiDAR activated successfully."
#         )

#         return True

#     except requests.exceptions.SSLError as error:
#         messagebox.showerror(
#             "Secure Connection Failed",
#             "Could not verify the secure license server certificate.\n\n"
#             "If this is an internal server, install the Naksha certificate or include naksha.crt in the package.\n\n"
#             f"{error}"
#         )
#         return False

#     except Exception as error:
#         messagebox.showerror(
#             "Server Connection Failed",
#             f"Could not connect to license server.\n\n{error}"
#         )
#         return False


# def check_license_status() -> str:
#     license_data = load_license_data()

#     if not license_data:
#         return "not_activated"

#     license_key = license_data.get("license_key")

#     if not license_key:
#         return "not_activated"

#     saved_version = license_data.get("software_version")

#     # When version changes, force user to activate again.
#     # Example: saved 1.0.2, current 1.0.3 => asks activation key again.
#     if saved_version != CURRENT_VERSION:
#         return "not_activated"

#     try:
#         machine_info = get_machine_info(
#             license_key=license_key,
#             user_name=license_data.get("user_name"),
#             user_email=license_data.get("user_email"),
#             organization=license_data.get("organization")
#         )

#         response = requests.post(
#             f"{SERVER_URL}/api/check-license",
#             json=machine_info,
#             timeout=20,
#             verify=get_ssl_verify_setting()
#         )

#         if response.status_code == 200:
#             return "valid"

#         if response.status_code == 426:
#             messagebox.showerror(
#                 "Update Required",
#                 get_error_text(response)
#             )
#             return "blocked"

#         return "not_activated"

#     except requests.exceptions.SSLError as error:
#         messagebox.showerror(
#             "Secure Connection Failed",
#             "Could not verify the secure license server certificate.\n\n"
#             "If this is an internal server, install the Naksha certificate or include naksha.crt in the package.\n\n"
#             f"{error}"
#         )
#         return "blocked"

#     except Exception as error:
#         messagebox.showerror(
#             "License Server Required",
#             f"Could not verify license with server.\n\n{error}"
#         )
#         return "blocked"


# def require_valid_license() -> bool:
#     status = check_license_status()

#     if status == "valid":
#         return True

#     if status == "blocked":
#         return False

#     license_key = ask_for_license_key()

#     if not license_key:
#         messagebox.showerror(
#             "Activation Required",
#             "Activation key is required to open NakshaAI-LiDAR."
#         )
#         return False

#     return activate_license(license_key)


# def _heartbeat_loop(interval_seconds: int) -> None:
#     while True:
#         time.sleep(interval_seconds)

#         license_data = load_license_data()

#         if not license_data:
#             continue

#         license_key = license_data.get("license_key")

#         if not license_key:
#             continue

#         try:
#             machine_info = get_machine_info(
#                 license_key=license_key,
#                 user_name=license_data.get("user_name"),
#                 user_email=license_data.get("user_email"),
#                 organization=license_data.get("organization")
#             )

#             response = requests.post(
#                 f"{SERVER_URL}/api/check-license",
#                 json=machine_info,
#                 timeout=20,
#                 verify=get_ssl_verify_setting()
#             )

#             if response.status_code == 426:
#                 messagebox.showerror(
#                     "Update Required",
#                     get_error_text(response)
#                 )
#                 os._exit(0)

#             if response.status_code not in (200, 426):
#                 os._exit(0)

#         except Exception:
#             # Temporary network drop should not immediately close the software.
#             # Next heartbeat will retry.
#             pass


# def start_license_heartbeat(interval_seconds: int = 300) -> None:
#     global _HEARTBEAT_STARTED

#     if _HEARTBEAT_STARTED:
#         return

#     _HEARTBEAT_STARTED = True

#     thread = threading.Thread(
#         target=_heartbeat_loop,
#         args=(interval_seconds,),
#         daemon=True
#     )
#     thread.start()

#### om dum durgaye namaha ####
import os
import sys
import json
import uuid
import time
import socket
import getpass
import hashlib
import platform
import threading
from pathlib import Path
from typing import Optional, Union
from datetime import datetime, timezone, timedelta

import requests
import tkinter as tk
from tkinter import simpledialog, messagebox


SERVER_URL = "https://192.168.10.89:8443"
CURRENT_VERSION = "1.0.1"

# Server OFF grace:
# Day 1-3 server off = same version opens.
# Day 4 server on = opens without key and timer resets.
# Day 4 server off = blocks until server is reachable.
OFFLINE_GRACE_DAYS = 365 

APP_DATA_DIR = Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "NakshaTech" / "NakshaAI-LiDAR"
LICENSE_FILE = APP_DATA_DIR / "license.json"

_HEARTBEAT_STARTED = False
_LAST_SERVER_HARD_BLOCK = False
_LAST_SERVER_OFFLINE_EXPIRED = False


def resource_path(relative_path: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / relative_path

    return Path(__file__).parent / relative_path


def get_ssl_verify_setting() -> Union[bool, str]:
    try:
        cert_path = resource_path("naksha.crt")
        if cert_path.exists():
            return str(cert_path)
    except Exception:
        pass

    return True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def get_error_text(response) -> str:
    try:
        data = response.json()
        return str(data.get("detail", response.text))
    except Exception:
        return response.text


def show_info(title: str, message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, message, parent=root)
        root.destroy()
    except Exception:
        pass


def show_error(title: str, message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror(title, message, parent=root)
        root.destroy()
    except Exception:
        pass


def ask_required_text(title: str, prompt: str) -> Optional[str]:
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        value = simpledialog.askstring(title, prompt, parent=root)

        root.destroy()

        if not value:
            return None

        return value.strip()
    except Exception:
        return None


def get_windows_machine_guid() -> str:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography"
        )
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return value
    except Exception:
        return ""


def get_mac_address() -> str:
    mac = uuid.getnode()
    return ":".join(
        f"{(mac >> shift) & 0xff:02x}"
        for shift in range(40, -1, -8)
    )


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
        return local_ip
    except Exception:
        return "unknown"


def get_machine_id() -> str:
    computer_name = socket.gethostname()
    machine_guid = get_windows_machine_guid()
    mac_address = get_mac_address()

    raw_value = f"{machine_guid}|{mac_address}|{computer_name}"
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def get_machine_info(
    license_key: str,
    user_name: Optional[str] = None,
    user_email: Optional[str] = None,
    organization: Optional[str] = None
) -> dict:
    return {
        "license_key": license_key,
        "machine_id": get_machine_id(),
        "computer_name": socket.gethostname(),
        "windows_user": getpass.getuser(),
        "os_version": platform.platform(),
        "local_ip": get_local_ip(),
        "mac_address": get_mac_address(),
        "software_version": CURRENT_VERSION,
        "user_name": user_name,
        "user_email": user_email,
        "organization": organization
    }


def save_license_data(data: dict) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with LICENSE_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=4)


def load_license_data() -> Optional[dict]:
    if not LICENSE_FILE.exists():
        return None

    try:
        with LICENSE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return None


def delete_saved_license() -> None:
    try:
        if LICENSE_FILE.exists():
            LICENSE_FILE.unlink()
    except Exception:
        pass


def cached_license_matches_this_machine(license_data: dict) -> bool:
    if not license_data:
        return False

    saved_machine_id = license_data.get("machine_id")
    current_machine_id = get_machine_id()

    if not saved_machine_id:
        return False

    return saved_machine_id == current_machine_id


def cached_license_has_token(license_data: dict) -> bool:
    if not license_data:
        return False

    return bool(
        license_data.get("license_key")
        and license_data.get("activation_token")
    )


def saved_license_version_matches_current(license_data: dict) -> bool:
    if not license_data:
        return False

    return license_data.get("software_version") == CURRENT_VERSION


def cached_license_still_allowed(license_data: dict) -> bool:
    if not cached_license_matches_this_machine(license_data):
        return False

    if not cached_license_has_token(license_data):
        return False

    if not saved_license_version_matches_current(license_data):
        return False

    last_success_at = parse_iso(license_data.get("last_success_at", ""))

    if last_success_at is None:
        return True

    expiry = last_success_at + timedelta(days=OFFLINE_GRACE_DAYS)

    return datetime.now(timezone.utc) <= expiry


def ask_for_license_key() -> Optional[str]:
    return ask_required_text(
        "NakshaAI-LiDAR Activation",
        "Enter your NakshaAI-LiDAR activation key:"
    )


def is_valid_email(email: str) -> bool:
    return "@" in email and "." in email


def ask_user_details() -> Optional[dict]:
    user_name = ask_required_text("User Details", "Enter your name:")
    if not user_name:
        show_error("Required", "Name is required.")
        return None

    user_email = ask_required_text("User Details", "Enter your email:")
    if not user_email:
        show_error("Required", "Email is required.")
        return None

    if not is_valid_email(user_email):
        show_error("Invalid Email", "Please enter a valid email address.")
        return None

    organization = ask_required_text("User Details", "Enter your organization:")
    if not organization:
        show_error("Required", "Organization is required.")
        return None

    return {
        "user_name": user_name,
        "user_email": user_email,
        "organization": organization
    }


def activate_license(license_key: str) -> bool:
    try:
        details = ask_user_details()
        if not details:
            return False

        machine_info = get_machine_info(
            license_key=license_key,
            user_name=details["user_name"],
            user_email=details["user_email"],
            organization=details["organization"]
        )

        response = requests.post(
            f"{SERVER_URL}/api/activate",
            json=machine_info,
            timeout=20,
            verify=get_ssl_verify_setting()
        )

        if response.status_code != 200:
            show_error("License Activation Failed", get_error_text(response))
            return False

        result = response.json()

        save_license_data({
            "license_key": license_key,
            "machine_id": machine_info["machine_id"],
            "activation_token": result.get("activation_token"),
            "software_version": CURRENT_VERSION,
            "activated_at": now_iso(),
            "last_success_at": now_iso(),
            "user_name": details["user_name"],
            "user_email": details["user_email"],
            "organization": details["organization"]
        })

        show_info("License Activated", "NakshaAI-LiDAR activated successfully.")
        return True

    except requests.exceptions.SSLError as error:
        show_error(
            "Secure Connection Failed",
            "Could not verify the license server certificate.\n\n"
            "Please check that naksha.crt is included in the software package.\n\n"
            f"{error}"
        )
        return False

    except Exception as error:
        show_error(
            "Server Connection Failed",
            f"Could not connect to license server.\n\n{error}"
        )
        return False


def check_license() -> bool:
    global _LAST_SERVER_HARD_BLOCK
    global _LAST_SERVER_OFFLINE_EXPIRED

    _LAST_SERVER_HARD_BLOCK = False
    _LAST_SERVER_OFFLINE_EXPIRED = False

    license_data = load_license_data()

    if not license_data:
        return False

    if not cached_license_matches_this_machine(license_data):
        _LAST_SERVER_HARD_BLOCK = True
        show_error("License Error", "This license was activated on another machine.")
        return False

    if not saved_license_version_matches_current(license_data):
        return False

    license_key = license_data.get("license_key")
    if not license_key:
        return False

    try:
        machine_info = get_machine_info(
            license_key=license_key,
            user_name=license_data.get("user_name"),
            user_email=license_data.get("user_email"),
            organization=license_data.get("organization")
        )

        response = requests.post(
            f"{SERVER_URL}/api/check-license",
            json=machine_info,
            timeout=10,
            verify=get_ssl_verify_setting()
        )

        if response.status_code == 200:
            license_data["last_success_at"] = now_iso()
            license_data["software_version"] = CURRENT_VERSION
            save_license_data(license_data)
            return True

        _LAST_SERVER_HARD_BLOCK = True

        if response.status_code == 426:
            show_error("Update Required", get_error_text(response))
            return False

        show_error("License Check Failed", get_error_text(response))
        return False

    except Exception:
        if cached_license_still_allowed(license_data):
            return True

        _LAST_SERVER_OFFLINE_EXPIRED = True
        show_error(
            "License Server Unreachable",
            "Could not reach the license server.\n\n"
            "Offline access period expired. Please connect to the license server and open again."
        )
        return False


def require_valid_license() -> bool:
    license_data = load_license_data()

    if license_data:
        saved_version = license_data.get("software_version")

        if saved_license_version_matches_current(license_data):
            if check_license():
                return True

            if _LAST_SERVER_HARD_BLOCK:
                return False

            if _LAST_SERVER_OFFLINE_EXPIRED:
                return False

            delete_saved_license()

        else:
            delete_saved_license()

            show_info(
                "New Version Activation Required",
                f"NakshaAI-LiDAR has been updated to version {CURRENT_VERSION}.\n\n"
                f"Previous activation was for version {saved_version or 'older version'}.\n\n"
                "Please enter the new activation key for this version."
            )

    license_key = ask_for_license_key()

    if not license_key:
        show_error("Activation Required", "Activation key is required to open NakshaAI-LiDAR.")
        return False

    return activate_license(license_key)


def _heartbeat_loop(interval_seconds: int) -> None:
    while True:
        time.sleep(interval_seconds)

        license_data = load_license_data()

        if not license_data:
            continue

        if not saved_license_version_matches_current(license_data):
            continue

        license_key = license_data.get("license_key")
        if not license_key:
            continue

        try:
            machine_info = get_machine_info(
                license_key=license_key,
                user_name=license_data.get("user_name"),
                user_email=license_data.get("user_email"),
                organization=license_data.get("organization")
            )

            response = requests.post(
                f"{SERVER_URL}/api/check-license",
                json=machine_info,
                timeout=10,
                verify=get_ssl_verify_setting()
            )

            if response.status_code == 200:
                license_data["last_success_at"] = now_iso()
                save_license_data(license_data)
                continue

            if response.status_code in (400, 403, 409, 426):
                show_error("License Blocked", get_error_text(response))
                os._exit(0)

        except Exception:
            pass


def start_license_heartbeat(interval_seconds: int = 300) -> None:
    global _HEARTBEAT_STARTED

    if _HEARTBEAT_STARTED:
        return

    _HEARTBEAT_STARTED = True

    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(interval_seconds,),
        daemon=True
    )
    thread.start()