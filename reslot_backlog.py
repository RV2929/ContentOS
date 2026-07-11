"""
One-off (re-runnable) fix-up: re-spread every currently "pending" entry in
dashboard/schedule.json across the per-channel daily posting queue
(scheduler_queue.py), so the existing backlog respects the same 10/day/channel
cap and 8am-11pm posting window as newly generated clips.

"done"/"uploading" entries are left untouched (already posted / in flight)
but still count against that day's cap. "pending" entries are re-ordered by
their *original* scheduledAt (which still encodes the correct FIFO order —
earlier batches got earlier timestamps) and reassigned fresh slots starting
from now.

Usage: python3 reslot_backlog.py [--dry-run]
"""

import json
import shutil
import sys
from pathlib import Path

from scheduler_queue import allocate_slots, normalize_channel

SCHEDULE_FILE = Path(__file__).parent / "dashboard" / "schedule.json"


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    schedule = json.loads(SCHEDULE_FILE.read_text())

    pending_by_channel: dict[str, list[str]] = {}
    for filename, entry in schedule.items():
        if entry.get("status") != "pending":
            continue
        channel = normalize_channel(entry.get("channel"))
        pending_by_channel.setdefault(channel, []).append(filename)

    if not pending_by_channel:
        print("No pending clips to re-slot.")
        return

    total = 0
    for channel, filenames in pending_by_channel.items():
        # Preserve FIFO order using each entry's current (buggy) scheduledAt —
        # earlier batches were still given earlier timestamps, so sorting by
        # it recovers the correct video-processing order.
        filenames.sort(key=lambda f: schedule[f]["scheduledAt"] or "")

        slots = allocate_slots(schedule, channel, filenames)

        print(f"\n{channel}: re-slotting {len(filenames)} pending clip(s)")
        for filename in filenames:
            old_at = schedule[filename]["scheduledAt"]
            new_at = slots[filename]
            schedule[filename]["scheduledAt"] = new_at.isoformat()
            print(f"  {filename[:60]:60s}  {(old_at[:16] if old_at else 'unscheduled')}  ->  {new_at.astimezone().strftime('%Y-%m-%d %I:%M %p')}")
        total += len(filenames)

    if dry_run:
        print(f"\n[dry run] Would re-slot {total} clip(s). No changes written.")
        return

    backup = SCHEDULE_FILE.with_suffix(".json.bak")
    shutil.copy(SCHEDULE_FILE, backup)

    # Re-read + merge in case the live server (background scheduler) advanced
    # any entries (e.g. pending -> uploading/done) while we were computing.
    fresh = json.loads(SCHEDULE_FILE.read_text())
    for filenames in pending_by_channel.values():
        for filename in filenames:
            if fresh.get(filename, {}).get("status") == "pending":
                fresh[filename]["scheduledAt"] = schedule[filename]["scheduledAt"]

    SCHEDULE_FILE.write_text(json.dumps(fresh, indent=2))
    print(f"\nRe-slotted {total} clip(s). Backup saved to {backup.name}.")


if __name__ == "__main__":
    main()
