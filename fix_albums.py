import csv
from pathlib import Path
import argparse
from tqdm import tqdm
import sys

# ------------------- argumentos -------------------

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

# ------------------- validações -------------------

if not CSV_FILE.exists():
    print(f"ERROR: CSV not found: {CSV_FILE}")
    sys.exit(1)

if not LIBRARY_ROOT.exists():
    print(f"ERROR: library not found: {LIBRARY_ROOT}")
    sys.exit(1)

# ------------------- indexação -------------------

print("Indexing files...")

filename_index = {}
hash_index = {}

def index_file(path):
    name = path.name
    filename_index.setdefault(name, []).append(path)

    # opcional: usar hash se existir no CSV
    # aqui assumimos que hash NÃO está no arquivo, então só será usado do CSV

# scan library + partner_photos
all_files = list(LIBRARY_ROOT.rglob("*")) + list(PARTNER_ROOT.rglob("*"))

for f in tqdm(all_files, desc="Scanning"):
    if f.is_file():
        index_file(f)

print(f"Indexed {len(filename_index)} unique filenames")

# ------------------- processamento -------------------

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

        # --- caso 1: match único ---
        if len(matches) == 1:
            resolved_path = matches[0]

        # --- caso 2: múltiplos → tentar hash ---
        elif len(matches) > 1 and file_hash:
            # tentativa simples: escolher primeiro (como você pediu)
            resolved_path = matches[0]

        # --- resultado ---
        if resolved_path:
            # reconstruir path relativo (library ou partner_photos)
            try:
                relative = resolved_path.relative_to(SOURCE_ROOT)
            except ValueError:
                relative = resolved_path

            row["dest"] = str(relative)
            fixed_rows.append(row)
        else:
            missing_rows.append(row)

# ------------------- salvar -------------------

def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

write_csv(FIXED_CSV, fixed_rows)
write_csv(MISSING_CSV, missing_rows)

# ------------------- resumo -------------------

print("\nDone.")
print(f"Fixed: {len(fixed_rows)}")
print(f"Still missing: {len(missing_rows)}")
print(f"Fixed CSV: {FIXED_CSV}")
print(f"Missing CSV: {MISSING_CSV}")