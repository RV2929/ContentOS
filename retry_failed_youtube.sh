#!/bin/bash
# Retries specific failed YouTube uploads (excludes clips that hit the daily quota cap).

ACCOUNT_ID=$(curl -s http://localhost:3000/api/youtube/status | python3 -c "
import json, sys
data = json.load(sys.stdin)
connected = [a['id'] for a in data.get('accounts', []) if a.get('connected')]
print(connected[0] if connected else '')
")

if [ -z "$ACCOUNT_ID" ]; then
  echo "No connected YouTube account found. Aborting."
  exit 1
fi

echo "Using YouTube account: $ACCOUNT_ID"
echo ""

# Only the connection-error failures — NOT the quota-cap one (Best_Financial_Advice clip08)
FILES=(
  "TOM_HOLLAND_How_Tom_Overcame_Social_Anxi_clip05_A_jaw-dropping_high-stakes_awe_story_of_.mp4"
  "TOM_HOLLAND_How_Tom_Overcame_Social_Anxi_clip06_Jays_story_of_making_a_peace_sound_and_h.mp4"
  "Communicate_with_Confidence_The_Blueprin_clip05_Genius_parentingrelationship_reframe_wit.mp4"
  "Communicate_with_Confidence_The_Blueprin_clip06_A_vivid_before-and-after_demo_of_watered.mp4"
  "Communicate_with_Confidence_The_Blueprin_clip07_The_counterintuitive_gem_the_more_words_.mp4"
  "Billionaires_WARNING_Im_SELLING_The_Cras_clip07_Maternal_mortality_as_the_measure_of_civ.mp4"
)

for filename in "${FILES[@]}"; do
  TITLE=$(python3 -c "
import json
with open('dashboard/schedule.json') as f:
    data = json.load(f)
print(data.get('$filename', {}).get('title', ''))
")
  DESC=$(python3 -c "
import json
with open('dashboard/schedule.json') as f:
    data = json.load(f)
print(data.get('$filename', {}).get('description', ''))
")

  echo "Retrying: $filename"
  JOB=$(curl -s -X POST http://localhost:3000/api/upload/youtube \
    -H "Content-Type: application/json" \
    --data-binary @<(python3 -c "
import json
print(json.dumps({
    'filename': '''$filename''',
    'title': '''$TITLE''',
    'description': '''$DESC''',
    'visibility': 'public',
    'accountId': '''$ACCOUNT_ID'''
}))
"))

  JOB_ID=$(echo "$JOB" | python3 -c "import json,sys; print(json.load(sys.stdin).get('jobId',''))")

  if [ -z "$JOB_ID" ]; then
    echo "  Failed to start job: $JOB"
    continue
  fi

  for i in $(seq 1 60); do
    sleep 3
    STATUS=$(curl -s http://localhost:3000/api/upload-progress/$JOB_ID | tail -1 | python3 -c "
import json,sys
try:
    for line in sys.stdin:
        if line.startswith('data: '):
            last = line[6:]
    d = json.loads(last)
    print(d.get('status',''))
except: print('')
" 2>/dev/null)
    if [ "$STATUS" = "complete" ] || [ "$STATUS" = "error" ]; then
      echo "  Result: $STATUS"
      break
    fi
  done

  echo "  Waiting 20s before next upload to stay well clear of any rate/quota limits..."
  sleep 20
done

echo ""
echo "Done. Note: 'The_Best_Financial_Advice...clip08' was NOT retried (hit daily quota cap earlier) — check tomorrow."
