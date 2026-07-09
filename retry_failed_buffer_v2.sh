#!/bin/bash
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
  sleep 10
done

echo ""
echo "Done. Checking final results..."
sleep 5
curl -s http://localhost:3000/api/schedule | python3 -c "
import json, sys
data = json.load(sys.stdin)
done = [f for f, e in data.items() if e.get('bufferStatus') == 'done']
failed = [f for f, e in data.items() if e.get('bufferStatus') == 'failed']
print(f'Done: {len(done)}')
print(f'Still failed: {len(failed)}')
for f in failed:
    print(' -', f)
"
