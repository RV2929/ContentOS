"""
Per-channel daily posting queue.

Caps each channel (podcast, football) at DAILY_CAP posts per calendar day
(system local time), spread evenly across the full 24 hours. When a day
fills up, overflow rolls to the next day, then the next, etc. —
first-in-first-out across whatever order filenames are handed in.

Used by contentos.py to slot newly generated clips, and by reslot_backlog.py
to re-spread any already-pending entries in schedule.json.
"""

import datetime
import os
import zoneinfo

DAILY_CAP = 15

# Statuses that occupy a slot on their scheduled day (already posted, actively
# posting, or waiting to post). "failed" clips never posted, so they don't
# consume a day's cap.
ACTIVE_STATUSES = ("pending", "uploading", "done")


def normalize_channel(channel: str | None) -> str:
    """Mirror dashboard/server.js's normalizeChannel(): only "football" is
    distinct, everything else (including missing/unknown) is "podcast"."""
    return "football" if channel == "football" else "podcast"


def _local_zone() -> datetime.tzinfo:
    try:
        link = os.readlink("/etc/localtime")
        name = link.split("zoneinfo/")[-1]
        return zoneinfo.ZoneInfo(name)
    except OSError:
        return datetime.datetime.now().astimezone().tzinfo


def _slot_datetime(day: datetime.date, slot_index: int, tz: datetime.tzinfo) -> datetime.datetime:
    """The local datetime for the Nth (0-based) posting slot of a given day,
    evenly spaced across the full 24 hours."""
    total_minutes = int(slot_index * (24 * 60) / DAILY_CAP)
    hour, minute = divmod(total_minutes, 60)
    return datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def allocate_slots(
    schedule: dict,
    channel: str,
    filenames: list[str],
    now: datetime.datetime | None = None,
) -> dict[str, datetime.datetime]:
    """
    Assign each filename in `filenames` (already in FIFO order) the next
    available posting slot for `channel`, respecting DAILY_CAP/day. Existing
    schedule entries for this channel (excluding the filenames being
    (re)assigned) count against each day's cap.

    Returns {filename: scheduled_at_utc_datetime}.
    """
    tz = _local_zone()
    now = (now or datetime.datetime.now(tz)).astimezone(tz)
    channel = normalize_channel(channel)
    reassigning = set(filenames)

    # How many posts are already committed to each day for this channel,
    # from entries we are NOT re-slotting right now.
    fixed_counts: dict[datetime.date, int] = {}
    for fname, entry in schedule.items():
        if fname in reassigning:
            continue
        if normalize_channel(entry.get("channel")) != channel:
            continue
        if entry.get("status") not in ACTIVE_STATUSES:
            continue
        at = datetime.datetime.fromisoformat(entry["scheduledAt"]).astimezone(tz)
        day = at.date()
        fixed_counts[day] = fixed_counts.get(day, 0) + 1

    assigned_counts: dict[datetime.date, int] = {}   # real assignments made in this call
    claimed_slot_idx: dict[datetime.date, set] = {}  # grid positions used or burned (past)

    result: dict[str, datetime.datetime] = {}
    day = now.date()

    for fname in filenames:
        while True:
            real_used = fixed_counts.get(day, 0) + assigned_counts.get(day, 0)
            if real_used >= DAILY_CAP:
                day += datetime.timedelta(days=1)
                continue

            claimed = claimed_slot_idx.setdefault(day, set())
            chosen_idx = None
            chosen_dt = None
            for idx in range(DAILY_CAP):
                if idx in claimed:
                    continue
                candidate = _slot_datetime(day, idx, tz)
                if day == now.date() and candidate <= now:
                    claimed.add(idx)  # slot time already elapsed today — unusable
                    continue
                chosen_idx = idx
                chosen_dt = candidate
                break

            if chosen_idx is None:
                # All of today's remaining slot times have already elapsed.
                day += datetime.timedelta(days=1)
                continue

            claimed.add(chosen_idx)
            assigned_counts[day] = assigned_counts.get(day, 0) + 1
            result[fname] = chosen_dt.astimezone(datetime.timezone.utc)
            break

    return result
