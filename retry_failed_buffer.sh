#!/bin/bash
# Retries every clip with bufferStatus "failed" through /api/upload/buffer,
# waiting 8 seconds between each to avoid Buffer's rate limit.

FILES=$(python3 -c "
import json
with open('dashboard/schedule.json') as f:
    data = json.load(f)
for filename, entry in data.items():
    if entry.get('bufferStatus') == 'failed':
        print(filename)
")

if [ -z "$FILES" ]; then
  echo "No failed Buffer posts found."
  exit 0
fi

echo "$FILES" | while IFS= read -r filename; do
  echo "Retrying: $filename"
  curl -s -X POST http://localhost:3000/api/upload/buffer \
    -H "Content-Type: application/json" \
    -d "{\"filename\":\"$filename\"}"
  echo ""
  sleep 8
done

echo "Done. Check statuses with: curl -s http://localhost:3000/api/schedule | python3 -m json.tool | grep -A2 bufferStatus"
