import csv
import hashlib
from pathlib import Path
import argparse
from tqdm import tqdm
import sys

# ------------------- args -------------------

parser = argparse.ArgumentParser(description="Fix missing photos by filename/hash")
parser.add_argument("csv", nargs="?", default="missing_photos.csv", help="Missing CSV file")
parser.add_argument("--source", default=".", help="Source root (default: current dir)")
args = parser.parse_args()

SOURCE_ROOT = Path(args.source).resolve()
CSV_FILE = (SOURCE_ROOT / args.csv).resolve()

LIBRARY_ROOT = SOURCE_ROOT / "library"
PARTNER_ROOT = SOURCE_ROOT / "partner_photos"

FIXED_CSV = SOURCE_ROOT / "fixed_photos.csv"
MISSING_CSV = SOURCE_ROOT / "still_missing.csv"

# ------------------- validation -------------------

if not CSV_FILE.exists():
    print(f"ERROR: CSV not found: {CSV_FILE}")
    sys.exit(1)

if not LIBRARY_ROOT.exists():
    print(f"ERROR: library not found: {LIBRARY_ROOT}")
    sys.exit(1)

# ------------------- indexing -------------------

print("Indexing files...")

filename_index = {}

def fast_hash(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(1024 * 1024))
    return h.hexdigest()

def index_file(path):
    name = path.name
    filename_index.setdefault(name, []).append(path)

# scan library + partner_photos
all_files = list(LIBRARY_ROOT.rglob("*")) + list(PARTNER_ROOT.rglob("*"))

for f in tqdm(all_files, desc="Scanning"):
    if f.is_file():
        index_file(f)

print(f"Indexed {len(filename_index)} unique filenames")

# ------------------- processing -------------------

fixed_rows = []
missing_rows = []

with open(CSV_FILE, newline="") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames

    required_fields = {"dest"}
    if not required_fields.issubset(fieldnames):
        print("ERROR: CSV must contain 'dest'")
        sys.exit(1)

    for row in tqdm(reader, desc="Resolving"):
        original_path = row["dest"]
        filename = Path(original_path).name
        file_hash = row.get("hash")

        matches = filename_index.get(filename, [])

        resolved_path = None

        # --- case 1: single match ---
        if len(matches) == 1:
            resolved_path = matches[0]

        # --- case 2: multiple matches → try hash ---
        elif len(matches) > 1 and file_hash:
            for candidate in matches:
                try:
                    if fast_hash(candidate) == file_hash:
                        resolved_path = candidate
                        break
                except Exception:
                    pass

        # --- result ---
        if resolved_path:
            # rebuild relative path (library or partner_photos)
            try:
                relative = resolved_path.relative_to(SOURCE_ROOT)
            except ValueError:
                relative = resolved_path

            row["dest"] = str(relative)
            fixed_rows.append(row)
        else:
            missing_rows.append(row)

# ------------------- save -------------------

def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

write_csv(FIXED_CSV, fixed_rows)
write_csv(MISSING_CSV, missing_rows)

# ------------------- summary -------------------

print("\nDone.")
print(f"Fixed: {len(fixed_rows)}")
print(f"Still missing: {len(missing_rows)}")
print(f"Fixed CSV: {FIXED_CSV}")
print(f"Missing CSV: {MISSING_CSV}")