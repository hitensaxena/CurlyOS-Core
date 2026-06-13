#!/bin/bash
# Kill the SSM-bound embed, relaunch DETACHED (survives SSM 1h timeout), resume
# from work already done, upload vectors + self-terminate the box on completion.
export BUCKET=curlyos-embed-627917840429-aps1
shutdown -c 2>/dev/null || true                 # cancel current safety timer
pkill -f aws_embed_worker.py 2>/dev/null || true  # stop the timeout-bound run
sleep 3
shutdown -h +360 curlyos-safety 2>/dev/null || true   # 6h backstop
cd /root
aws s3 cp s3://$BUCKET/aws_embed_worker.py /root/aws_embed_worker.py
setsid bash -c '
  cd /root
  /opt/ev/bin/python aws_embed_worker.py archive_chunks.jsonl archive_vectors.tsv 64 >> /var/log/embed_run.log 2>&1
  L=$(wc -l < archive_vectors.tsv)
  aws s3 cp archive_vectors.tsv s3://$BUCKET/
  echo "FINISHED lines=$L $(date)" >> /var/log/embed_run.log
  aws s3 cp /var/log/embed_run.log s3://$BUCKET/embed_run.log
  sleep 5
  shutdown -h now
' </dev/null >/var/log/embed_launch.log 2>&1 &
sleep 3
echo "resume launched; worker pids: $(pgrep -f aws_embed_worker.py | tr '\n' ' ')"
