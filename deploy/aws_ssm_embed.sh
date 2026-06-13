#!/bin/bash
# Runs on the AL2023 embed box via SSM. venv (fresh pip), embed, ship vectors to S3.
shutdown -c 2>/dev/null || true               # reset the user-data safety timer
shutdown -h +60 "curlyos safety" 2>/dev/null || true
BUCKET=curlyos-embed-627917840429-aps1
LOG=/var/log/embed.log
exec > >(tee -a "$LOG") 2>&1                   # to SSM stdout AND the log file
trap 'aws s3 cp "$LOG" s3://$BUCKET/embed.log 2>/dev/null || true' EXIT  # always ship log
set -e
echo "=== start $(date) ==="
cd /root

python3 -m venv /opt/ev
/opt/ev/bin/pip install --quiet --upgrade pip
/opt/ev/bin/pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
/opt/ev/bin/pip install --quiet sentence-transformers

aws s3 cp s3://$BUCKET/archive_chunks.jsonl .
aws s3 cp s3://$BUCKET/aws_embed_worker.py .
aws s3 cp s3://$BUCKET/archive_vectors.tsv . 2>/dev/null || true   # resume if partial

echo "=== embedding $(date) ==="
/opt/ev/bin/python aws_embed_worker.py archive_chunks.jsonl archive_vectors.tsv 256

LINES=$(wc -l < archive_vectors.tsv)
echo "=== vectors produced: $LINES ==="
aws s3 cp archive_vectors.tsv s3://$BUCKET/
echo "=== DONE $(date) lines=$LINES ==="
