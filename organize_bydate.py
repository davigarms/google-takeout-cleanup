import os
import subprocess
import json
import shutil
import re
from datetime import datetime
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from tqdm import tqdm

# ------------------- args -------------------

parser = argparse.ArgumentParser()
parser.add_argument("source")
parser.add_argument("--output", default="./output")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--verbose", action="store_true")
parser.add_argument("--exif-only", action="store_true")
parser.add_argument("--workers", type=int, default=8,
                    help="Threads para cópia paralela (padrão: 8)")
parser.add_argument("--exif-batch", type=int, default=10,
                    help="Arquivos por batch EXIF (padrão: 50)")
args = parser.parse_args()

SOURCE      = args.source
OUTPUT_DIR  = os.path.abspath(args.output)
UNKNOWN_DIR = os.path.join(OUTPUT_DIR, "unknown")
DRY_RUN     = args.dry_run
VERBOSE     = args.verbose
EXIF_ONLY   = args.exif_only
WORKERS     = args.workers
EXIF_BATCH  = args.exif_batch

MEDIA_EXT   = {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".mkv", ".avi"}
EXIF_FIELDS = ["DateTimeOriginal", "CreateDate"]

DATE_PATTERNS = [
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
]

# ------------------- EXIF DAEMON (batch) -------------------

class ExifDaemon:
    """
    Processo exiftool persistente (-stay_open).

    read_batch(files) envia N arquivos numa única mensagem e lê
    N resultados de volta — um único round-trip de IPC por batch,
    barra atualiza a cada batch concluído.

    Ajuste --exif-batch para equilibrar granularidade vs throughput:
      - menor (10-20)  → barra mais fluida, mais round-trips
      - maior (100-200) → barra em saltos, menos round-trips
      - 50 é um bom ponto de partida
    """

    def __init__(self):
        self._proc = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-@", "-", "-fast2"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def read_batch(self, files: list) -> dict:
        if not files:
            return {}

        # Monta um único bloco de comandos para todos os arquivos do batch
        # seguido de -execute — exiftool processa tudo e responde com JSON + {ready}
        lines = []
        for f in EXIF_FIELDS:
            lines.append(f"-{f}")
        lines.append("-j")
        lines.extend(files)
        lines.append("-execute")
        self._proc.stdin.write("\n".join(lines) + "\n")
        self._proc.stdin.flush()

        buf = []
        while True:
            line = self._proc.stdout.readline()
            if "{ready}" in line:
                break
            buf.append(line)

        try:
            data = json.loads("".join(buf))
        except Exception:
            return {f: None for f in files}

        out = {f: None for f in files}
        for item in data:
            fp = item.get("SourceFile")
            if not fp:
                continue
            for field in EXIF_FIELDS:
                if field in item:
                    try:
                        out[fp] = datetime.strptime(item[field], "%Y:%m:%d %H:%M:%S")
                        break
                    except Exception:
                        pass

        return out

    def close(self):
        try:
            self._proc.stdin.write("-stay_open\nFalse\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

# ------------------- SCAN ÚNICO -------------------

def scan_source(root):
    media_files = []
    sidecar_map = defaultdict(list)
    json_index  = {}

    for r, _, files in os.walk(root):
        for f in files:
            if f.startswith("."):
                continue
            full = os.path.join(r, f)
            ext  = os.path.splitext(f)[1].lower()
            base = f.split(".")[0]

            if ext in MEDIA_EXT:
                media_files.append(full)
            else:
                sidecar_map[base].append(full)
                lf = f.lower()
                if "json" in lf or "metadata" in lf or "supplemental" in lf:
                    json_index[base] = full

    return media_files, sidecar_map, json_index

# ------------------- PARSE DE DATA POR TEXTO -------------------

def parse_date_from_text(file):
    for p in DATE_PATTERNS:
        m = p.search(file)
        if m:
            y, mo, d = m.groups()
            return datetime(int(y), int(mo), int(d))

    parts = file.split(os.sep)
    for i in range(len(parts) - 1):
        if parts[i].isdigit() and len(parts[i]) == 4 and parts[i + 1].isdigit():
            return datetime(int(parts[i]), int(parts[i + 1]), 1)

    return None

# ------------------- DESTINO -------------------

def build_dest(dt):
    if not dt:
        return UNKNOWN_DIR
    return os.path.join(OUTPUT_DIR, str(dt.year), f"{dt.month:02d}")

# ------------------- CÓPIA -------------------

_conflict_counter = defaultdict(int)

def copy_file(src, dest):
    if DRY_RUN:
        return
    os.makedirs(dest, exist_ok=True)
    name   = os.path.basename(src)
    target = os.path.join(dest, name)
    if os.path.exists(target):
        base, ext = os.path.splitext(name)
        _conflict_counter[target] += 1
        target = os.path.join(dest, f"{base}_{_conflict_counter[target]}{ext}")
    shutil.copy2(src, target)

# ------------------- RESOLUÇÃO DE DATA -------------------

def resolve_date(file, exif_data, json_index):
    dt = exif_data.get(file)

    if not dt and not EXIF_ONLY:
        base = os.path.basename(file).split(".")[0]
        jf   = json_index.get(base)
        if jf:
            try:
                with open(jf, encoding="utf-8") as fh:
                    data = json.load(fh)
                if "photoTakenTime" in data:
                    dt = datetime.fromtimestamp(int(data["photoTakenTime"]["timestamp"]))
                elif "creationTime" in data:
                    dt = datetime.fromtimestamp(int(data["creationTime"]["timestamp"]))
            except Exception:
                pass

    if not dt:
        dt = parse_date_from_text(file)

    return dt

# ------------------- TAREFA DE CÓPIA -------------------

def process_file(file, exif_data, json_index, sidecar_map):
    dt   = resolve_date(file, exif_data, json_index)
    dest = build_dest(dt)
    copy_file(file, dest)
    base = os.path.basename(file).split(".")[0]
    for sc in sidecar_map.get(base, []):
        copy_file(sc, dest)
    if VERBOSE:
        print(file, "->", dest)

# ------------------- MAIN -------------------

print("[INFO] Escaneando árvore de arquivos...")
media_files, sidecar_map, json_index = scan_source(SOURCE)
print(f"[INFO] {len(media_files)} arquivo(s) de mídia encontrado(s)")

if not media_files:
    print("[DONE] Nada a processar.")
    raise SystemExit(0)

# Leitura EXIF: daemon persistente + batches → um round-trip por batch
exif_data = {}
daemon = ExifDaemon()
try:
    with tqdm(total=len(media_files), desc="lendo EXIF", smoothing=0) as pbar:
        for i in range(0, len(media_files), EXIF_BATCH):
            batch = media_files[i:i + EXIF_BATCH]
            exif_data.update(daemon.read_batch(batch))
            pbar.update(len(batch))
finally:
    daemon.close()

# Cópia paralela
pbar = tqdm(total=len(media_files), desc="organizando", smoothing=0)

with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futures = {
        pool.submit(process_file, f, exif_data, json_index, sidecar_map): f
        for f in media_files
    }
    for fut in as_completed(futures):
        try:
            fut.result()
        except Exception as e:
            print(f"[ERRO] {futures[fut]}: {e}")
        pbar.update(1)

pbar.close()
print("[DONE]")