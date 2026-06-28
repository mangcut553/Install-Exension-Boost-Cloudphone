import os
import sys
import time
import shutil
import subprocess
import httpx
import urllib.parse
import zipfile
import sqlite3
import requests
import glob
import builtins
from concurrent.futures import ThreadPoolExecutor, as_completed
from colorama import Fore, Style, init

init(autoreset=True)

# ------------------------------------------------------------
#  Fix terminal thiếu Carriage Return (\r) — một số môi trường
#  Termux/UGPhone chỉ xử lý LF (xuống dòng) mà không tự "lùi về
#  lề trái" (\r), khiến dòng mới bắt đầu ngay tại cột cũ thay vì
#  cột 0 (hiện tượng output bị "zíc zắc"/vỡ layout). Ép mọi _cprint()
#  trong toàn script kết thúc bằng \r\n thay vì \n mặc định.
# ------------------------------------------------------------

def _cprint(*args, **kwargs):
    if "end" not in kwargs:
        kwargs["end"] = "\r\n"
    builtins.print(*args, **kwargs)


FINAL_DIR = "/storage/emulated/0/"
DEST_DIR = "/storage/emulated/0/Download/NexusHideout"

os.makedirs(DEST_DIR, exist_ok=True)
os.makedirs(FINAL_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-S918B Build/QP1A.190711.020) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.7339.51 Mobile Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

TITLE = Fore.CYAN + Style.BRIGHT
SUCCESS = Fore.GREEN + Style.BRIGHT
ERROR = Fore.RED + Style.BRIGHT

os.system("clear")

if not (hasattr(os, "geteuid") and os.geteuid() == 0):
    _cprint(ERROR + "Root Not Detected, Exiting... ")
    sys.exit(1)

lite_packages = [
    "com.android.providers.calendar", "com.android.wallpaper.livepicker", "com.android.soundrecorder",
    "com.android.server.telecom", "com.google.android.apps.nbu.files", "com.android.providers.telephony",
    "com.android.providers.contacts", "com.android.phone", "com.android.emergency", "com.android.egg",
    "com.wsh.toolkit", "com.wsh.appstorage", "com.android.calculator2", "com.android.music",
    "com.android.musicfx", "com.sohu.inputmethod.sogou", "net.sourceforge.opencamera",
    "com.google.android.googlequicksearchbox", "com.google.android.gm", "com.google.android.youtube",
    "com.google.android.apps.docs", "com.google.android.apps.meetings", "com.google.android.apps.maps",
    "com.google.android.apps.photos", "com.google.android.contacts", "com.google.android.calendar",
    "com.google.ar.core", "com.google.android.play.games", "com.google.android.apps.magazines",
    "com.google.android.apps.subscriptions.red", "com.google.android.videos",
    "com.google.android.apps.googleassistant", "com.google.android.apps.messaging",
    "com.google.android.dialer", "com.android.mms", "com.og.toolcenter", "com.og.gamecenter",
    "com.android.contacts", "com.android.calendar", "com.android.calllogbackup", "com.wsh.appstore",
    "com.android.tools", "com.android.quicksearchbox", "com.google.android.apps.gallery",
    "com.google.android.apps.wellbeing", "com.google.android.apps.googleone", "com.sec.android.gallery3d",
    "com.miui.gallery", "com.coloros.gallery3d", "com.vivo.gallery", "com.motorola.gallery",
    "com.transsion.gallery", "com.sonyericsson.album", "com.lge.gallery", "com.htc.album",
    "com.huawei.photos", "com.android.gallery3d", "com.android.gallery", "com.google.android.deskclock",
    "com.sec.android.app.clockpackage", "com.miui.clock", "com.coloros.alarmclock", "com.vivo.alarmclock",
    "com.motorola.timeweatherwidget", "com.android.deskclock", "com.huawei.clock", "com.lge.clock",
    "com.android.email", "com.android.printspooler", "com.android.bookmarkprovider", "com.android.bips",
    "com.android.cellbroadcastreceiver", "com.android.cellbroadcastservice", "com.android.dreams.basic",
    "com.android.dreams.phototable", "com.android.wallpaperbackup", "com.android.wallpapercropper",
    "com.android.wallpaperpicker", "com.android.statementservice", "com.android.hotwordenrollment.okgoogle",
    "com.android.hotwordenrollment.xgoogle", "com.android.sharedstoragebackup", "com.android.stk",
    "com.google.android.tag", "com.android.bluetooth", "com.android.bluetoothmidiservice",
    "com.android.messaging", "com.samsung.android.messaging", "com.android.mms.service",
    "com.miui.smsservice", "com.coloros.mms", "com.android.vending", "com.google.android.gms",
    "com.vivo.message", "com.huawei.message", "com.lge.message", "com.sonyericsson.conversations",
    "com.motorola.messaging", "com.transsion.message"
]

# Launcher packages — KHÔNG đưa vào lite_packages cố định vì có thể đang active.
# Chỉ xoá nếu chắc chắn KHÔNG phải launcher hiện tại đang chạy.
# com.og.launcher = package dùng chung cho cả "OG Launcher" và "Ug Launcher"
# com.android.launcher3 KHÔNG được xoá trong mọi trường hợp.
LAUNCHER_CANDIDATES = ["com.og.launcher"]

# ------------------------------------------------------------
#  Tìm đúng đường dẫn su binary — không phụ thuộc PATH vì khi
#  Python được gọi từ "su -c python3 ...", PATH có thể không
#  chứa thư mục của su gốc (/system/xbin, /sbin, /data/adb...)
# ------------------------------------------------------------
def _find_su():
    for path in ["/system/xbin/su", "/system/bin/su", "/sbin/su",
                 "/su/bin/su", "/magisk/.core/bin/su", "su"]:
        try:
            r = subprocess.run(
                [path, "-c", "id"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, timeout=3
            )
            if b"uid=0" in r.stdout:
                return path
        except Exception:
            continue
    return "su"

_SU_BIN = _find_su()

# ------------------------------------------------------------
#  Performance Boost — chạy 1 lần khi setup, không cần giữ Termux mở
# ------------------------------------------------------------

# Các chuỗi là warning/banner của Termux su wrapper, không phải output thật
_SU_NOISE = [
    "No su program found",
    "Termux does not supply",
    "androidcentral.com",
    "information about rooting",
    "tools for rooting",
    "see e.g.",
]

def run_su(cmd, timeout=10):
    try:
        result = subprocess.run(
            [_SU_BIN, "-c", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout, universal_newlines=True
        )
        # Lọc bỏ các dòng là warning của su wrapper trong CẢ stdout lẫn stderr
        # (một số cloudphone inject banner vào stdout thay vì stderr)
        combined = result.stdout + result.stderr
        lines = [
            line for line in combined.splitlines()
            if not any(noise in line for noise in _SU_NOISE)
        ]
        return "\n".join(lines).strip() or None
    except Exception:
        return None

def write_su(path, value):
    return run_su(f'echo "{value}" > "{path}"') is not None

def read_su(path):
    # Thử đọc trực tiếp bằng Python trước (nhanh hơn, không qua su wrapper)
    try:
        with open(path, "r") as f:
            val = f.read().strip()
            if val:
                return val
    except Exception:
        pass
    # Fallback qua su nếu không đọc được (permission denied)
    out = run_su(f'cat "{path}" 2>/dev/null')
    return out if out else None

def get_cpu_cores():
    # Đọc trực tiếp từ sysfs — không cần su vì /sys/devices/system/cpu thường readable
    cores = glob.glob("/sys/devices/system/cpu/cpu[0-9]*")
    cores = [c for c in cores if os.path.basename(c)[3:].isdigit()]
    cores.sort(key=lambda p: int(os.path.basename(p)[3:]))
    return cores

def get_available_governors(core_path):
    out = read_su(f"{core_path}/cpufreq/scaling_available_governors")
    return out.split() if out else []

def get_max_freq(core_path):
    out = read_su(f"{core_path}/cpufreq/cpuinfo_max_freq")
    # Strip hết whitespace và ký tự lạ trước khi check isdigit
    out = out.strip() if out else None
    return int(out) if out and out.isdigit() else None

def boost_cpu_once():
    _cprint(TITLE + "Dang Boost CPU (performance + max freq lock)...")
    cores = get_cpu_cores()
    if not cores:
        _cprint(ERROR + "Khong tim thay CPU cores trong sysfs.")
        return
    for core in cores:
        governors = get_available_governors(core)
        if "performance" in governors:
            write_su(f"{core}/cpufreq/scaling_governor", "performance")
        max_freq = get_max_freq(core)
        if max_freq:
            write_su(f"{core}/cpufreq/scaling_min_freq", max_freq)
            write_su(f"{core}/cpufreq/scaling_max_freq", max_freq)
    _cprint(SUCCESS + f"Da Boost {len(cores)} CPU cores.")

    # --- schedtune / stune boost (nếu kernel hỗ trợ) ---
    # Boost top-app + foreground cgroup để Roblox/tool được ưu tiên scheduler
    stune_targets = {
        "/dev/stune/top-app/schedtune.boost":       "100",
        "/dev/stune/top-app/schedtune.prefer_idle": "1",
        "/dev/stune/foreground/schedtune.boost":    "50",
        "/dev/stune/foreground/schedtune.prefer_idle": "1",
    }
    stune_ok = 0
    for path, val in stune_targets.items():
        if write_su(path, val):
            stune_ok += 1
    if stune_ok:
        _cprint(SUCCESS + f"schedtune boost: {stune_ok}/{len(stune_targets)} nodes da set.")

    # --- sched_boost kernel param ---
    # Hint kernel ưu tiên performance thay vì power saving
    for p in ("/proc/sys/kernel/sched_boost",
              "/sys/module/cpu_boost/parameters/input_boost_enabled"):
        write_su(p, "1")

    # --- Scheduler latency tuning (giảm jitter khi nhiều tab) ---
    vm_writes = {
        "/proc/sys/kernel/sched_min_granularity_ns":      "3000000",
        "/proc/sys/kernel/sched_wakeup_granularity_ns":   "4000000",
        "/proc/sys/kernel/sched_migration_cost_ns":       "5000000",
        "/proc/sys/kernel/sched_latency_ns":              "24000000",
        "/proc/sys/kernel/sched_nr_migrate":              "64",
        "/proc/sys/kernel/sched_child_runs_first":        "1",
    }
    sched_ok = 0
    for path, val in vm_writes.items():
        if write_su(path, val):
            sched_ok += 1
    if sched_ok:
        _cprint(SUCCESS + f"Scheduler tuning: {sched_ok}/{len(vm_writes)} params da set.")


GPU_PATH_CANDIDATES = {
    "adreno": ["/sys/class/kgsl/kgsl-3d0"],
    "mali": [
        "/sys/devices/platform/mali",
        "/sys/devices/platform/13000000.mali",
        "/sys/class/misc/mali0",
        "/sys/kernel/gpu",
    ],
}

def detect_gpu():
    for path in GPU_PATH_CANDIDATES["adreno"]:
        if os.path.isdir(path) or read_su(f"{path}/gpu_busy_percentage") is not None:
            return "adreno", path
    for path in GPU_PATH_CANDIDATES["mali"]:
        if os.path.isdir(path):
            return "mali", path
        if run_su(f'[ -d "{path}" ] && echo yes') == "yes":
            return "mali", path
    return None, None

def boost_gpu_once():
    _cprint(TITLE + "Dang do & Boost GPU (Adreno/Mali)...")
    gpu_type, base = detect_gpu()
    if not gpu_type:
        _cprint(ERROR + "Khong tim thay GPU path trong sysfs.")
        return

    if gpu_type == "adreno":
        max_gpuclk = read_su(f"{base}/max_gpuclk")
        write_su(f"{base}/min_pwrlevel", "0")
        write_su(f"{base}/max_pwrlevel", "0")
        write_su(f"{base}/devfreq/governor", "performance")
        if max_gpuclk:
            write_su(f"{base}/gpuclk", max_gpuclk)
        # Giữ GPU luôn bật — không cho sleep khi nhiều tab Roblox
        write_su(f"{base}/force_clk_on",  "1")
        write_su(f"{base}/force_bus_on",  "1")
        write_su(f"{base}/force_rail_on", "1")
        write_su(f"{base}/idle_timer",    "0")
        _cprint(SUCCESS + f"Adreno GPU Boost max clock + force-on (clk={max_gpuclk or 'N/A'})")

    elif gpu_type == "mali":
        for p in (f"{base}/dvfs_governor", f"{base}/power_policy"):
            write_su(p, "performance")
        max_freq = read_su(f"{base}/max_freq") or read_su(f"{base}/gpu_clock_max")
        if max_freq:
            write_su(f"{base}/min_freq", max_freq)
        # Mali: tắt idle / autosuspend
        write_su(f"{base}/dvfs_enable", "0")
        write_su(f"{base}/idle_hysteresis_time_ms", "0")
        for pm_path in (
            f"{base}/power/autosuspend_delay_ms",
            f"{base}/../power/autosuspend_delay_ms",
        ):
            write_su(pm_path, "-1")
        _cprint(SUCCESS + f"Mali GPU Boost performance + force-on (max_freq={max_freq or 'N/A'})")


def boost_ram_once():
    _cprint(TITLE + "Dang Boost RAM (vm tuning + kill background)...")

    # vm params — an toàn cho multi-tab, KHÔNG drop cache
    vm_params = {
        "/proc/sys/vm/swappiness":               "0",    # ưu tiên RAM thật, không swap
        "/proc/sys/vm/vfs_cache_pressure":       "50",   # giữ dentry/inode cache lâu hơn
        "/proc/sys/vm/dirty_ratio":              "5",    # flush dirty pages sớm
        "/proc/sys/vm/dirty_background_ratio":   "2",
        "/proc/sys/vm/dirty_expire_centisecs":   "500",
        "/proc/sys/vm/dirty_writeback_centisecs":"500",
        "/proc/sys/vm/min_free_kbytes":          "16384",# giữ buffer free tối thiểu 16MB
        "/proc/sys/vm/overcommit_memory":        "1",    # cho phép overcommit (Roblox cần)
        "/proc/sys/vm/page-cluster":             "0",    # đọc từng page, không prefetch swap
        "/proc/sys/vm/stat_interval":            "10",   # giảm overhead cập nhật vmstat
        "/proc/sys/vm/oom_kill_allocating_task": "1",    # OOM kill nhanh, không freeze
    }
    ok = 0
    for path, val in vm_params.items():
        if write_su(path, val):
            ok += 1
    _cprint(SUCCESS + f"VM tuning: {ok}/{len(vm_params)} params da set.")

    # Kill background apps không cần thiết (am kill-all chỉ kill cached/empty processes)
    try:
        r = subprocess.run(
            ["am", "kill-all"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=10
        )
        _cprint(SUCCESS + "am kill-all: da giai phong background processes.")
    except Exception as e:
        _cprint(ERROR + f"am kill-all that bai: {e}")

    # Compact memory — gộp free pages, giảm phân mảnh (an toàn, không drop cache)
    write_su("/proc/sys/vm/compact_memory", "1")
    _cprint(SUCCESS + "Memory compaction da chay.")


def boost_io_once():
    _cprint(TITLE + "Dang Boost I/O scheduler...")
    # Tìm tất cả block devices (mmcblk, sda, nvme, ufs...)
    block_devs = glob.glob("/sys/block/mmcblk*") + glob.glob("/sys/block/sd*") + \
                 glob.glob("/sys/block/nvme*") + glob.glob("/sys/block/sdc*")

    io_ok = 0
    for dev in block_devs:
        dev_name = os.path.basename(dev)
        sched_path = f"{dev}/queue/scheduler"
        avail = read_su(sched_path) or ""
        # Ưu tiên: none > noop > deadline (tốt nhất cho flash storage, không queue delay)
        chosen = None
        for pref in ("none", "noop", "deadline", "mq-deadline"):
            if pref in avail:
                chosen = pref
                break
        if chosen:
            write_su(sched_path, chosen)
            io_ok += 1

        # read_ahead_kb = 512 — balance giữa sequential read và latency
        write_su(f"{dev}/queue/read_ahead_kb", "512")
        # Tắt rotational hint (SSD/flash, không cần elevator)
        write_su(f"{dev}/queue/rotational", "0")
        # nr_requests tăng để tránh I/O stall khi nhiều tab
        write_su(f"{dev}/queue/nr_requests", "256")

    if io_ok:
        _cprint(SUCCESS + f"I/O scheduler set tren {io_ok} block devices.")
    else:
        _cprint(Fore.YELLOW + "Khong tim thay block device de tune I/O (co the la UFS/virtio).")


def set_resolution_480p():
    """Đổi resolution về 480p với DPI 160 — giảm tải GPU đáng kể."""
    _cprint(TITLE + "Setting resolution 480p...")
    try:
        subprocess.run(["su", "-c", "wm size 480x1080 && wm density 160"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, timeout=5)
        _cprint(SUCCESS + "Resolution: 480x1080 | DPI: 160")
    except Exception as e:
        _cprint(ERROR + f"Resolution failed: {e}")

def boost_animations_off():
    """Tắt toàn bộ animation scale — UI phản hồi tức thì, giảm tải CPU."""
    _cprint(TITLE + "Disabling animations...")
    cmds = [
        "settings put global window_animation_scale 0",
        "settings put global transition_animation_scale 0",
        "settings put global animator_duration_scale 0",
    ]
    for cmd in cmds:
        subprocess.run(["su", "-c", cmd],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, timeout=5)
    _cprint(SUCCESS + "Animations disabled")

def boost_vm_heap():
    """Tăng heap size cho Dalvik/ART — giảm GC pause gây giật khi chơi game."""
    _cprint(TITLE + "Tuning VM heap...")
    props = [
        "setprop dalvik.vm.heapsize 512m",
        "setprop dalvik.vm.heapgrowthlimit 256m",
        "setprop dalvik.vm.heapminfree 4m",
        "setprop dalvik.vm.heapmaxfree 16m",
    ]
    for cmd in props:
        subprocess.run(["su", "-c", cmd],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, timeout=5)
    _cprint(SUCCESS + "VM heap tuned (512m)")

def boost_tcp():
    """TCP BBR + fast open — giảm latency mạng khi chơi game online."""
    _cprint(TITLE + "Tuning TCP...")
    cmds = [
        "sysctl -w net.ipv4.tcp_fastopen=3",
        "sysctl -w net.ipv4.tcp_congestion_control=bbr 2>/dev/null || true",
        "sysctl -w net.ipv4.tcp_no_metrics_save=1",
        "sysctl -w net.ipv4.tcp_timestamps=0",
    ]
    for cmd in cmds:
        subprocess.run(["su", "-c", cmd],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, timeout=5)
    _cprint(SUCCESS + "TCP tuned")

def boost_all_tabs_oom():
    """Boost OOM priority cho TẤT CẢ tab ugphone/roblox đang chạy — tránh bị Android kill khi treo nhiều tab."""
    _cprint(TITLE + "Boosting OOM priority for all tabs...")
    boosted = 0
    try:
        r = subprocess.run(["su", "-c", "ps -A"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=10,
                           universal_newlines=True)
        target_keywords = ["ugphone", "roblox", "com.og"]
        for line in r.stdout.splitlines():
            if any(kw in line.lower() for kw in target_keywords):
                parts = line.split()
                if len(parts) >= 2:
                    pid = parts[1]
                    if pid.isdigit():
                        subprocess.run(
                            ["su", "-c", f"echo -900 > /proc/{pid}/oom_score_adj 2>/dev/null"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, timeout=3
                        )
                        boosted += 1
    except Exception:
        pass
    _cprint(SUCCESS + f"OOM boosted for {boosted} tab(s)")

def run_performance_boost_once():
    _cprint(TITLE + "\nPERFORMANCE BOOST (1 lan)")
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)
    boost_cpu_once()
    boost_gpu_once()
    boost_ram_once()
    boost_io_once()
    boost_animations_off()
    boost_vm_heap()
    boost_tcp()
    set_resolution_480p()
    boost_all_tabs_oom()
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)
    _cprint(SUCCESS + "Performance Boost hoan tat.\n")


def get_prop(name):
    # Thử gọi getprop trực tiếp trước (không qua su wrapper để tránh banner noise)
    try:
        r = subprocess.run(
            ["getprop", name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=5,
            universal_newlines=True
        )
        val = r.stdout.strip()
        if val and not any(noise in val for noise in _SU_NOISE):
            return val
    except Exception:
        pass
    # Fallback qua su
    out = run_su(f"getprop {name}")
    return out if out else "N/A"

def get_cpu_current_freq(core_path):
    out = read_su(f"{core_path}/cpufreq/scaling_cur_freq")
    out = out.strip() if out else None
    return int(out) if out and out.isdigit() else None

def get_cpu_governor(core_path):
    out = read_su(f"{core_path}/cpufreq/scaling_governor")
    return out.strip() if out else "N/A"

def summarize_cores(cores):
    """Gộp các core có cùng (max_freq, governor) thành 1 dòng, tránh in 8 dòng riêng
    gây vỡ layout trên màn hình Termux hẹp. Trả về list dòng tóm tắt."""
    groups = {}
    for core in cores:
        max_freq = get_max_freq(core)
        cur_freq = get_cpu_current_freq(core)
        governor = get_cpu_governor(core)
        max_mhz = round(max_freq / 1000) if max_freq else None
        cur_mhz = round(cur_freq / 1000) if cur_freq else None
        key = (max_mhz, governor)
        groups.setdefault(key, []).append(cur_mhz)

    lines = []
    for (max_mhz, governor), freqs in sorted(groups.items(), key=lambda x: (x[0][0] or 0)):
        count = len(freqs)
        avg_cur = round(sum(f for f in freqs if f) / len([f for f in freqs if f])) if any(freqs) else None
        lines.append(f"{count} core @ {avg_cur or 'N/A'}/{max_mhz or 'N/A'} MHz ({governor})")
    return lines

def print_device_info():
    """In thông tin phần cứng gọn: SoC, cores (gộp theo cluster), GPU."""
    _cprint(TITLE + "\nDevice Info")
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)

    soc_model = get_prop("ro.soc.model")
    board = get_prop("ro.product.board") or get_prop("ro.board.platform")
    manufacturer = get_prop("ro.product.manufacturer")
    model = get_prop("ro.product.model")
    android_ver = get_prop("ro.build.version.release")

    _cprint(Fore.WHITE + f"{manufacturer} {model}")
    _cprint(Fore.WHITE + f"Android {android_ver}")
    _cprint(Fore.WHITE + f"SoC: {soc_model if soc_model != 'N/A' else board}")

    cores = get_cpu_cores()
    _cprint(Fore.WHITE + f"Cores: {len(cores)}")
    for line in summarize_cores(cores):
        _cprint(Fore.GREEN + line)

    gpu_type, gpu_base = detect_gpu()
    if gpu_type == "adreno":
        gpu_clk = read_su(f"{gpu_base}/gpuclk")
        gpu_busy = read_su(f"{gpu_base}/gpu_busy_percentage")
        _cprint(Fore.WHITE + f"GPU: Adreno {gpu_clk or 'N/A'} Hz, load {gpu_busy or 'N/A'}%")
    elif gpu_type == "mali":
        mali_freq = read_su(f"{gpu_base}/cur_freq") or read_su(f"{gpu_base}/clock")
        _cprint(Fore.WHITE + f"GPU: Mali {mali_freq or 'N/A'}")
    else:
        _cprint(Fore.WHITE + "GPU: N/A")

    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)

def get_uptime_seconds():
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return None

def format_duration(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if not days: parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"

def get_battery_health():
    out = run_su("dumpsys battery")
    if not out:
        return {}
    info = {}
    for line in out.splitlines():
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            info[key.strip()] = value.strip()
    return info

def estimate_device_freshness():
    """Ước lượng % 'độ mới' của máy dựa trên uptime liên tục và tình trạng pin.
    Đây là chỉ số tham khảo (heuristic), không phải con số chính xác tuyệt đối
    vì Android không cung cấp API đo 'độ hao mòn' trực tiếp như iOS Battery Health."""
    uptime = get_uptime_seconds()
    battery = get_battery_health()

    score = 100
    notes = []

    if uptime:
        uptime_days = uptime / 86400
        if uptime_days > 7:
            penalty = min(15, round(uptime_days / 3))
            score -= penalty
            notes.append(f"Uptime dài ({format_duration(uptime)}) - nên reboot định kỳ")

    level = battery.get("level")
    scale = battery.get("scale")
    health = battery.get("health")
    temp = battery.get("temperature")

    if health and health.isdigit():
        # health code Android: 2=GOOD, 3=OVERHEAT, 4=DEAD, 5=OVER_VOLTAGE, 6=UNSPECIFIED_FAILURE, 7=COLD
        health_map = {"2": "GOOD", "3": "OVERHEAT", "4": "DEAD", "5": "OVER_VOLTAGE", "6": "FAILURE", "7": "COLD"}
        health_str = health_map.get(health, "UNKNOWN")
        if health_str != "GOOD":
            score -= 20
            notes.append(f"Pin health: {health_str}")

    if temp and temp.isdigit():
        temp_c = int(temp) / 10
        if temp_c >= 40:
            score -= 10
            notes.append(f"Nhiet do pin cao: {temp_c:.1f}C")

    score = max(0, min(100, score))
    return score, notes, uptime, level, scale

def print_device_freshness():
    _cprint(TITLE + "\nDevice Health")
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)

    score, notes, uptime, level, scale = estimate_device_freshness()

    color = Fore.GREEN if score >= 80 else (Fore.YELLOW if score >= 50 else Fore.RED)
    _cprint(color + Style.BRIGHT + f"Trạng Thái Của Máy: ~{score}%")

    uptime_str = format_duration(uptime) if uptime is not None else "N/A"
    pin_str = f"{round(int(level) / int(scale) * 100)}%" if (level and scale and level.isdigit() and scale.isdigit()) else "N/A"
    _cprint(Fore.WHITE + f"Uptime: {uptime_str}")
    _cprint(Fore.WHITE + f"Pin: {pin_str}")

    for note in notes:
        _cprint(Fore.YELLOW + f"- {note}")

    if not notes:
        _cprint(Fore.GREEN + "ngon nha ni-JackPot")

    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)

def get_ram_info():
    """Đọc tổng RAM và RAM khả dụng từ /proc/meminfo, trả về (total_mb, available_mb) hoặc (None, None)."""
    try:
        info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    value_kb = int(parts[1].strip().split()[0])
                    info[key] = value_kb

        total_kb = info.get("MemTotal")
        available_kb = info.get("MemAvailable")

        if available_kb is None:
            free_kb = info.get("MemFree", 0)
            cached_kb = info.get("Cached", 0)
            buffers_kb = info.get("Buffers", 0)
            available_kb = free_kb + cached_kb + buffers_kb

        if total_kb is None:
            return None, None

        return round(total_kb / 1024), round(available_kb / 1024)
    except Exception:
        return None, None

def print_ram_status():
    total_mb, available_mb = get_ram_info()
    if total_mb is None:
        _cprint(ERROR + "Không đọc được thông tin RAM.")
        return
    used_mb = total_mb - available_mb
    percent_used = round((used_mb / total_mb) * 100, 1) if total_mb else 0
    _cprint(TITLE + "\nRAM Status")
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)
    _cprint(Fore.WHITE + f"Total: {total_mb} MB")
    _cprint(Fore.GREEN + f"Available: {available_mb} MB")
    _cprint(Fore.WHITE + f"Using: {used_mb} MB ({percent_used}%)")
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)

def run(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL
    )

ERROR_LOG_PATH = os.path.join(DEST_DIR, "error_log.txt")

def _log_error(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

_DNS_SERVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "208.67.222.222"]
_dns_changed = False

def _set_dns(dns):
    try:
        for prop in ("net.dns1", "net.dns2", "dhcp.eth0.dns1", "dhcp.wlan0.dns1"):
            run_su(f"setprop {prop} {dns}")
        run_su("ndc resolver flushnet 100 2>/dev/null || true")
        run_su("ndc resolver clearnetdns 100 2>/dev/null || true")
        time.sleep(1)
        return True
    except Exception:
        return False

def _check_dns():
    try:
        r = subprocess.run(
            ["getent", "hosts", "www.dropbox.com"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["nslookup", "www.dropbox.com"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False

def _try_fix_dns():
    global _dns_changed
    for dns in _DNS_SERVERS:
        _cprint(TITLE + f"Thu doi DNS sang {dns}...")
        _set_dns(dns)
        if _check_dns():
            _cprint(SUCCESS + f"DNS {dns} hoat dong!")
            _dns_changed = True
            return True
        _cprint(ERROR + f"DNS {dns} khong resolve duoc, thu tiep...")
    _cprint(ERROR + "Tat ca DNS deu that bai.")
    return False

def download(url, dst, retries=6):
    # Nếu file đã tồn tại và có dung lượng → dùng lại
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        _cprint(SUCCESS + f"Da co san: {os.path.basename(dst)}")
        return True

    delays = [5, 5, 10, 10, 15, 15]
    temp_dst = dst + ".part"

    DNS_ERRORS = (
        "No address associated with hostname",
        "Name or service not known",
        "Temporary failure in name resolution",
        "getaddrinfo",
    )
    NETWORK_ERRORS = (
        "Network is unreachable",
        "Connection refused",
    )

    _dns_fixed = False  # chỉ thử fix DNS 1 lần per file

    for attempt in range(1, retries + 1):
        try:
            current_pos = os.path.getsize(temp_dst) if os.path.exists(temp_dst) else 0
            headers = {**HEADERS, "Range": f"bytes={current_pos}-"} if current_pos > 0 else HEADERS

            with httpx.Client(verify=False, timeout=60.0, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416:
                        break
                    r.raise_for_status()
                    mode = "ab" if current_pos > 0 else "wb"
                    with open(temp_dst, mode) as f:
                        for chunk in r.iter_bytes(chunk_size=1024*1024):
                            f.write(chunk)

            if os.path.exists(temp_dst):
                os.rename(temp_dst, dst)
            _cprint(SUCCESS + f"Downloaded: {os.path.basename(dst)}")
            return True

        except Exception as e:
            err_str = str(e)

            # Lỗi DNS: thử đổi DNS 1 lần rồi retry ngay
            if any(sig in err_str for sig in DNS_ERRORS):
                _cprint(ERROR + f"Loi DNS ({attempt}/{retries}): {e}")
                if not _dns_fixed:
                    _dns_fixed = True
                    if _try_fix_dns():
                        continue  # retry ngay sau khi đổi DNS
                # DNS fix thất bại hoặc đã fix rồi vẫn lỗi → skip
                _log_error(f"DNS fail, skip: {os.path.basename(dst)} | {url} | {e}")
                _cprint(ERROR + f"Skip {os.path.basename(dst)} — da ghi error_log.txt")
                if os.path.exists(temp_dst): os.remove(temp_dst)
                return False

            # Lỗi network cứng → skip ngay
            if any(sig in err_str for sig in NETWORK_ERRORS):
                _log_error(f"Network fail, skip: {os.path.basename(dst)} | {url} | {e}")
                _cprint(ERROR + f"Loi mang — skip {os.path.basename(dst)}: {e}")
                if os.path.exists(temp_dst): os.remove(temp_dst)
                return False

            if attempt < retries:
                wait = delays[attempt - 1]
                _cprint(ERROR + f"Download Failed ({attempt}/{retries}): {e} — Retry In {wait}s")
                time.sleep(wait)
            else:
                _log_error(f"Failed {retries} attempts: {os.path.basename(dst)} | {url} | {e}")
                _cprint(ERROR + f"Download Failed After {retries} Attempts: {url}")
                if os.path.exists(temp_dst): os.remove(temp_dst)
                return False

def install(p, retries=6):
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        _cprint(ERROR + f"Skip install (file khong ton tai): {os.path.basename(p)}")
        return False
    delays = [5, 5, 10, 10, 15, 15]
    for attempt in range(1, retries + 1):
        try:
            result = run(["pm", "install", "-r", p])
            out = (result.stdout or b"").decode(errors="ignore").strip()
            err = (result.stderr or b"").decode(errors="ignore").strip()
            combined = out + " " + err
            if result.returncode == 0 or "Success" in combined:
                _cprint(SUCCESS + f"Installed: {os.path.basename(p)}")
                return True
            else:
                raise Exception(err or out or "Unknown error")
        except Exception as e:
            if attempt < retries:
                wait = delays[attempt - 1]
                _cprint(ERROR + f"Install Failed ({attempt}/{retries}): {e} — Retry In {wait}s")
                time.sleep(wait)
            else:
                _log_error(f"Install failed {retries} attempts: {os.path.basename(p)} | {e}")
                _cprint(ERROR + f"Install Failed After {retries} Attempts: {p}")
                return False

def is_installed(pkg):
    # Thử pm list packages trực tiếp trước
    try:
        r = subprocess.run(
            ["pm", "list", "packages", pkg],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=10,
            universal_newlines=True
        )
        if f"package:{pkg}" in r.stdout:
            return True
    except Exception:
        pass
    # Fallback qua su
    out = run_su(f"pm list packages {pkg}")
    return bool(out and f"package:{pkg}" in out)

cloudflare_path = os.path.join(DEST_DIR, "com-cloudflare-onedotonedotonedotone-3837-66752135-ef8b2f5f382404189163d4d14c3128a8 (1).apk")
termuxboot_path = os.path.join(DEST_DIR, "com.termux.boot_1000.apk")
mtmanager_path = os.path.join(DEST_DIR, "MT-Manager_2.26.5.apk")
smartlauncher_path = os.path.join(DEST_DIR, "SmartLauncher_latest.apk")
controlscreen_path = os.path.join(DEST_DIR, "control-screen-orientation-4-4.apk")
uglauncher_path = os.path.join(DEST_DIR, "Launcher_14.0.apk")
zarchiver_path = os.path.join(DEST_DIR, "ZArchiver_1.0.10.apk")
gboard_path = os.path.join(DEST_DIR, "Gboard_17.6.5.apk")

apps = [
    ("https://www.dropbox.com/scl/fi/jhxtc2ai2kog2stflzkio/MT-Manager_2.26.5.apk?rlkey=wyfe0iupi7bp7o6uqiqlr5g2d&dl=1", mtmanager_path, "bin.mt.plus.canary"),
    ("https://f-droid.org/repo/com.termux.boot_1000.apk", termuxboot_path, "com.termux.boot"),
    ("https://sl-builds.nyc3.cdn.digitaloceanspaces.com/sl-releases/SmartLauncher_latest.apk", smartlauncher_path, "ginlemon.flowerfree"),
    ("https://www.dropbox.com/scl/fi/0xltersi5nwb019k0s6by/control-screen-orientation-4-4.apk?rlkey=6qj2kvg4x80bauwywsw8cf8my&st=4ub1dbqc&dl=1", controlscreen_path, "ahapps.controlthescreenorientation"),
    ("https://www.dropbox.com/scl/fi/576vqijqv7rc2ud3e9q75/com-cloudflare-onedotonedotonedotone-3837-66752135-ef8b2f5f382404189163d4d14c3128a8.apk?rlkey=ve4kjmjbb7jz9giulxdd7z38m&st=ka5j6bjj&dl=1", cloudflare_path, "com.cloudflare.onedotonedotonedotone"),
    ("https://www.dropbox.com/scl/fi/x5kcxghi47n841rh6ijh6/Launcher_14.0.apk?rlkey=kjg2m7sgmgrom21gyiv1z7o09&st=yq5thz7e&dl=1", uglauncher_path, "com.og.launcher"),
    ("https://www.dropbox.com/scl/fi/axmd6svh7krau02gmbfo9/ZArchiver_1.0.10.apk?rlkey=wxr3ycc66qbohhw5dcvsisnfo&dl=1", zarchiver_path, "ru.zdevs.zarchiver"),
    ("https://www.dropbox.com/scl/fi/vwpx52d9j073awgf4dwhw/Gboard-the-Google-Keyboard_17.6.5.924672101-beta-arm64-v8a_APKPure.apk?rlkey=jj692c3ofs5u4yvv6vto52m0j&dl=1", gboard_path, "com.google.android.inputmethod.latin"),
]

def set_default_launcher(pkg):
    run(["cmd", "package", "set-home-activity", pkg])
    run(["am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"])

def get_current_launcher():
    # Thử trực tiếp trước
    try:
        r = subprocess.run(
            ["cmd", "package", "resolve-activity", "--brief",
             "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=10,
            universal_newlines=True
        )
        val = r.stdout.strip()
        if val and not any(noise in val for noise in _SU_NOISE):
            return val
    except Exception:
        pass
    out = run_su("cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME")
    return out if out else ""

def confirm_launcher_switch(pkg, max_wait=6, poll_interval=1):
    """Chờ chủ động (thay vì sleep cố định) tới khi get_current_launcher() phản ánh
    đúng package vừa set, tối đa max_wait giây. Trả về True nếu xác nhận thành công."""
    waited = 0
    while waited < max_wait:
        if pkg in get_current_launcher():
            return True
        time.sleep(poll_interval)
        waited += poll_interval
    return False

os.system("clear")
_cprint(TITLE + "Launcher Setup")
_cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)
current = get_current_launcher()
if current:
    _cprint(Fore.WHITE + f"Current launcher: {current}")
_cprint()
_cprint(Fore.GREEN + Style.BRIGHT + "[1] Giu nguyen launcher hien tai")
_cprint(Fore.CYAN + Style.BRIGHT + "[2] Chuyen sang Smart Launcher")
_cprint(Fore.MAGENTA + Style.BRIGHT + "[3] Chuyen sang Ug Launcher")
_cprint(Fore.YELLOW + Style.BRIGHT + "[4] Boost device only (khong cai app, chi boost)")
_cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)

while True:
    _cprint(Fore.WHITE + Style.BRIGHT + "Chon (1/2/3/4): ", end="")
    sys.stdout.flush()
    choice = sys.stdin.readline().strip()

    if choice in ("1", "2", "3", "4"):
        break
    _cprint(ERROR + "Nhap 1, 2, 3 hoac 4")

USE_SMART_LAUNCHER = (choice == "2")
USE_UG_LAUNCHER = (choice == "3")
BOOST_ONLY = (choice == "4")

# Nếu chọn Smart Launcher hoặc Ug Launcher thì bắt buộc phải cài, dù đã cài rồi cũng force reinstall
if USE_SMART_LAUNCHER:
    smart_app = next(a for a in apps if a[2] == "ginlemon.flowerfree")
    if not is_installed("ginlemon.flowerfree"):
        _cprint(TITLE + "Downloading Smart Launcher...")
        download(smart_app[0], smart_app[1])
        install(smart_app[1])
    _cprint(TITLE + "Setting Smart Launcher as default...")
    set_default_launcher("ginlemon.flowerfree")
    if confirm_launcher_switch("ginlemon.flowerfree"):
        _cprint(SUCCESS + "Smart Launcher đã được đặt làm launcher mặc định!")
    else:
        _cprint(ERROR + "Không xác nhận được Smart Launcher đã active — kiểm tra lại thủ công.")

    # Xoá com.og.launcher cũ (signature cũ) để tránh INSTALL_FAILED_UPDATE_INCOMPATIBLE
    if is_installed("com.og.launcher"):
        _cprint(TITLE + "Xoa com.og.launcher cu...")
        run(["pm", "clear", "--user", "0", "com.og.launcher"])
        run(["pm", "uninstall", "com.og.launcher"])
        run(["pm", "uninstall", "--user", "0", "com.og.launcher"])

elif USE_UG_LAUNCHER:
    ug_app = next(a for a in apps if a[2] == "com.og.launcher")

    if "com.og.launcher" in current:
        _cprint(TITLE + "Đang dùng com.og.launcher — chuyển tạm qua Smart Launcher trước...")
        smart_app = next(a for a in apps if a[2] == "ginlemon.flowerfree")
        if not is_installed("ginlemon.flowerfree"):
            _cprint(TITLE + "Downloading Smart Launcher...")
            download(smart_app[0], smart_app[1])
            install(smart_app[1])
        set_default_launcher("ginlemon.flowerfree")
        if confirm_launcher_switch("ginlemon.flowerfree"):
            _cprint(SUCCESS + "Đã chuyển tạm sang Smart Launcher.")
        else:
            _cprint(ERROR + "Không xác nhận được Smart Launcher đã active, vẫn tiếp tục...")

    # Xoá com.og.launcher cũ (signature cũ) để tránh INSTALL_FAILED_UPDATE_INCOMPATIBLE
    if is_installed("com.og.launcher"):
        _cprint(TITLE + "Xoa com.og.launcher cu...")
        run(["pm", "clear", "--user", "0", "com.og.launcher"])
        run(["pm", "uninstall", "com.og.launcher"])
        run(["pm", "uninstall", "--user", "0", "com.og.launcher"])

    _cprint(TITLE + "Downloading Ug Launcher...")
    download(ug_app[0], ug_app[1])
    install(ug_app[1])
    _cprint(TITLE + "Setting Ug Launcher as default...")
    set_default_launcher("com.og.launcher")
    if confirm_launcher_switch("com.og.launcher"):
        _cprint(SUCCESS + "Ug Launcher đã được đặt làm launcher mặc định!")
    else:
        _cprint(ERROR + "Không xác nhận được Ug Launcher đã active — kiểm tra lại thủ công.")

elif BOOST_ONLY:
    # Boost Only: chỉ xử lý launcher nếu đang dùng com.og.launcher (cần đổi trước khi xoá/cài lại)
    ug_app = next(a for a in apps if a[2] == "com.og.launcher")
    if "com.og.launcher" in current:
        _cprint(TITLE + "Boost Only — Dang dung com.og.launcher, chuyen tam qua Smart Launcher...")
        smart_app = next(a for a in apps if a[2] == "ginlemon.flowerfree")
        if not is_installed("ginlemon.flowerfree"):
            _cprint(TITLE + "Downloading Smart Launcher...")
            download(smart_app[0], smart_app[1])
            install(smart_app[1])
        set_default_launcher("ginlemon.flowerfree")
        if confirm_launcher_switch("ginlemon.flowerfree"):
            _cprint(SUCCESS + "Da chuyen tam sang Smart Launcher.")
        else:
            _cprint(ERROR + "Khong xac nhan duoc Smart Launcher da active, van tiep tuc...")

    if is_installed("com.og.launcher"):
        _cprint(TITLE + "Xoa com.og.launcher cu...")
        run(["pm", "clear", "--user", "0", "com.og.launcher"])
        run(["pm", "uninstall", "com.og.launcher"])
        run(["pm", "uninstall", "--user", "0", "com.og.launcher"])

    _cprint(TITLE + "Downloading Ug Launcher (moi)...")
    download(ug_app[0], ug_app[1])
    install(ug_app[1])
    _cprint(TITLE + "Setting Ug Launcher as default...")
    set_default_launcher("com.og.launcher")
    if confirm_launcher_switch("com.og.launcher"):
        _cprint(SUCCESS + "Ug Launcher moi da duoc dat lam launcher mac dinh!")
    else:
        _cprint(ERROR + "Khong xac nhan duoc Ug Launcher da active — kiem tra lai thu cong.")

else:
    if "com.og.launcher" in current:
        _cprint(TITLE + "Dang dung com.og.launcher — xoa va cai lai Ug Launcher moi...")
        run(["pm", "clear", "--user", "0", "com.og.launcher"])
        run(["pm", "uninstall", "com.og.launcher"])
        run(["pm", "uninstall", "--user", "0", "com.og.launcher"])
        ug_app = next(a for a in apps if a[2] == "com.og.launcher")
        download(ug_app[0], ug_app[1])
        install(ug_app[1])
        set_default_launcher("com.og.launcher")
        if confirm_launcher_switch("com.og.launcher"):
            _cprint(SUCCESS + "Ug Launcher moi da duoc dat lam launcher mac dinh!")
        else:
            _cprint(ERROR + "Khong xac nhan duoc Ug Launcher da active — kiem tra lai thu cong.")
    elif is_installed("com.og.launcher"):
        _cprint(TITLE + "Xoa com.og.launcher cu...")
        run(["pm", "clear", "--user", "0", "com.og.launcher"])
        run(["pm", "uninstall", "com.og.launcher"])
        run(["pm", "uninstall", "--user", "0", "com.og.launcher"])
    _cprint(SUCCESS + "Giu nguyen launcher hien tai.")

os.system("clear")

# Bỏ các launcher không được chọn ra khỏi danh sách cài (không cần thiết)
def _keep_app(pkg):
    if pkg == "ginlemon.flowerfree":
        return USE_SMART_LAUNCHER
    if pkg == "com.og.launcher":
        return USE_UG_LAUNCHER
    return True

if not BOOST_ONLY:
    apps_to_install = [a for a in apps if _keep_app(a[2])]

    missing_apps = [app for app in apps_to_install if not is_installed(app[2])]

    if missing_apps:
        with ThreadPoolExecutor(max_workers=2) as ex:
            [ex.submit(download, app[0], app[1]) for app in missing_apps]

        with ThreadPoolExecutor(max_workers=2) as ex:
            [ex.submit(install, app[1]) for app in missing_apps]
else:
    _cprint(TITLE + "Boost Only — bo qua tai & cai app moi.")

def disable_package(p): run(["pm", "disable-user", "--user", "0", p])
def uninstall_package(p): run(["pm", "uninstall", "--user", "0", p])
def clear_package(p): run(["pm", "clear", "--user", "0", p])

def par_run(func, args_list, max_workers=99):
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        [f.result() for f in as_completed([ex.submit(func, *args) for args in args_list])]

#  An toàn: không bao giờ xoá launcher đang active, tránh crash SystemUI / treo "đang khởi động..."
current_launcher_str = get_current_launcher()
safe_launcher_candidates = [
    pkg for pkg in LAUNCHER_CANDIDATES
    if is_installed(pkg) and pkg not in current_launcher_str
]
if safe_launcher_candidates:
    _cprint(Fore.YELLOW + f"Se xoa launcher khong dung: {safe_launcher_candidates}")
skipped_launchers = [pkg for pkg in LAUNCHER_CANDIDATES if pkg not in safe_launcher_candidates and is_installed(pkg)]
if skipped_launchers:
    _cprint(Fore.YELLOW + f"Giu lai (dang active): {skipped_launchers}")

packages_to_remove = lite_packages + safe_launcher_candidates

if os.path.exists(DEST_DIR):
    for f in os.listdir(DEST_DIR):
        if not f.endswith(".apk"):
            f_path = os.path.join(DEST_DIR, f)
            if os.path.isdir(f_path): shutil.rmtree(f_path)
            else: os.remove(f_path)

_cprint(TITLE + "Cleaning Device...")
ram_before_total, ram_before_avail = get_ram_info()
par_run(clear_package, [(p,) for p in packages_to_remove], 16)
par_run(disable_package, [(p,) for p in packages_to_remove], 16)
par_run(uninstall_package, [(p,) for p in packages_to_remove], 16)
_cprint(SUCCESS + "Device Cleaned Successfully! ")

run_performance_boost_once()
print_device_info()

_cprint(SUCCESS + "\nAll Tasks Completed!-Tool Made By Mangcutyeuiem And NXMC Dev")
print_ram_status()

# Thống kê RAM đã giải phóng sau khi xoá app thừa
ram_after_total, ram_after_avail = get_ram_info()
if ram_before_avail is not None and ram_after_avail is not None:
    gained = ram_after_avail - ram_before_avail
    _cprint(TITLE + "\nRAM Optimization Result")
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)
    _cprint(Fore.WHITE + f"Truoc cleanup: {ram_before_avail} MB kha dung")
    _cprint(Fore.WHITE + f"Sau cleanup:   {ram_after_avail} MB kha dung")
    color = Fore.GREEN if gained >= 0 else Fore.RED
    sign = "+" if gained >= 0 else ""
    _cprint(color + Style.BRIGHT + f"Da giai phong: {sign}{gained} MB")
    _cprint(Fore.YELLOW + Style.BRIGHT + "-" * 30)

print_device_freshness()

if os.path.exists(DEST_DIR): shutil.rmtree(DEST_DIR)
