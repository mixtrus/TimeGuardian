import ctypes
import datetime
import os
import struct
import subprocess
import sys
from typing import List, Optional, Tuple, Dict, Set
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import requests

# Windows SYSTEMTIME for SetSystemTime (UTC)
class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", ctypes.c_ushort),
        ("wMonth", ctypes.c_ushort),
        ("wDayOfWeek", ctypes.c_ushort),
        ("wDay", ctypes.c_ushort),
        ("wHour", ctypes.c_ushort),
        ("wMinute", ctypes.c_ushort),
        ("wSecond", ctypes.c_ushort),
        ("wMilliseconds", ctypes.c_ushort),
    ]

def is_windows() -> bool:
    return os.name == "nt"

def set_system_time_utc_via_helper(target_epoch_ms: int) -> Tuple[bool, str]:
    helper = os.path.join(os.path.dirname(sys.argv[0]), "elevated_time_setter.py")
    if not os.path.isfile(helper):
        candidate = os.path.join(os.path.dirname(__file__), "..", "elevated_time_setter.py")
        candidate = os.path.abspath(candidate)
        if os.path.isfile(candidate):
            helper = candidate
    if not os.path.isfile(helper):
        return False, "Elevated helper not found."
    python_exe = sys.executable
    cmd = [
        "powershell",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        f"Start-Process -FilePath '{python_exe}' -ArgumentList @('{helper}', '--set-utc-epoch-ms', '{target_epoch_ms}') -Verb RunAs -WindowStyle Hidden"
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return True, "Elevation invoked (verification pending)."
    except subprocess.TimeoutExpired:
        return False, "Timed out requesting elevation."
    except Exception as e:
        return False, f"Failed to invoke elevation: {e}"

def list_windows_timezones() -> List[str]:
    try:
        out = subprocess.check_output(["tzutil", "/l"], text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []

def get_current_timezone() -> str:
    try:
        out = subprocess.check_output(["tzutil", "/g"], text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        return out.strip()
    except Exception:
        return ""

def set_windows_timezone(tz_standard_name: str) -> Tuple[bool, str]:
    try:
        subprocess.check_call(["tzutil", "/s", tz_standard_name], timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        return True, f"Timezone set to {tz_standard_name}."
    except subprocess.CalledProcessError as e:
        return False, f"Failed to set timezone: {e}"
    except Exception as e:
        return False, f"Error setting timezone: {e}"

def _format_utc_offset(minutes: int, with_space: bool = True) -> str:
    sign = "+" if minutes >= 0 else "-"
    m = abs(minutes)
    hh = m // 60
    mm = m % 60
    return f"UTC {sign}{hh:02d}:{mm:02d}" if with_space else f"UTC{sign}{hh:02d}:{mm:02d}"

def _parse_display_utc_prefix(display: str) -> Optional[int]:
    if not display.startswith("(UTC") or ")" not in display:
        return None
    try:
        prefix = display[1:display.index(")")]  # e.g., 'UTC+03:30'
        if not prefix.startswith("UTC"):
            return None
        s = prefix[3:]
        sign = 1 if s[0] == "+" else -1
        s = s[1:]
        if ":" in s:
            hh, mm = s.split(":", 1)
        else:
            hh = s[:2]
            mm = s[2:4] if len(s) >= 4 else "00"
        return sign * (int(hh) * 60 + int(mm))
    except Exception:
        return None

def _enum_windows_time_zones_registry() -> List[Dict]:
    zones: List[Dict] = []
    if not is_windows():
        return zones
    try:
        import winreg  # type: ignore
        base = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Time Zones"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ) as root:
            i = 0
            while True:
                try:
                    key_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, key_name, 0, winreg.KEY_READ) as k:
                        try:
                            display, _ = winreg.QueryValueEx(k, "Display")
                        except Exception:
                            display = ""
                        try:
                            tzi_bytes, _ = winreg.QueryValueEx(k, "TZI")
                            bias, std_bias, dlt_bias, *rest = struct.unpack("<3l8H8H", tzi_bytes)
                            daylight_month = rest[8]
                            standard_month = rest[0]
                            has_dst = (daylight_month != 0 or standard_month != 0)
                            offset_minutes = -(bias)
                        except Exception:
                            offset_minutes = _parse_display_utc_prefix(display) or 0
                            has_dst = False
                        zones.append({
                            "key": key_name,
                            "display": display,
                            "offset_minutes": offset_minutes,
                            "has_dst": has_dst
                        })
                except Exception:
                    continue
    except Exception:
        pass
    return zones

WIN_TO_IANA: Dict[str, str] = {
    "UTC": "Etc/UTC",
    "Iran Standard Time": "Asia/Tehran",
    # ... (mapping as in previous version, omitted here for brevity; keep the full mapping in your file)
}

def windows_key_to_iana(key: str) -> Optional[str]:
    return WIN_TO_IANA.get(key)

def iana_city_region(iana: str) -> Tuple[str, str]:
    if "/" in iana:
        area, city = iana.split("/", 1)
        return city.replace("_", " "), area
    return iana, ""

def list_timezone_choices_labels() -> List[Tuple[str, str]]:
    zones = _enum_windows_time_zones_registry()
    items: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for z in zones:
        key = z["key"]
        off = z["offset_minutes"]
        label_off = _format_utc_offset(off)
        iana = windows_key_to_iana(key)
        if iana:
            city, area = iana_city_region(iana)
            label = f"(UTC {label_off[4:]}) {city}/{area}" if area else f"(UTC {label_off[4:]}) {city}"
        else:
            disp = z["display"]
            city_part = disp.split(")", 1)[1].strip() if ")" in disp else disp
            label = f"(UTC {label_off[4:]}) {city_part}"
        if key not in seen:
            items.append((label, key))
            seen.add(key)
    def offset_for_key(k: str) -> int:
        for z in zones:
            if z["key"] == k:
                return z["offset_minutes"]
        return 0
    items.sort(key=lambda x: (offset_for_key(x[1]), x[0]))
    return items

def get_timezone_from_ip_modern(timeout_sec: float = 5.0, overall_timeout_sec: float = 8.0) -> Tuple[Optional[str], Optional[int]]:
    headers = {"User-Agent": "TimeGuardian/1.0"}

    def p_ipapi():
        r = requests.get("https://ipapi.co/json/", timeout=timeout_sec, headers=headers)
        if r.status_code == 200:
            j = r.json()
            iana = j.get("timezone")
            uo = j.get("utc_offset")
            off = None
            if isinstance(uo, str) and len(uo) in (5, 6):
                sign = 1 if uo[0] == "+" else -1
                hh = int(uo[1:3]); mm = int(uo[-2:])
                off = sign * (hh * 60 + mm)
            return iana, off
        raise RuntimeError("ipapi.co failed")

    def p_ipapi_com():
        r = requests.get("http://ip-api.com/json", timeout=timeout_sec, headers=headers)
        if r.status_code == 200:
            j = r.json()
            iana = j.get("timezone")
            off_sec = j.get("offset")
            off = int(off_sec // 60) if isinstance(off_sec, int) else None
            return iana, off
        raise RuntimeError("ip-api.com failed")

    def p_ipwho():
        r = requests.get("https://ipwho.is/", timeout=timeout_sec, headers=headers)
        if r.status_code == 200:
            j = r.json()
            tz = j.get("timezone") or {}
            iana = tz.get("id")
            off_sec = tz.get("offset")
            off = int(off_sec // 60) if isinstance(off_sec, (int, float)) else None
            return iana, off
        raise RuntimeError("ipwho.is failed")

    def p_worldtimeapi():
        r = requests.get("https://worldtimeapi.org/api/ip", timeout=timeout_sec, headers=headers)
        if r.status_code == 200:
            j = r.json()
            iana = j.get("timezone")
            uo = j.get("utc_offset")
            off = None
            if isinstance(uo, str) and len(uo) == 6 and uo[3] == ":":
                sign = 1 if uo[0] == "+" else -1
                hh = int(uo[1:3]); mm = int(uo[4:6])
                off = sign * (hh * 60 + mm)
            return iana, off
        raise RuntimeError("worldtimeapi failed")

    def p_ifconfig():
        r = requests.get("https://ifconfig.co/json", timeout=timeout_sec, headers=headers)
        if r.status_code == 200:
            j = r.json()
            iana = j.get("timezone")
            return iana, None
        raise RuntimeError("ifconfig.co failed")

    def p_ip_sb():
        r = requests.get("https://api.ip.sb/geoip", timeout=timeout_sec, headers=headers)
        if r.status_code == 200:
            j = r.json()
            iana = j.get("timezone")
            off_sec = j.get("offset")
            off = int(off_sec // 60) if isinstance(off_sec, (int, float)) else None
            return iana, off
        raise RuntimeError("ip.sb failed")

    providers = [p_ipapi, p_ipapi_com, p_ipwho, p_worldtimeapi, p_ifconfig, p_ip_sb]

    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    with ThreadPoolExecutor(max_workers=len(providers)) as ex:
        futures = [ex.submit(p) for p in providers]
        done, not_done = wait(futures, timeout=overall_timeout_sec, return_when=FIRST_COMPLETED)
        for d in done:
            try:
                iana, off = d.result()
                if iana or off is not None:
                    return iana, off
            except Exception:
                continue
        for f in futures:
            try:
                iana, off = f.result(timeout=0.1)
                if iana or off is not None:
                    return iana, off
            except Exception:
                pass
    return None, None

def get_timezone_offset_from_ip(timeout_sec: float = 5.0) -> Optional[int]:
    iana, off = get_timezone_from_ip_modern(timeout_sec=timeout_sec)
    return off

def _best_zone_for_offset(offset_minutes: int) -> Optional[str]:
    zones = _enum_windows_time_zones_registry()
    candidates_utc = [z for z in zones if (not z["has_dst"]) and z["offset_minutes"] == offset_minutes and z["key"].upper().startswith("UTC")]
    if candidates_utc:
        return sorted((z["key"] for z in candidates_utc))[0]
    candidates = [z for z in zones if (not z["has_dst"]) and z["offset_minutes"] == offset_minutes]
    if candidates:
        return sorted((z["key"] for z in candidates))[0]
    any_candidates = [z for z in zones if z["offset_minutes"] == offset_minutes]
    if any_candidates:
        return sorted((z["key"] for z in any_candidates))[0]
    return None

def set_timezone_by_utc_label(label: str) -> Tuple[bool, str]:
    if not label.upper().startswith("UTC"):
        return False, "Invalid UTC label."
    try:
        s = label[3:].strip()
        sign = 1 if s[0] == "+" else -1
        hh, mm = s[1:].split(":")
        minutes = sign * (int(hh) * 60 + int(mm))
    except Exception:
        return False, "Invalid UTC label format."
    key = _best_zone_for_offset(minutes)
    if not key:
        return False, f"No timezone found for UTC{label[3:]}."
    return set_windows_timezone(key)

def set_region_from_country_code(country_code: str) -> Tuple[bool, str]:
    ps_script = r"""
$ErrorActionPreference = 'Stop'
function Try-SetHomeLocation([string]$CountryCode) {
  try {
    if (Get-Command -Name Set-WinHomeLocation -ErrorAction SilentlyContinue) {
      $geo = Get-WinHomeLocation -ListAvailable | Where-Object { $_.CountryCode -eq $CountryCode }
      if ($geo) {
        Set-WinHomeLocation -GeoId $geo.GeoId
        return $true
      }
    }
  } catch { }
  return $false
}
function Try-SetCulture([string]$CountryCode) {
  try {
    if (Get-Command -Name Set-Culture -ErrorAction SilentlyContinue) {
      $candidate = "en-$CountryCode"
      try {
        Set-Culture -CultureInfo $candidate
        return $true
      } catch {
        $cult = [System.Globalization.CultureInfo]::GetCultures([System.Globalization.CultureTypes]::AllCultures) |
          Where-Object { $_.Name -match "-$CountryCode$" } | Select-Object -First 1
        if ($cult) {
          Set-Culture -CultureInfo $cult.Name
          return $true
        }
      }
    }
  } catch { }
  return $false
}
$cc = "%CC%"
$ok1 = Try-SetHomeLocation $cc
$ok2 = Try-SetCulture $cc
if ($ok1 -or $ok2) { exit 0 } else { exit 1 }
"""
    ps_script = ps_script.replace("%CC%", country_code.upper())
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".ps1", encoding="utf-8") as tf:
            tf.write(ps_script)
            script_path = tf.name
    except Exception as e:
        return False, f"Failed to create temp script: {e}"
    cmd = [
        "powershell",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        f"Start-Process powershell -ArgumentList '-ExecutionPolicy Bypass -File \"{script_path}\"' -Verb RunAs -WindowStyle Hidden"
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "Timed out requesting elevation for region change."
    except Exception as e:
        return False, f"Failed to invoke elevation: {e}"
    return True, "Region change requested (verification pending)."

def register_startup(app_name: str, exe_path: str, enable: bool) -> Tuple[bool, str]:
    """
    Adds or removes a Run registry key for current user (no admin required).
    Writes a --startup flag to allow the app to detect startup launches and suppress the welcome dialog.
    """
    if not is_windows():
        return False, "Startup registration is only available on Windows."
    try:
        import winreg  # type: ignore
    except Exception:
        return False, "winreg not available."
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_ALL_ACCESS) as key:
            if enable:
                value = f"\"{exe_path}\" --startup"
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, value)
                return True, "Startup enabled."
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
                return True, "Startup disabled."
    except Exception as e:
        return False, f"Failed to update startup: {e}"