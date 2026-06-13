"""Runs ON the transient AWS box. Reads archive_chunks.jsonl ({"id","text"} per
line), embeds with BAAI/bge-m3 (same model as curlyos-core -> compatible vectors),
writes archive_vectors.tsv: '<id>\\t[<float>,...]' (pgvector literal) per line.

GPU auto-used if available, else CPU with all cores. Resumable: skips ids already
present in the output file.

    python3 aws_embed_worker.py archive_chunks.jsonl archive_vectors.tsv [BATCH]
"""
import json
import os
import sys
import time

IN = sys.argv[1] if len(sys.argv) > 1 else "archive_chunks.jsonl"
OUT = sys.argv[2] if len(sys.argv) > 2 else "archive_vectors.tsv"
BATCH = int(sys.argv[3]) if len(sys.argv) > 3 else 256

import torch
from sentence_transformers import SentenceTransformer

device = "cuda" if torch.cuda.is_available() else "cpu"
if device == "cpu":
    torch.set_num_threads(os.cpu_count() or 8)
print(f"device={device} threads={torch.get_num_threads()} batch={BATCH}", flush=True)

rows = []
with open(IN) as f:
    for line in f:
        line = line.strip()
        if line:
            d = json.loads(line)
            rows.append((d["id"], d["text"]))
print(f"loaded {len(rows)} rows", flush=True)

done = set()
if os.path.exists(OUT):
    with open(OUT) as f:
        for line in f:
            i = line.split("\t", 1)[0]
            if i:
                done.add(i)
    print(f"resume: {len(done)} already embedded", flush=True)
rows = [r for r in rows if r[0] not in done]
# Sort by length so each batch has similar-length texts -> far less padding
# waste on CPU (big throughput win for variable-length chunks).
rows.sort(key=lambda r: len(r[1] or ""))
print(f"to embed: {len(rows)} (length-sorted)", flush=True)

model = SentenceTransformer("BAAI/bge-m3", device=device)
t0 = time.time()
n = 0
with open(OUT, "a") as out:
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        vecs = model.encode([t for _, t in batch], normalize_embeddings=True,
                            batch_size=BATCH, show_progress_bar=False)
        for (rid, _), v in zip(batch, vecs):
            out.write(rid + "\t[" + ",".join(f"{float(x):.7f}" for x in v) + "]\n")
        out.flush()
        n += len(batch)
        if (i // BATCH) % 5 == 0 or n == len(rows):
            rate = n / max(time.time() - t0, 1e-3)
            print(f"  {n}/{len(rows)}  {rate:.0f}/s", flush=True)
print(f"DONE {n} embedded in {time.time()-t0:.0f}s", flush=True)
