import os
import hashlib
import argparse
import shutil
from tqdm import tqdm
from datetime import datetime

# -----------------------------
# CONFIG
# -----------------------------

IGNORED_EXTENSIONS = {".json", ".ds_store"}
OUTPUT_DIR = "output"
DUPLICATES_DIR = os.path.join(OUTPUT_DIR, "duplicates")
LOG_FILE = "media_dedup.log"
CHUNK_SIZE = 1024 * 1024


# -----------------------------
# LOGGING
# -----------------------------

def log(message):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(message + "\n")

def log_line(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    log(line)


# -----------------------------
# FILES
# -----------------------------

def is_ignored(file_name):
    _, ext = os.path.splitext(file_name.lower())
    return ext in IGNORED_EXTENSIONS

def list_files(root_path):
    files = []
    for root, _, filenames in os.walk(root_path):
        for name in filenames:
            if not is_ignored(name):
                files.append(os.path.join(root, name))
    return files


# -----------------------------
# HASH
# -----------------------------

def compute_hash(file_path):
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


# -----------------------------
# INDEX
# -----------------------------

def build_index(base_path):
    log_line("[INFO] Counting files in BASE...")

    all_files = list_files(base_path)
    log_line(f"[INFO] Total files to index: {len(all_files)}")

    path_index = {}
    hash_index = set()

    log_line("[INFO] Indexing BASE...")

    for full_path in tqdm(all_files, desc="Indexing", unit="file"):
        try:
            rel_path = os.path.relpath(full_path, base_path)
            file_hash = compute_hash(full_path)

            path_index[rel_path] = full_path
            hash_index.add(file_hash)

        except Exception as e:
            log_line(f"[ERROR] {full_path}: {e}")

    log_line(f"[INFO] Indexed files: {len(path_index)}")

    return path_index, hash_index


# -----------------------------
# COPY WITH SIDECARS
# -----------------------------

def copy_with_sidecars(src, root_path, dest_root, dry_run, log_prefix):
    base_dir = os.path.dirname(src)
    base_name = os.path.basename(src)

    for name in os.listdir(base_dir):
        if name == base_name or name.startswith(base_name + "."):
            full_src = os.path.join(base_dir, name)
            rel = os.path.relpath(full_src, root_path)
            dest = os.path.join(dest_root, rel)

            log_line(f"[{log_prefix}] {full_src} -> {dest}")

            if not dry_run:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(full_src, dest)


def copy_file(src, incoming_root, dry_run):
    copy_with_sidecars(
        src,
        incoming_root,
        OUTPUT_DIR,
        dry_run,
        "COPY"
    )


def copy_duplicate(src, incoming_root, dry_run, reason):
    log_line(f"[DUPLICATE] reason={reason} src={src}")

    copy_with_sidecars(
        src,
        incoming_root,
        DUPLICATES_DIR,
        dry_run,
        "DUPLICATE COPY"
    )


# -----------------------------
# PROCESS
# -----------------------------

def process(base_path, incoming_path, dry_run=False, verbose=False, hash_mode=False, copy_duplicates=False):
    path_index, hash_index = build_index(base_path)

    log_line("[INFO] Processing INCOMING...\n")

    stats = {
        "copied": 0,
        "skipped_path": 0,
        "skipped_hash": 0,
        "duplicates_copied": 0,
        "errors": 0
    }

    incoming_files = list_files(incoming_path)
    iterator = tqdm(incoming_files, desc="Processing", unit="file") if verbose else incoming_files

    for incoming_file in iterator:
        rel_path = os.path.relpath(incoming_file, incoming_path)

        try:
            # -------------------------
            # PATH CHECK
            # -------------------------
            if rel_path in path_index:

                if not hash_mode:
                    stats["skipped_path"] += 1
                    log_line(f"[CHECK] {rel_path} -> SKIP (existing path)")

                    if copy_duplicates:
                        copy_duplicate(incoming_file, incoming_path, dry_run, "path-match")
                        stats["duplicates_copied"] += 1

                    continue

                # HASH MODE
                incoming_hash = compute_hash(incoming_file)
                existing_hash = compute_hash(path_index[rel_path])

                if incoming_hash == existing_hash:
                    stats["skipped_hash"] += 1
                    log_line(f"[CHECK] {rel_path} -> SKIP (duplicate hash, same path)")

                    if copy_duplicates:
                        copy_duplicate(incoming_file, incoming_path, dry_run, "hash-match-same-path")
                        stats["duplicates_copied"] += 1

                    continue

                log_line(f"[CHECK] {rel_path} -> COPY (updated file in hash-mode)")

            else:
                # -------------------------
                # HASH CHECK (global)
                # -------------------------
                incoming_hash = compute_hash(incoming_file)

                if incoming_hash in hash_index:
                    stats["skipped_hash"] += 1
                    log_line(f"[CHECK] {rel_path} -> SKIP (duplicate hash, global match)")

                    if copy_duplicates:
                        copy_duplicate(incoming_file, incoming_path, dry_run, "hash-global-match")
                        stats["duplicates_copied"] += 1

                    continue

                log_line(f"[CHECK] {rel_path} -> COPY (new file)")

            # -------------------------
            # COPY (WITH SIDECARS)
            # -------------------------
            copy_file(incoming_file, incoming_path, dry_run)
            stats["copied"] += 1

        except Exception as e:
            log_line(f"[ERROR] {incoming_file}: {e}")
            stats["errors"] += 1

    print_summary(stats)


# -----------------------------
# SUMMARY
# -----------------------------

def print_summary(stats):
    summary = [
        "\n========== SUMMARY ==========",
        f"Copied:              {stats['copied']}",
        f"Skipped (path):      {stats['skipped_path']}",
        f"Skipped (hash):      {stats['skipped_hash']}",
        f"Duplicates copied:   {stats['duplicates_copied']}",
        f"Errors:              {stats['errors']}",
        "=============================\n"
    ]

    for line in summary:
        print(line)
        log(line)


# -----------------------------
# MAIN
# -----------------------------

def main():
    parser = argparse.ArgumentParser(description="Media dedup")

    parser.add_argument("path", help="BASE directory")
    parser.add_argument("--incoming", required=True)

    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--hash-mode", action="store_true")
    parser.add_argument("--copy-duplicates", action="store_true")

    args = parser.parse_args()

    process(
        base_path=args.path,
        incoming_path=args.incoming,
        dry_run=args.dry_run,
        verbose=args.verbose,
        hash_mode=args.hash_mode,
        copy_duplicates=args.copy_duplicates
    )


if __name__ == "__main__":
    main()