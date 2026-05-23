import os
import json
import shutil
import hashlib
import csv
import unicodedata
import re
from datetime import datetime
import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
from threading import Lock

# ------------------- arguments -------------------

parser = argparse.ArgumentParser()
parser.add_argument("source", help="Google Photos Takeout folder")
parser.add_argument("--output", default=".", help="Output directory")
parser.add_argument("-move", action="store_true")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--workers", type=int, default=4)
parser.add_argument("--verbose", action="store_true")

args = parser.parse_args()

SOURCE_DIR = args.source
OUTPUT_DIR = args.output
MOVE_MODE = args.move
DRY_RUN = args.dry_run
WORKERS = args.workers
VERBOSE = args.verbose

start_time = time.time()

# ------------------- directories -------------------

LIB_DIR = os.path.join(OUTPUT_DIR, "library")
ALBUM_DIR = os.path.join(OUTPUT_DIR, "albums")
PARTNER_DIR = os.path.join(OUTPUT_DIR, "partner_photos")
UNKNOWN_DIR = os.path.join(LIB_DIR, "unknown")
LOG_FILE = os.path.join(OUTPUT_DIR, "photo_log.csv")

for d in [OUTPUT_DIR, LIB_DIR, ALBUM_DIR, PARTNER_DIR, UNKNOWN_DIR]:
    os.makedirs(d, exist_ok=True)

seen_hashes = {}

# ------------------- metrics -------------------

size_totals = {"library": 0, "partner_photos": 0, "albums": 0, "total": 0}

# ------------------- logging -------------------

def log(msg):
    if VERBOSE:
        tqdm.write(msg)

# ------------------- helpers -------------------

def fast_hash(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(1024 * 1024))
    return h.hexdigest()

def normalize(s):
    return re.sub(r'[^a-zA-Z0-9]', '', s).lower()

def year_month(data):
    try:
        ts = int(data["photoTakenTime"]["timestamp"])
        dt = datetime.utcfromtimestamp(ts)
        return str(dt.year), f"{dt.month:02d}"
    except:
        return "unknown", "00"

def is_album_folder(name):
    return not (name.startswith("Photos from") or name.startswith("Archive"))

def detect_partner(data, root):
    origin = data.get("googlePhotosOrigin", {})
    if isinstance(origin, dict) and "fromPartnerSharing" in origin:
        return True
    if "sharedAlbumMetadata" in data:
        return True
    if "shared" in root.lower():
        return True
    return False

def find_photo_file(root, title, file_index):
    title = unicodedata.normalize("NFC", title)
    expected = os.path.join(root, title)
    if expected in file_index:
        return expected
    base_norm = normalize(os.path.splitext(title)[0])
    for f in file_index:
        if normalize(os.path.splitext(os.path.basename(f))[0]).startswith(base_norm[:8]):
            return f
    return None

def safe_link(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        new = f"{base}_{i}{ext}"
        if not os.path.exists(new):
            return new
        i += 1

# ------------------- progress tracking -------------------

def count_files(source):
    total = 0
    for _, _, files in os.walk(source):
        total += len(files)
    return total

def count_total_size(source):
    total = 0
    for root, _, files in os.walk(source):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except:
                pass
    return total

TOTAL_FILES = count_files(SOURCE_DIR)
TOTAL_BYTES = count_total_size(SOURCE_DIR)

pbar_files = tqdm(total=TOTAL_FILES, desc="Files", unit="file")
pbar_bytes = tqdm(total=TOTAL_BYTES, desc="Copy", unit="B", unit_scale=True, unit_divisor=1024)

pbar_lock = Lock()

# ------------------- copy logic -------------------

def copy_with_progress(src, dst):
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                buf = fsrc.read(1024 * 1024)
                if not buf:
                    break
                fdst.write(buf)

                with pbar_lock:
                    pbar_bytes.update(len(buf))

        shutil.copystat(src, dst)
        return dst

    except Exception as e:
        log(f"Error copying {src}: {e}")
        return None

def copy_or_move(src, dst):
    if DRY_RUN:
        try:
            size = os.path.getsize(src)
            with pbar_lock:
                pbar_bytes.update(size)
        except:
            pass
        return dst

    try:
        if MOVE_MODE:
            shutil.move(src, dst)
            size = os.path.getsize(dst)
            with pbar_lock:
                pbar_bytes.update(size)
            return dst
        else:
            return copy_with_progress(src, dst)

    except Exception as e:
        log(f"Error: {src} {e}")
        return None

# ------------------- executor -------------------

executor = ThreadPoolExecutor(max_workers=WORKERS)
futures = []

# ------------------- load CSV -------------------

if os.path.exists(LOG_FILE):
    with open(LOG_FILE, newline="") as f:
        for row in csv.DictReader(f):
            if row["hash"]:
                seen_hashes[row["hash"]] = row["dest"]

# ------------------- main -------------------

write_header = not os.path.exists(LOG_FILE)

with open(LOG_FILE, "a", newline="") as log_file:
    writer = csv.writer(log_file)
    if write_header:
        writer.writerow(["filename","hash","dest","album","source","partner","live_pair","json"])

    for root, _, files in os.walk(SOURCE_DIR):
        album = os.path.basename(root)
        file_index = {os.path.join(root, f) for f in files}
        processed = set()

        # -------- JSON --------
        for file in [f for f in files if f.endswith(".json")]:
            json_path = os.path.join(root, file)

            try:
                data = json.load(open(json_path))
            except:
                pbar_files.update(1)
                continue

            title = data.get("title")
            if not isinstance(title, str):
                pbar_files.update(1)
                continue

            photo_path = find_photo_file(root, title, file_index)
            if not photo_path:
                pbar_files.update(1)
                continue

            processed.add(photo_path)
            h = fast_hash(photo_path)

            if h not in seen_hashes:
                y, m = year_month(data)

                if detect_partner(data, root):
                    dest_dir = os.path.join(PARTNER_DIR, y, m)
                else:
                    dest_dir = os.path.join(LIB_DIR, y, m)

                dest = os.path.join(dest_dir, os.path.basename(photo_path))

                future = executor.submit(copy_or_move, photo_path, dest)
                futures.append((future, photo_path))
                dest_photo = dest

                seen_hashes[h] = dest_photo

                # -------- COPY JSON --------
                json_dest = dest_photo + ".json"
                future = executor.submit(copy_or_move, json_path, json_dest)
                futures.append((future, json_path))

            else:
                dest_photo = seen_hashes[h]

            # -------- ALBUM --------
            if is_album_folder(album):
                album_path = os.path.join(ALBUM_DIR, album)
                os.makedirs(album_path, exist_ok=True)

                link = safe_link(os.path.join(album_path, os.path.basename(dest_photo)))

                if not os.path.exists(link):
                    try:
                        rel = os.path.relpath(dest_photo, album_path)
                        if not DRY_RUN:
                            os.symlink(rel, link)
                    except:
                        pass

            writer.writerow([os.path.basename(photo_path), h, dest_photo, album, root, False, "", True])
            log(f"Processed (JSON): {os.path.basename(photo_path)}")

            pbar_files.update(1)

        # -------- no JSON --------
        for photo in [f for f in files if f.lower().endswith((".jpg",".jpeg",".png",".heic",".mov",".mp4"))]:
            photo_path = os.path.join(root, photo)

            if photo_path in processed:
                pbar_files.update(1)
                continue

            h = fast_hash(photo_path)

            if h not in seen_hashes:
                ts = os.path.getmtime(photo_path)
                dt = datetime.utcfromtimestamp(ts)

                dest_dir = os.path.join(LIB_DIR, str(dt.year), f"{dt.month:02d}")
                dest = os.path.join(dest_dir, photo)

                future = executor.submit(copy_or_move, photo_path, dest)
                futures.append((future, photo_path))
                dest_photo = dest

                seen_hashes[h] = dest_photo
            else:
                dest_photo = seen_hashes[h]

            # -------- ALBUM --------
            if is_album_folder(album):
                album_path = os.path.join(ALBUM_DIR, album)
                os.makedirs(album_path, exist_ok=True)

                link = safe_link(os.path.join(album_path, os.path.basename(dest_photo)))

                if not os.path.exists(link):
                    try:
                        rel = os.path.relpath(dest_photo, album_path)
                        if not DRY_RUN:
                            os.symlink(rel, link)
                    except:
                        pass

            writer.writerow([photo, h, dest_photo, album, root, False, "", False])
            log(f"Processed (no JSON): {photo}")

            pbar_files.update(1)

# ------------------- wait -------------------

for future, src in futures:
    if future.result() is None:
        log(f"Failed: {src}")

executor.shutdown()

pbar_files.close()
pbar_bytes.close()

# ------------------- summary -------------------

elapsed = time.time() - start_time
throughput = (TOTAL_BYTES/1024/1024)/elapsed if elapsed > 0 else 0

print(f"\nElapsed: {int(elapsed//3600):02d}:{int((elapsed%3600)//60):02d}:{int(elapsed%60):02d}")
print(f"Speed: {throughput:.2f} MB/s")
print("Finished.")
print("Workers:", WORKERS)
print("Mode:", "MOVE" if MOVE_MODE else "COPY")
print("Dry-run:", DRY_RUN)