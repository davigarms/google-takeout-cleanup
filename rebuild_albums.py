import os
import csv
import shutil
from pathlib import Path
import argparse
import sys
from tqdm import tqdm

# ------------------- argumentos -------------------

parser = argparse.ArgumentParser(description="Rebuild albums from photo_log.csv")
parser.add_argument("csv", nargs="?", default="photo_log.csv", help="CSV file (default: photo_log.csv)")
parser.add_argument("--output", default="albums", help="Output folder (default: albums)")
parser.add_argument("--library-root", default="library", help="Library root (default: ./library)")
parser.add_argument("--partner-root", default="partner_photos", help="Partner root (default: ./partner_photos)")
parser.add_argument("--source", help="Source root override (base path for resolving files)")
parser.add_argument("--method", choices=["copy", "hardlink", "symlink"], default="symlink", help="Transfer method")
args = parser.parse_args()

OUTPUT_DIR = Path(args.output)

# source override (se fornecido)
if args.source:
    SOURCE_ROOT = Path(args.source).resolve()
    CSV_FILE = (SOURCE_ROOT / args.csv).resolve()
    OUTPUT_DIR = (SOURCE_ROOT / args.output).resolve()
    LIBRARY_ROOT = (SOURCE_ROOT / args.library_root).resolve()
    PARTNER_ROOT = (SOURCE_ROOT / args.partner_root).resolve()
    MISSING_CSV = SOURCE_ROOT / "missing_photos.csv"
else:
    CSV_FILE = Path(args.csv).resolve()
    OUTPUT_DIR = Path(args.output).resolve()
    LIBRARY_ROOT = Path(args.library_root).resolve()
    PARTNER_ROOT = Path(args.partner_root).resolve()
    MISSING_CSV =  "./missing_photos.csv"


METHOD = args.method

# ------------------- validações -------------------

if not CSV_FILE.exists():
    print(f"ERROR: CSV file not found: {CSV_FILE}")
    sys.exit(1)

if not LIBRARY_ROOT.exists():
    print(f"ERROR: Library root not found: {LIBRARY_ROOT}")
    sys.exit(1)

if not LIBRARY_ROOT.is_dir():
    print(f"ERROR: Library root is not a directory: {LIBRARY_ROOT}")
    sys.exit(1)

# ------------------- funções -------------------

def safe_filename(path):
    if not path.exists():
        return path
    base = path.stem
    ext = path.suffix
    i = 1
    while True:
        new = path.with_name(f"{base}_{i}{ext}")
        if not new.exists():
            return new
        i += 1

def transfer(src, dst):
    dst = safe_filename(dst)
    try:
        if METHOD == "copy":
            shutil.copy2(src, dst)
        elif METHOD == "hardlink":
            os.link(src, dst)
        elif METHOD == "symlink":
            os.symlink(src, dst)
    except Exception as e:
        print(f"ERROR copying {src} -> {dst}: {e}")

def map_to_real_path(original_path):
    p = Path(original_path)
    parts = p.parts

    if "library" in parts:
        idx = parts.index(args.library_root)
        relative = Path(*parts[idx+1:])
        return LIBRARY_ROOT / relative

    if "partner_photos" in parts:
        idx = parts.index(args.partner_root)
        relative = Path(*parts[idx+1:])
        return PARTNER_ROOT / relative

    # fallback (último recurso)
    return LIBRARY_ROOT / p.name

# ------------------- execução -------------------

print("Rebuilding albums...")
print("CSV:", CSV_FILE)
print("Library root:", LIBRARY_ROOT)
print("Partner root:", PARTNER_ROOT)
print("Output:", OUTPUT_DIR)
print("Method:", METHOD)
if args.source:
    print("Source:", args.source)

# pré-contagem para tqdm
with open(CSV_FILE, newline="") as f:
    total_rows = sum(1 for _ in f) - 1  # desconta header

processed = 0
missing = 0
missing_rows = []
fieldnames = None  

with open(CSV_FILE, newline="") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames 

    required_fields = {"album", "dest"}
    if not required_fields.issubset(fieldnames):
        print(f"ERROR: CSV missing required columns: {required_fields}")
        sys.exit(1)

    for row in tqdm(reader, total=total_rows, desc="Processing"):
        album = row["album"]
        src_original = row["dest"]

        if not album:
            continue

        src_path = map_to_real_path(src_original)

        if not src_path.exists():
            missing += 1
            missing_rows.append(row)
            continue

        album_dir = OUTPUT_DIR / album
        album_dir.mkdir(parents=True, exist_ok=True)

        dst = album_dir / src_path.name

        if dst.exists():
            continue

        transfer(src_path, dst)
        processed += 1

# ------------------- salvar missing -------------------

print("DEBUG missing count:", missing)
print("DEBUG missing_rows len:", len(missing_rows))
print("DEBUG missing_csv path:", MISSING_CSV)


if missing_rows:
    with open(MISSING_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(missing_rows)

    print(f"Missing CSV written to: {MISSING_CSV}")

# ------------------- resumo -------------------

print("\nDone.")
print(f"Files processed: {processed}")
print(f"Missing files: {missing}")
print("Output:", OUTPUT_DIR)