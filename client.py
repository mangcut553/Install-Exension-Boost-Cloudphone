import asyncio
import base64
import fcntl
import glob
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

import websockets

BASE_DIR = "/sdcard/Download/ManagerClient"
COOKIE_FILE = "/sdcard/Download/cookie.txt"
SHOUKO_DIR = "/sdcard/Download/Shouko"
SWITCHED_DIR = os.path.join(SHOUKO_DIR, "switched")
AUTOEXEC_PRIMARY_DIR = "/storage/emulated/0/Delta/Autoexecute"
AUTOEXEC_DIR_CANDIDATES = (AUTOEXEC_PRIMARY_DIR, "/sdcard/Delta/Autoexecute")
STORAGE_ROOTS = ("/sdcard", "/storage/emulated/0")
COOKIE_CANDIDATES = (
    COOKIE_FILE,
    "/storage/emulated/0/Download/cookie.txt",
    "/sdcard/cookie.txt",
    "/storage/emulated/0/cookie.txt",
)
SWITCHED_DIR_CANDIDATES = (
    SWITCHED_DIR,
    "/storage/emulated/0/Download/Shouko/switched",
    "/sdcard/Download/switched",
    "/storage/emulated/0/Download/switched",
)
CONFIG_FILE = os.path.join(SHOUKO_DIR, "config.json")
SERVER_LINKS_FILE = os.path.join(SHOUKO_DIR, "server_links.txt")
TOKEN_FILE = os.path.join(BASE_DIR, "device_token.txt")
NAME_FILE = os.path.join(BASE_DIR, "name.txt")
LOCK_FILE = os.path.join(BASE_DIR, "client.lock")
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 12 * 1024 * 1024
CONTENT_SCAN_INTERVAL = 10
CLIENT_VERSION = "1.5.3"

for directory in (BASE_DIR, SHOUKO_DIR, SWITCHED_DIR):
    os.makedirs(directory, exist_ok=True)

LOCK_HANDLE = open(LOCK_FILE, "w", encoding="utf-8")
try:
    fcntl.flock(LOCK_HANDLE, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("MCMWMANAGER client đã chạy ở một tiến trình khác")


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as file:
            return file.read().strip()
    except Exception:
        return default


def count_lines(path):
    try:
        content = read_binary(path)
        return sum(bool(line.strip()) for line in content.decode("utf-8", errors="ignore").splitlines())
    except Exception:
        return 0


def read_cpu_times():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as file:
            values = [int(value) for value in file.readline().split()[1:]]
        if len(values) < 4:
            return None
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle
    except Exception:
        return None


CPU_PREVIOUS = read_cpu_times()
POWER_CACHE = {"time": 0, "battery": None, "temperature": None}
AUTOEXEC_CACHE = {"time": 0, "paths": []}
DATA_PATH_CACHE = {"time": 0, "cookiePaths": [], "switchedDirs": []}
CONTENT_STATUS_CACHE = {"time": 0, "snapshot": {}}
CONTENT_SCAN_LOCK = threading.Lock()


def cpu_usage_percent():
    global CPU_PREVIOUS
    current = read_cpu_times()
    previous = CPU_PREVIOUS
    CPU_PREVIOUS = current
    if not current or not previous:
        return 0
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return 0
    return round(max(0, min(100, (total_delta - idle_delta) * 100 / total_delta)), 1)


def command_output(command, timeout=6):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=timeout,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def numeric_value(value):
    try:
        return float(str(value).strip())
    except Exception:
        return None


def normalized_temperature(value):
    number = numeric_value(value)
    if number is None or number <= 0:
        return None
    if number > 1000:
        number /= 1000
    elif number > 150:
        number /= 10
    return round(number, 1) if 0 < number <= 150 else None


def parse_dumpsys_battery(output):
    values = {}
    for line in str(output or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    level, scale = numeric_value(values.get("level")), numeric_value(values.get("scale"))
    battery = None
    if level is not None:
        battery = level * 100 / scale if scale and scale > 0 else level
        battery = round(max(0, min(100, battery)), 1)
    return battery, normalized_temperature(values.get("temperature"))


def read_power_snapshot():
    if time.time() - POWER_CACHE["time"] < 45:
        return POWER_CACHE["battery"], POWER_CACHE["temperature"]
    battery = temperature = None

    termux_status = command_output(["termux-battery-status"], timeout=4)
    if termux_status:
        try:
            data = json.loads(termux_status)
            value = numeric_value(data.get("percentage"))
            if value is not None:
                battery = round(max(0, min(100, value)), 1)
            temperature = normalized_temperature(data.get("temperature"))
        except Exception:
            pass

    if battery is None or temperature is None:
        dumpsys = command_output(["dumpsys", "battery"], timeout=5)
        if not dumpsys:
            dumpsys = command_output(["su", "-c", "dumpsys battery"], timeout=5)
        parsed_battery, parsed_temperature = parse_dumpsys_battery(dumpsys)
        if battery is None:
            battery = parsed_battery
        if temperature is None:
            temperature = parsed_temperature

    power_paths = sorted(glob.glob("/sys/class/power_supply/*"), key=lambda path: "battery" not in path.lower())
    for path in power_paths:
        if battery is None:
            value = numeric_value(read_text(os.path.join(path, "capacity"), ""))
            if value is not None:
                battery = round(max(0, min(100, value)), 1)
        if temperature is None:
            for filename in ("temp", "temperature", "batt_temp"):
                temperature = normalized_temperature(read_text(os.path.join(path, filename), ""))
                if temperature is not None:
                    break
        if battery is not None and temperature is not None:
            break

    if temperature is None:
        zones = []
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            value = normalized_temperature(read_text(path, ""))
            if value is None:
                continue
            kind = read_text(os.path.join(os.path.dirname(path), "type"), "").lower()
            priority = 0 if "battery" in kind else 1 if any(word in kind for word in ("cpu", "soc", "ap")) else 2
            zones.append((priority, path, value))
        if zones:
            temperature = sorted(zones)[0][2]

    POWER_CACHE.update({"time": time.time(), "battery": battery, "temperature": temperature})
    return battery, temperature


def health_snapshot():
    memory = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as file:
            for line in file:
                key, value = line.split(":", 1)
                memory[key] = int(value.strip().split()[0])
    except Exception:
        pass
    total_kb = memory.get("MemTotal", 0)
    available_kb = memory.get("MemAvailable", memory.get("MemFree", 0))
    try:
        disk = shutil.disk_usage("/sdcard")
        storage_total = round(disk.total / (1024 ** 3), 2)
        storage_used = round(disk.used / (1024 ** 3), 2)
    except Exception:
        storage_total = storage_used = 0
    battery, temperature = read_power_snapshot()
    try:
        uptime = float(read_text("/proc/uptime", "0").split()[0])
    except Exception:
        uptime = 0
    try:
        load1 = os.getloadavg()[0]
    except Exception:
        load1 = 0
    return {
        "cpuUsage": cpu_usage_percent(),
        "ramUsedMb": round(max(0, total_kb - available_kb) / 1024, 1),
        "ramTotalMb": round(total_kb / 1024, 1),
        "storageUsedGb": storage_used,
        "storageTotalGb": storage_total,
        "battery": battery,
        "temperature": temperature,
        "uptimeSeconds": round(uptime),
        "load1": round(load1, 2),
    }


PACKAGE_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]*)*$")


def clone_packages(prefix):
    prefix = str(prefix or "").strip().lower()
    if len(prefix) < 2 or len(prefix) > 100 or not PACKAGE_PREFIX_RE.fullmatch(prefix):
        raise ValueError("Prefix package không hợp lệ")
    commands = (
        ["cmd", "package", "list", "packages"],
        ["pm", "list", "packages"],
        ["su", "-c", "cmd package list packages"],
        ["su", "-c", "pm list packages"],
    )
    packages = set()
    for command in commands:
        output = command_output(command, timeout=4)
        for line in output.splitlines():
            name = line.strip().removeprefix("package:").split("=", 1)[0].strip().lower()
            if name.startswith(prefix) and re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", name):
                packages.add(name)
        if packages:
            break
    return prefix, sorted(packages)[:500]


def memory_kb(value):
    text = str(value or "0").strip().upper().replace("IB", "").replace("B", "")
    match = re.match(r"^([0-9.]+)([KMG]?)$", text)
    if not match:
        return 0
    number = numeric_value(match.group(1)) or 0
    unit = match.group(2)
    if unit == "G":
        number *= 1024 * 1024
    elif unit == "M":
        number *= 1024
    return max(0, number)


def process_rows(output):
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    header_index = -1
    headers = []
    for index, line in enumerate(lines):
        columns = re.split(r"\s+", line.upper())
        if "PID" in columns and any(name in columns for name in ("RSS", "RES")):
            header_index, headers = index, columns
            break
    if header_index < 0:
        return []
    pid_index = headers.index("PID")
    cpu_index = next((headers.index(name) for name in ("%CPU", "CPU") if name in headers), None)
    ram_index = next((headers.index(name) for name in ("RSS", "RES") if name in headers), None)
    rows = []
    for line in lines[header_index + 1:]:
        parts = re.split(r"\s+", line, maxsplit=max(0, len(headers) - 1))
        if len(parts) <= max(pid_index, ram_index):
            continue
        try:
            int(parts[pid_index])
        except Exception:
            continue
        cpu = numeric_value(parts[cpu_index].rstrip("%")) if cpu_index is not None and cpu_index < len(parts) else 0
        tokens = {
            token.split(":", 1)[0].lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+(?:\:[A-Za-z0-9_.-]+)?", line)
        }
        rows.append({"tokens": tokens, "cpu": max(0, cpu or 0), "ramKb": memory_kb(parts[ram_index])})
    return rows


def aggregate_clone_processes(packages, rows):
    package_set = set(packages)
    details = {name: {"package": name, "running": False, "cpuUsage": 0, "ramMb": 0} for name in packages}
    for row in rows:
        matches = package_set.intersection(row["tokens"])
        for name in matches:
            item = details[name]
            item["running"] = True
            item["cpuUsage"] += row["cpu"]
            item["ramMb"] += row["ramKb"] / 1024
    for item in details.values():
        item["cpuUsage"] = round(item["cpuUsage"], 1)
        item["ramMb"] = round(item["ramMb"], 1)
    return details


def clone_monitor_snapshot(prefix):
    prefix, packages = clone_packages(prefix)
    if not packages:
        return {"prefix": prefix, "total": 0, "running": 0, "cpuUsage": 0, "ramMb": 0, "packages": [], "checkedAt": int(time.time() * 1000)}
    process_commands = (
        ["su", "-c", "ps -A -o PID,NAME,%CPU,RSS,ARGS"],
        ["ps", "-A", "-o", "PID,NAME,%CPU,RSS,ARGS"],
        ["su", "-c", "ps -A -o PID,NAME,RSS,ARGS"],
        ["ps", "-A", "-o", "PID,NAME,RSS,ARGS"],
        ["su", "-c", "top -b -n 1"],
        ["top", "-b", "-n", "1"],
    )
    best = aggregate_clone_processes(packages, [])
    best_score = (0, 0)
    for command in process_commands:
        rows = process_rows(command_output(command, timeout=4))
        if not rows:
            continue
        candidate = aggregate_clone_processes(packages, rows)
        running = sum(item["running"] for item in candidate.values())
        score = (running, sum(item["ramMb"] for item in candidate.values()))
        if score > best_score:
            best, best_score = candidate, score
        if running == len(packages) and score[1] > 0 and any(item["cpuUsage"] > 0 for item in candidate.values()):
            break

    if packages and not any(item["running"] for item in best.values()):
        activity = command_output(["dumpsys", "activity", "processes"], timeout=4)
        if not activity:
            activity = command_output(["su", "-c", "dumpsys activity processes"], timeout=4)
        for name in packages:
            if name in activity:
                best[name]["running"] = True

    details = sorted(best.values(), key=lambda item: (not item["running"], item["package"]))
    return {
        "prefix": prefix,
        "total": len(packages),
        "running": sum(item["running"] for item in details),
        "cpuUsage": round(sum(item["cpuUsage"] for item in details), 1),
        "ramMb": round(sum(item["ramMb"] for item in details), 1),
        "packages": details[:200],
        "checkedAt": int(time.time() * 1000),
    }


def capture_screenshot():
    path = os.path.join(BASE_DIR, f"screenshot_{os.getpid()}.png")
    commands = (
        ["screencap", "-p", path],
        ["su", "-c", f"screencap -p {path}"],
        ["termux-screenshot", "-f", path],
    )
    try:
        for command in commands:
            try:
                if os.path.exists(path):
                    os.remove(path)
                result = subprocess.run(command, capture_output=True, timeout=8, check=False)
                if result.returncode != 0 or not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                if not 8 <= size <= MAX_SCREENSHOT_BYTES:
                    continue
                with open(path, "rb") as image:
                    raw = image.read(MAX_SCREENSHOT_BYTES + 1)
                if len(raw) > MAX_SCREENSHOT_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    continue
                width = int.from_bytes(raw[16:20], "big") if len(raw) >= 24 else 0
                height = int.from_bytes(raw[20:24], "big") if len(raw) >= 24 else 0
                return {
                    "content": base64.b64encode(raw).decode("ascii"),
                    "mimeType": "image/png",
                    "width": width,
                    "height": height,
                    "capturedAt": int(time.time() * 1000),
                }
            except Exception:
                continue
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    raise RuntimeError("Không chụp được màn hình. Hãy cấp quyền Termux:API hoặc Root cho thiết bị")


def safe_name(value, default="file.txt"):
    name = os.path.basename(str(value or "").strip())
    cleaned = "".join(char for char in name if char.isalnum() or char in "._-()[] ")
    return cleaned[:120] or default


def is_autoexec_name(value):
    compact = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    return compact.startswith("autoexec")


def storage_marker(path):
    normalized = os.path.normpath(str(path or "").strip())
    if normalized == "/storage/emulated/0":
        normalized = "/sdcard"
    if normalized.startswith("/storage/emulated/0/"):
        normalized = "/sdcard/" + normalized[len("/storage/emulated/0/"):]
    return normalized.lower()


def path_exists(path, kind="file"):
    try:
        direct = os.path.isdir(path) if kind == "dir" else os.path.isfile(path)
        if direct:
            return True
    except Exception:
        pass
    flag = "-d" if kind == "dir" else "-f"
    try:
        return subprocess.run(
            ["su", "-c", f"test {flag} {shlex.quote(path)}"],
            capture_output=True,
            timeout=4,
            check=False,
        ).returncode == 0
    except Exception:
        return False


def add_storage_path(found, path, kind, verified=False):
    path = os.path.normpath(str(path or "").strip())
    if not path.startswith(("/sdcard/", "/storage/emulated/0/")):
        return
    if verified or path_exists(path, kind):
        found.setdefault(storage_marker(path), path)


def resolve_data_paths(refresh=False):
    if not refresh and time.time() - DATA_PATH_CACHE["time"] < 30:
        return list(DATA_PATH_CACHE["cookiePaths"]), list(DATA_PATH_CACHE["switchedDirs"])

    cookie_paths, switched_dirs = {}, {}
    for path in COOKIE_CANDIDATES:
        add_storage_path(cookie_paths, path, "file")
        if cookie_paths:
            break
    for path in SWITCHED_DIR_CANDIDATES:
        add_storage_path(switched_dirs, path, "dir")
        if switched_dirs:
            break

    # Android scoped storage can make the normal Python view look empty even
    # though root can see the files. Search both names for the same storage.
    for root in STORAGE_ROOTS:
        quoted_root = shlex.quote(root)
        cookie_command = f"find {quoted_root}/Download -maxdepth 9 -type f -iname 'cookie.txt' -print 2>/dev/null | head -n 100"
        switched_command = f"find {quoted_root}/Download -maxdepth 9 -type d -iname 'switched' -print 2>/dev/null | head -n 100"
        if not cookie_paths:
            for path in command_output(["su", "-c", cookie_command], timeout=6).splitlines():
                add_storage_path(cookie_paths, path, "file", verified=True)
        if not switched_dirs:
            for path in command_output(["su", "-c", switched_command], timeout=6).splitlines():
                add_storage_path(switched_dirs, path, "dir", verified=True)
        if cookie_paths and switched_dirs:
            break

    cookie_result = list(cookie_paths.values())
    switched_result = list(switched_dirs.values())
    DATA_PATH_CACHE.update({
        "time": time.time(),
        "cookiePaths": cookie_result,
        "switchedDirs": switched_result,
    })
    return list(cookie_result), list(switched_result)


def add_autoexec_dir(found, path):
    path = os.path.normpath(str(path or "").strip())
    if not path.startswith(("/sdcard/", "/storage/emulated/0/")) or not is_autoexec_name(os.path.basename(path)):
        return
    found.setdefault(storage_marker(path), path)


def autoexec_dirs(refresh=False):
    if not refresh and AUTOEXEC_CACHE["paths"] and time.time() - AUTOEXEC_CACHE["time"] < 30:
        return list(AUTOEXEC_CACHE["paths"])
    found = {}
    for path in AUTOEXEC_DIR_CANDIDATES:
        if path_exists(path, "dir"):
            add_autoexec_dir(found, path)
            break
    if found:
        paths = list(found.values())
        AUTOEXEC_CACHE.update({"time": time.time(), "paths": paths})
        return list(paths)

    known = tuple(
        f"{root}/{suffix}"
        for root in STORAGE_ROOTS
        for suffix in ("Delta/Autoexecute", "Delta/Autoexec", "Autoexecute", "Autoexec", "Download/Autoexecute", "Download/Autoexec")
    )
    for path in known:
        if path_exists(path, "dir"):
            add_autoexec_dir(found, path)
    if found:
        paths = sorted(found.values(), key=str.lower)
        AUTOEXEC_CACHE.update({"time": time.time(), "paths": paths})
        return list(paths)

    skipped = {"dcim", "pictures", "movies", "music", "alarms", "notifications", "podcasts", "ringtones"}
    for storage_root in STORAGE_ROOTS:
        base_depth = storage_root.count("/")
        try:
            for root, dirs, _ in os.walk(storage_root):
                if is_autoexec_name(os.path.basename(root)):
                    add_autoexec_dir(found, root)
                    dirs[:] = []
                    continue
                depth = root.rstrip("/").count("/") - base_depth
                dirs[:] = [name for name in dirs if not name.startswith(".") and name.lower() not in skipped and depth < 8]
        except Exception:
            pass

    root_commands = (
        "find /sdcard -maxdepth 9 -type d -iname '*auto*exec*' -print 2>/dev/null | head -n 500",
        "find /storage/emulated/0 -maxdepth 9 -type d -iname '*auto*exec*' -print 2>/dev/null | head -n 500",
    )
    for command in root_commands:
        output = command_output(["su", "-c", command], timeout=6)
        for path in output.splitlines():
            add_autoexec_dir(found, path)

    if not found:
        default = AUTOEXEC_PRIMARY_DIR
        os.makedirs(default, exist_ok=True)
        add_autoexec_dir(found, default)
    paths = sorted(found.values(), key=str.lower)
    AUTOEXEC_CACHE.update({"time": time.time(), "paths": paths})
    return list(paths)


def directory_files(directory):
    files = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file():
                    files.setdefault(storage_marker(entry.path), entry.path)
    except Exception:
        pass

    # Always merge the root view. On UGPhone os.scandir() may succeed but show
    # an empty/partial directory because of Android scoped-storage rules.
    command = f"find {shlex.quote(directory)} -maxdepth 1 -type f -print 2>/dev/null"
    for path in command_output(["su", "-c", command], timeout=6).splitlines():
        path = os.path.normpath(path.strip())
        if storage_marker(os.path.dirname(path)) == storage_marker(directory):
            files.setdefault(storage_marker(path), path)
    return sorted(files.values(), key=str.lower)


def script_names(directories=None):
    names = {os.path.basename(path) for directory in (directories or autoexec_dirs()) for path in directory_files(directory)}
    return sorted((name for name in names if name), key=str.lower)


def script_snapshot(refresh=False):
    directories = autoexec_dirs(refresh=refresh)
    files = [path for directory in directories for path in directory_files(directory)]
    names = sorted({os.path.basename(path) for path in files if os.path.basename(path)}, key=str.lower)
    return directories, names, len(files)


def cookie_snapshot(refresh=False):
    cookie_paths, _ = resolve_data_paths(refresh=refresh)
    best_count, best_path = 0, COOKIE_FILE
    for path in cookie_paths:
        try:
            count = sum(
                bool(line.strip())
                for line in read_binary(path).decode("utf-8", errors="ignore").splitlines()
            )
        except Exception:
            continue
        if (best_path == COOKIE_FILE and path == COOKIE_FILE) or count > best_count:
            best_count, best_path = count, path
    return best_count, best_path, cookie_paths


def switched_snapshot(refresh=False):
    _, directories = resolve_data_paths(refresh=refresh)
    best_count, best_files, best_directory = 0, [], SWITCHED_DIR
    for directory in directories:
        files = [path for path in directory_files(directory) if path.lower().endswith(".txt")]
        count = sum(count_lines(path) for path in files)
        if len(files) > len(best_files) or count > best_count:
            best_count, best_files, best_directory = count, files, directory
    return best_count, best_directory, best_files, directories


def content_status_snapshot(refresh=False):
    cookie, cookie_path, cookie_paths = cookie_snapshot(refresh=refresh)
    switched, switched_directory, switched_files, switched_dirs = switched_snapshot(refresh=False)
    script_dirs, scripts, script_file_count = script_snapshot(refresh=refresh)
    return {
        "cookie": cookie,
        "cookiePath": cookie_path,
        "cookiePaths": cookie_paths,
        "switched": switched,
        "switchedPath": switched_directory,
        "switchedDirs": switched_dirs,
        "switchedFileCount": len(switched_files),
        "scripts": scripts,
        "scriptDirs": script_dirs,
        "scriptFileCount": script_file_count,
    }


def refresh_content_status_cache(force=False):
    with CONTENT_SCAN_LOCK:
        if not force and CONTENT_STATUS_CACHE["snapshot"] and CONTENT_STATUS_CACHE["time"]:
            return dict(CONTENT_STATUS_CACHE["snapshot"])
        try:
            snapshot = content_status_snapshot(refresh=True)
            snapshot.update({
                "scanMode": "loop",
                "scannedAt": int(time.time() * 1000),
                "scanError": "",
            })
        except Exception as error:
            snapshot = dict(CONTENT_STATUS_CACHE["snapshot"])
            snapshot.update({
                "scanMode": "loop",
                "scannedAt": int(time.time() * 1000),
                "scanError": str(error),
            })
        CONTENT_STATUS_CACHE.update({"time": time.time(), "snapshot": snapshot})
        return dict(snapshot)


def invalidate_content_status_cache():
    CONTENT_STATUS_CACHE["time"] = 0
    CONTENT_STATUS_CACHE["snapshot"] = {}
    DATA_PATH_CACHE["time"] = 0
    AUTOEXEC_CACHE["time"] = 0


def write_script(directory, name, content):
    path = os.path.join(directory, name)
    write_text_file(path, content)


def write_text_file(path, content):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
        return
    except Exception:
        pass
    command = f"mkdir -p {shlex.quote(os.path.dirname(path))} && cat > {shlex.quote(path)}"
    result = subprocess.run(["su", "-c", command], input=content, text=True, capture_output=True, timeout=10, check=False)
    if result.returncode != 0:
        message = result.stderr.decode(errors="ignore") if isinstance(result.stderr, bytes) else result.stderr
        raise PermissionError(message or "Không có quyền ghi tệp trên bộ nhớ thiết bị")


def remove_path(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    command = f"test -f {shlex.quote(path)} && rm -f {shlex.quote(path)}"
    try:
        return subprocess.run(["su", "-c", command], capture_output=True, timeout=6, check=False).returncode == 0
    except Exception:
        return False


def read_binary(path):
    try:
        with open(path, "rb") as file:
            return file.read(MAX_EXPORT_BYTES + 1)
    except Exception:
        result = subprocess.run(["su", "-c", f"cat {shlex.quote(path)}"], capture_output=True, timeout=12, check=False)
        if result.returncode != 0:
            raise PermissionError("Không đọc được tệp " + os.path.basename(path))
        return result.stdout


def switched_count():
    return switched_snapshot()[0]


def import_config(filename, encoded):
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > MAX_EXPORT_BYTES:
        raise ValueError("Tệp config lớn hơn 16 MB")
    filename = safe_name(filename, "config.json")
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            total_size = 0
            for info in archive.infolist():
                if info.is_dir() or info.file_size > MAX_EXPORT_BYTES:
                    continue
                total_size += info.file_size
                if total_size > MAX_EXPORT_BYTES:
                    raise ValueError("Tổng dữ liệu giải nén lớn hơn 16 MB")
                name = safe_name(info.filename, "")
                if not name:
                    continue
                with open(os.path.join(SHOUKO_DIR, name), "wb") as output:
                    output.write(archive.read(info))
    else:
        with open(os.path.join(SHOUKO_DIR, filename), "wb") as output:
            output.write(raw)


def edit_config(updates, server_link):
    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                config = json.load(file)
        except Exception:
            config = {}
    for key, value in (updates or {}).items():
        config[str(key)] = "1" if value is True else "0" if value is False else str(value)
    if updates:
        with open(CONFIG_FILE, "w", encoding="utf-8") as file:
            json.dump(config, file, ensure_ascii=False, indent=2)
    if server_link is not None:
        base = "ugphone.bryxis.torven.roblox://placeID="
        current = read_text(SERVER_LINKS_FILE)
        if "=" in current:
            base = current.split("=", 1)[0] + "="
        with open(SERVER_LINKS_FILE, "w", encoding="utf-8") as file:
            file.write(base + str(server_link).strip())


def export_files(type_value, filename=None):
    files = []
    if type_value == "config_qh":
        files = [path for path in (CONFIG_FILE, SERVER_LINKS_FILE) if os.path.isfile(path)]
    elif type_value == "cookie":
        _, cookie_path, cookie_paths = cookie_snapshot(refresh=True)
        files = [cookie_path] if cookie_path in cookie_paths else []
    elif type_value == "switched":
        _, _, files, _ = switched_snapshot(refresh=True)
    elif type_value in ("script_all", "script_single"):
        target = safe_name(filename, "") if filename else ""
        for directory in autoexec_dirs():
            for path in directory_files(directory):
                name = os.path.basename(path)
                if type_value == "script_all" or name == target:
                    files.append(path)
    unique = []
    seen = set()
    for path in files:
        marker = storage_marker(path)
        if marker not in seen:
            seen.add(marker)
            unique.append(path)
    return unique


def make_export(type_value, machine_name, filename=None):
    files = export_files(type_value, filename)
    if not files:
        raise FileNotFoundError("Không có tệp phù hợp để export")
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, path in enumerate(files):
            parent = safe_name(os.path.basename(os.path.dirname(path)), "data")
            arcname = os.path.basename(path)
            if type_value.startswith("script"):
                arcname = f"[{parent}]_{arcname}"
            if arcname in archive.namelist():
                arcname = f"{index}_{arcname}"
            content = read_binary(path)
            if len(content) > MAX_EXPORT_BYTES:
                raise ValueError("Tệp export lớn hơn 16 MB")
            archive.writestr(arcname, content)
            if memory.tell() > MAX_EXPORT_BYTES:
                raise ValueError("Dữ liệu export lớn hơn 16 MB")
    raw = memory.getvalue()
    if len(raw) > MAX_EXPORT_BYTES:
        raise ValueError("Dữ liệu export lớn hơn 16 MB")
    name = safe_name(f"MCMWMANAGER_{machine_name}_{type_value}.zip", "MCMWMANAGER_export.zip")
    return name, base64.b64encode(raw).decode("ascii"), files


MACHINE_NAME = read_text(NAME_FILE, "unknown")
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", read_text(TOKEN_FILE, ""))
WS_URL = os.environ.get("WS_URL", "")


async def respond(ws, event, channel_id, **data):
    await ws.send(json.dumps({"event": event, "data": {"channelId": channel_id, **data}}))


async def handle_message(ws, message):
    payload = json.loads(message)
    event, data = payload.get("event"), payload.get("data", {})
    channel_id = data.get("channelId")

    if event in ("auth_success", "heartbeat_ack"):
        return
    if event == "status_request":
        snapshot = await asyncio.to_thread(refresh_content_status_cache, False)
        await respond(ws, "status_response", channel_id, **snapshot)
    elif event == "list_script_request":
        snapshot = await asyncio.to_thread(refresh_content_status_cache, False)
        await respond(ws, "list_script_response", channel_id, scripts=snapshot.get("scripts", []), scriptDirs=snapshot.get("scriptDirs", []), scriptFileCount=snapshot.get("scriptFileCount", 0), scanMode="loop", scannedAt=snapshot.get("scannedAt"))
    elif event == "import_cookie":
        try:
            incoming = {line.strip() for line in str(data.get("content", "")).splitlines() if line.strip()}
            if not incoming:
                raise ValueError("Dữ liệu cookie trống")
            _, cookie_path, cookie_paths = await asyncio.to_thread(cookie_snapshot, True)
            existing = set()
            if cookie_path in cookie_paths:
                try:
                    existing = set(read_binary(cookie_path).decode("utf-8", errors="ignore").splitlines())
                except Exception:
                    pass
            await asyncio.to_thread(write_text_file, cookie_path, "\n".join(sorted(existing | incoming)) + "\n")
            invalidate_content_status_cache()
            await respond(ws, "import_response", channel_id, type="cookie", success=True)
        except Exception as error:
            await respond(ws, "import_response", channel_id, type="cookie", success=False, error_msg=str(error))
    elif event == "import_script":
        try:
            name, content = safe_name(data.get("filename"), "auto.txt"), str(data.get("content", ""))
            if not content:
                raise ValueError("Nội dung script trống")
            saved, errors = 0, []
            for directory in autoexec_dirs(refresh=True):
                try:
                    write_script(directory, name, content)
                    saved += 1
                except Exception as error:
                    errors.append(str(error))
            if not saved:
                raise PermissionError(errors[0] if errors else "Không tìm thấy thư mục Auto Execute có thể ghi")
            invalidate_content_status_cache()
            await respond(ws, "import_response", channel_id, type="script", success=True, savedDirs=saved)
        except Exception as error:
            await respond(ws, "import_response", channel_id, type="script", success=False, error_msg=str(error))
    elif event == "import_config_qh":
        try:
            import_config(data.get("filename"), data.get("content", ""))
            await respond(ws, "import_response", channel_id, type="config_qh", success=True)
        except Exception as error:
            await respond(ws, "import_response", channel_id, type="config_qh", success=False, error_msg=str(error))
    elif event == "edit_config_qh":
        try:
            edit_config(data.get("config", {}), data.get("server_link"))
            await respond(ws, "edit_config_response", channel_id, success=True)
        except Exception as error:
            await respond(ws, "edit_config_response", channel_id, success=False, error_msg=str(error))
    elif event == "del_request":
        deleted = False
        try:
            kind = data.get("type")
            if kind == "cookie":
                cookie_paths, _ = await asyncio.to_thread(resolve_data_paths, True)
                for path in cookie_paths:
                    deleted = remove_path(path) or deleted
                invalidate_content_status_cache()
            elif kind == "switched":
                _, switched_dirs = await asyncio.to_thread(resolve_data_paths, True)
                for directory in switched_dirs:
                    for path in directory_files(directory):
                        if path.lower().endswith(".txt"):
                            deleted = remove_path(path) or deleted
                invalidate_content_status_cache()
            elif kind == "script":
                target = safe_name(data.get("filename"), "")
                for directory in autoexec_dirs():
                    for path in directory_files(directory):
                        if target and os.path.basename(path) == target and remove_path(path):
                            deleted = True
                if deleted:
                    invalidate_content_status_cache()
            await respond(ws, "del_response", channel_id, type=kind, filename=data.get("filename"), deleted=deleted, error_msg="" if deleted else "Không tìm thấy tệp")
        except Exception as error:
            await respond(ws, "del_response", channel_id, type=data.get("type"), deleted=False, error_msg=str(error))
    elif event == "export_request":
        files = []
        try:
            type_value = str(data.get("typeValue", ""))
            archive_name, encoded, files = make_export(type_value, MACHINE_NAME, data.get("filename"))
            await respond(ws, "export_response", channel_id, success=True, typeValue=type_value, filename=archive_name, content=encoded)
            if data.get("delAfter"):
                for path in files:
                    remove_path(path)
        except Exception as error:
            await respond(ws, "export_response", channel_id, success=False, typeValue=data.get("typeValue"), error_msg=str(error))
    elif event == "clone_monitor_request":
        try:
            snapshot = await asyncio.to_thread(clone_monitor_snapshot, data.get("prefix"))
            await respond(ws, "clone_monitor_response", channel_id, success=True, **snapshot)
        except Exception as error:
            await respond(ws, "clone_monitor_response", channel_id, success=False, error_msg=str(error))
    elif event == "screenshot_request":
        try:
            screenshot = await asyncio.to_thread(capture_screenshot)
            await respond(ws, "screenshot_response", channel_id, success=True, **screenshot)
        except Exception as error:
            await respond(ws, "screenshot_response", channel_id, success=False, error_msg=str(error))
    elif event == "restart_request":
        try:
            if os.system("su -c 'echo MCMWMANAGER_ROOT_OK' >/dev/null 2>&1") != 0:
                raise PermissionError("Thiết bị không có quyền Root")
            await respond(ws, "restart_response", channel_id, success=True)
            await asyncio.sleep(1)
            os.system("su -c 'reboot'")
        except Exception as error:
            await respond(ws, "restart_response", channel_id, success=False, error_msg=str(error))
    elif event == "update_client_request":
        try:
            download_url = str(data.get("downloadUrl", "")).strip()
            if not download_url.startswith(("https://", "http://")):
                raise ValueError("Link cập nhật client không hợp lệ")
            request = urllib.request.Request(download_url, headers={"User-Agent": "MCMWMANAGER-Client"})
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read(5 * 1024 * 1024 + 1)
            if not content or len(content) > 5 * 1024 * 1024:
                raise ValueError("Client tải về trống hoặc lớn hơn 5 MB")
            source = content.decode("utf-8")
            compile(source, "client.py", "exec")
            client_path = os.path.abspath(__file__)
            temporary = client_path + ".new"
            with open(temporary, "wb") as output:
                output.write(content)
            os.replace(temporary, client_path)
            await respond(ws, "update_client_response", channel_id, success=True, version=data.get("expectedVersion", "new"))
            await asyncio.sleep(1)
            os.execv(sys.executable, [sys.executable, client_path])
        except Exception as error:
            await respond(ws, "update_client_response", channel_id, success=False, error_msg=str(error))


async def connect():
    async with websockets.connect(WS_URL, ping_interval=None, max_size=24 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"event": "authenticate", "data": {"machineName": MACHINE_NAME, "deviceToken": DEVICE_TOKEN, "clientVersion": CLIENT_VERSION}}))

        async def heartbeat():
            while True:
                health = await asyncio.to_thread(health_snapshot)
                await ws.send(json.dumps({"event": "heartbeat", "data": {"clientVersion": CLIENT_VERSION, "health": health}}))
                await asyncio.sleep(25)

        async def content_scanner():
            while True:
                await asyncio.to_thread(refresh_content_status_cache, True)
                await asyncio.sleep(CONTENT_SCAN_INTERVAL)

        tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(content_scanner())]
        try:
            async for message in ws:
                await handle_message(ws, message)
        finally:
            for task in tasks:
                task.cancel()


async def main():
    if not WS_URL or not DEVICE_TOKEN:
        raise SystemExit("Thiếu WS_URL hoặc device token")
    while True:
        try:
            await connect()
        except Exception as error:
            print("[MCMWMANAGER]", error)
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
