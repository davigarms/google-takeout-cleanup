import os
import json
import select
import subprocess
import re
import time
import argparse
from datetime import datetime
from tqdm import tqdm

# ------------------- args -------------------

parser = argparse.ArgumentParser(description="Fix EXIF dates on Google Takeout library")
parser.add_argument("path",           help="Root folder (library/)")
parser.add_argument("--dry-run",      action="store_true")
parser.add_argument("--verbose",      action="store_true")
parser.add_argument("--batch-size",   type=int, default=100)
parser.add_argument("--skip-dirs",    nargs="*", default=["@eaDir", ".git", ".Trash", "@tmp"])
parser.add_argument("--exif-timeout", type=int, default=10, help="Per-file exiftool query timeout (s)")
args = parser.parse_args()

ROOT         = args.path
DRY_RUN      = args.dry_run
VERBOSE      = args.verbose
BATCH_SIZE   = args.batch_size
SKIP_DIRS    = set(args.skip_dirs)
EXIF_TIMEOUT = args.exif_timeout

START_TIME = time.time()

# ------------------- constants -------------------

PHOTO_EXT = {".jpg", ".jpeg", ".heic", ".png"}
VIDEO_EXT = {".mov", ".mp4"}

LOCAL_TZ = (lambda z: z[:3] + ":" + z[3:])(datetime.now().astimezone().strftime("%z"))
NOW      = datetime.now()

KNOWN_DEFAULTS = {
    datetime(1970, 1, 1, 0, 0, 0),  # Unix epoch
    datetime(1904, 1, 1, 0, 0, 0),  # QuickTime epoch
    datetime(1980, 1, 1, 0, 0, 0),  # FAT32 / camera no battery
    datetime(2001, 1, 1, 0, 0, 0),  # Core Data epoch (old iOS)
}

# ------------------- logging -------------------

LOG_FILE   = "fix_dates.log"
ERROR_FILE = "fix_dates_errors.log"

with open(LOG_FILE, "a") as f:
    f.write(f"\n──── RUN {datetime.now()} ────\n")

def log(msg, level="INFO", console=False):
    if DRY_RUN:
        msg = f"[DRY] {msg}"
    line = f"[{level}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if level == "ERROR":
        with open(ERROR_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    if VERBOSE or console or level == "ERROR":
        tqdm.write(line)

# ------------------- stats -------------------

stats = {
    "filename_dt":   0,  # full datetime in filename
    "filename_date": 0,  # date only in filename
    "exif_ok":       0,  # exif already valid, skipped
    "folder":        0,  # date from folder structure
    "epoch":         0,  # last resort
    "errors":        0,
}

# ------------------- file type -------------------

def is_photo(file):
    return os.path.splitext(file)[1].lower() in PHOTO_EXT

def is_video(file):
    return os.path.splitext(file)[1].lower() in VIDEO_EXT

def is_media(file):
    ext = os.path.splitext(file)[1].lower()
    return ext in PHOTO_EXT or ext in VIDEO_EXT

# ------------------- exiftool persistent process -------------------

EXIF_PROCESS = subprocess.Popen(
    ["exiftool", "-stay_open", "True", "-@", "-"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
    bufsize=1,
)

def exiftool_query(exif_args):
    cmd = "\n".join(exif_args + ["-execute\n"])
    EXIF_PROCESS.stdin.write(cmd)
    EXIF_PROCESS.stdin.flush()

    output   = ""
    deadline = time.time() + EXIF_TIMEOUT

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            log(f"exiftool timeout: {exif_args[-1]}", "ERROR")
            return ""
        ready, _, _ = select.select([EXIF_PROCESS.stdout], [], [], remaining)
        if not ready:
            log(f"exiftool timeout: {exif_args[-1]}", "ERROR")
            return ""
        line = EXIF_PROCESS.stdout.readline()
        if "{ready}" in line:
            break
        output += line

    return output

def stop_exiftool():
    try:
        EXIF_PROCESS.stdin.write("-stay_open\nFalse\n")
        EXIF_PROCESS.stdin.flush()
        EXIF_PROCESS.wait(timeout=5)
    except Exception:
        EXIF_PROCESS.kill()

# ------------------- EXIF read -------------------

EXIF_CACHE = {}

def get_exif(file):
    if file in EXIF_CACHE:
        return EXIF_CACHE[file]

    fields = (
        ["-DateTimeOriginal", "-OffsetTimeOriginal"]
        if is_photo(file)
        else ["-CreateDate", "-MediaCreateDate"]
    )

    try:
        output = exiftool_query(["-j", "-fast"] + fields + [file])
        data   = json.loads(output)[0] if output.strip() else {}
    except Exception:
        data = {}

    EXIF_CACHE[file] = data
    return data

def parse_exif_dt(val):
    try:
        return datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None

def dt_is_plausible(dt):
    if dt is None:                   return False
    if dt in KNOWN_DEFAULTS:         return False
    if dt.year < 1990:               return False
    if dt > NOW:                     return False
    if dt.month == 0 or dt.day == 0: return False
    return True

def exif_is_valid(file):
    """Returns (is_valid: bool, dt: datetime | None)"""
    exif = get_exif(file)
    if not exif:
        return False, None

    if is_video(file):
        for key in ["CreateDate", "MediaCreateDate"]:
            dt = parse_exif_dt(exif.get(key, ""))
            if dt_is_plausible(dt):
                return True, dt
    else:
        dt = parse_exif_dt(exif.get("DateTimeOriginal", ""))
        if dt_is_plausible(dt):
            return True, dt

    return False, None

# ------------------- date sources -------------------

_FULL_DT_RE = re.compile(
    r"(20\d{2})[:\-_]?(\d{2})[:\-_]?(\d{2})[_\-T ](\d{2})[:\-_]?(\d{2})[:\-_]?(\d{2})"
)
_DATE_RE = re.compile(r"(20\d{2})[:\-_]?(\d{2})[:\-_]?(\d{2})")

def parse_date_from_name(filepath):
    """
    Filename is source of truth when it contains a date.
    - Full datetime → use directly
    - Date only     → use date + mtime for time component
    Returns (datetime, method) or (None, None).
    """
    name = os.path.splitext(os.path.basename(filepath))[0]

    m = _FULL_DT_RE.search(name)
    if m:
        y, mo, d, h, mi, s = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)), "filename_dt"
        except ValueError:
            pass

    m = _DATE_RE.search(name)
    if m:
        y, mo, d = m.groups()
        try:
            mt = datetime.fromtimestamp(os.path.getmtime(filepath))
            return datetime(int(y), int(mo), int(d), mt.hour, mt.minute, mt.second), "filename_date"
        except ValueError:
            pass

    return None, None


def parse_date_from_folder(path):
    """
    Folder structure YYYY/MM is reliable — organize.py already placed
    files here based on the Google Takeout JSON timestamp.
    Returns datetime or None.
    """
    parts = path.replace("\\", "/").split("/")
    for i in range(len(parts) - 1):
        if re.fullmatch(r"20\d{2}", parts[i]) and re.fullmatch(r"\d{2}", parts[i + 1]):
            try:
                year  = int(parts[i])
                month = int(parts[i + 1])
                mt    = datetime.fromtimestamp(os.path.getmtime(path))
                return datetime(year, month, mt.day, mt.hour, mt.minute, mt.second)
            except (ValueError, OSError):
                pass
    return None


def epoch_datetime(file):
    """Last resort: preserve mtime time-of-day, year set to 1970 as sentinel."""
    mt = datetime.fromtimestamp(os.path.getmtime(file))
    return datetime(1970, 1, 1, mt.hour, mt.minute, mt.second)

# ------------------- EXIF write -------------------

BATCH = []  # list of (file, dt, is_vid)

def add_to_batch(file, dt, is_vid):
    EXIF_CACHE.pop(file, None)
    BATCH.append((file, dt, is_vid))
    if len(BATCH) >= BATCH_SIZE:
        flush_batch()

def build_write_args(dt, is_vid):
    dt_str = dt.strftime("%Y:%m:%d %H:%M:%S")
    if is_vid:
        return [
            f"-CreateDate={dt_str}",
            f"-MediaCreateDate={dt_str}",
            f"-ModifyDate={dt_str}",
            f"-QuickTime:CreateDate={dt_str}",
            f"-QuickTime:ModifyDate={dt_str}",
        ]
    else:
        return [
            f"-DateTimeOriginal={dt_str}",
            f"-CreateDate={dt_str}",
            f"-ModifyDate={dt_str}",
            f"-OffsetTimeOriginal={LOCAL_TZ}",
            f"-OffsetTime={LOCAL_TZ}",
            f"-OffsetTimeDigitized={LOCAL_TZ}",
        ]

def flush_batch():
    if not BATCH:
        return

    base = ["exiftool", "-overwrite_original", "-P", "-fast"]

    for file, dt, is_vid in BATCH:
        cmd = base + build_write_args(dt, is_vid) + [file]
        log(f"[WRITE] {dt} → {file}")
        if not DRY_RUN:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if result.returncode != 0:
                log(f"exiftool write failed: {result.stderr.decode().strip()} [{file}]", "ERROR")

    log(f"[BATCH] flushed {len(BATCH)} files")
    BATCH.clear()

# ------------------- walk -------------------

def walk_media(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            fp = os.path.join(dirpath, name)
            if is_media(fp):
                yield fp

# ------------------- main -------------------

log(f"START | path={ROOT} | dry={DRY_RUN} | tz={LOCAL_TZ}", console=True)

files = list(walk_media(ROOT))
log(f"TOTAL MEDIA FILES: {len(files)}", console=True)

with tqdm(total=len(files), desc="Processing", unit="file") as pbar:
    for file in files:
        is_vid = is_video(file)

        try:
            # ---- 1. FILENAME ----
            dt, method = parse_date_from_name(file)
            if dt:
                add_to_batch(file, dt, is_vid)
                stats[method] += 1
                log(f"[{method.upper()}] {file}")
                pbar.update(1)
                continue

            # ---- 2. EXIF ----
            valid, _ = exif_is_valid(file)
            if valid:
                stats["exif_ok"] += 1
                log(f"[EXIF OK] {file}")
                pbar.update(1)
                continue

            # ---- 3. FOLDER ----
            dt = parse_date_from_folder(file)
            if dt:
                add_to_batch(file, dt, is_vid)
                stats["folder"] += 1
                log(f"[FOLDER] {file}")
                pbar.update(1)
                continue

            # ---- 4. EPOCH ----
            dt = epoch_datetime(file)
            add_to_batch(file, dt, is_vid)
            stats["epoch"] += 1
            log(f"[EPOCH] {file}")

        except Exception as e:
            stats["errors"] += 1
            log(f"{file} → {e}", "ERROR")

        pbar.update(1)

flush_batch()
stop_exiftool()

# ------------------- summary -------------------

elapsed = time.time() - START_TIME
h, rem  = divmod(int(elapsed), 3600)
m, s    = divmod(rem, 60)
total   = len(files)
fixed   = total - stats["exif_ok"] - stats["errors"]

print(f"\n── Summary ──────────────────────────────────")
print(f"  Total files      : {total}")
print(f"  Fixed            : {fixed}")
print(f"    Filename (dt)  : {stats['filename_dt']}")
print(f"    Filename (date): {stats['filename_date']}")
print(f"    Folder         : {stats['folder']}")
print(f"    Epoch          : {stats['epoch']}")
print(f"  Skipped (ok)     : {stats['exif_ok']}")
print(f"  Errors           : {stats['errors']}")
print(f"  Elapsed          : {h:02d}:{m:02d}:{s:02d}")
print(f"  Dry run          : {DRY_RUN}")
print(f"─────────────────────────────────────────────")