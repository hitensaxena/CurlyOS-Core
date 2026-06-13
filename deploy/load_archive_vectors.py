"""Load archive_vectors.tsv ('<mem_id>\\t[<float>,...]' per line, bge-m3 1024-dim
from the AWS box) into pgvector: COPY -> temp table -> UPDATE memories.embedding
for the rows still NULL. Idempotent. Run on the curlyos-core host.

    .venv/bin/python deploy/load_archive_vectors.py /path/archive_vectors.tsv
"""
import sys
import psycopg

TSV = sys.argv[1] if len(sys.argv) > 1 else "archive_vectors.tsv"
dsn = [l.split("=", 1)[1].strip().strip('"') for l in open(".env")
       if l.startswith("CURLYOS_DATABASE_URL=")][0]

conn = psycopg.connect(dsn)
with conn.cursor() as cur:
    cur.execute("CREATE TEMP TABLE _v (id text PRIMARY KEY, emb text)")
    n = 0
    with cur.copy("COPY _v (id, emb) FROM STDIN") as copy:
        for line in open(TSV, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line:
                continue
            i, emb = line.split("\t", 1)
            copy.write_row((i, emb))
            n += 1
    print(f"staged {n} vectors")
    cur.execute(
        "UPDATE memories m SET embedding = _v.emb::vector "
        "FROM _v WHERE m.id = _v.id AND m.embedding IS NULL"
    )
    print(f"updated {cur.rowcount} memories")
conn.commit()

with conn.cursor() as cur:
    remaining = cur.execute(
        "SELECT count(*) FROM memories m JOIN episodes e ON m.source_episode_id=e.id "
        "WHERE e.source_ref LIKE 'mind:%' AND m.embedding IS NULL"
    ).fetchone()[0]
print(f"mind chunk-memories still NULL: {remaining}")
conn.close()
