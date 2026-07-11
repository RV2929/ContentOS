// Vercel serverless function — reads performance.jsonl from GitHub and returns
// only the most recent row per (filename, platform) combination. The file has
// one row per clip per day it was collected, so this dedupes down to current stats.
const RAW_URL =
  'https://raw.githubusercontent.com/RV2929/ContentOS/main/dashboard/performance.jsonl';

module.exports = async function handler(req, res) {
  try {
    const resp = await fetch(RAW_URL, {
      headers: { 'User-Agent': 'contentos-dashboard', 'Cache-Control': 'no-cache' },
      signal: AbortSignal.timeout(8000),
    });

    if (resp.status === 404) {
      // File not yet pushed — no performance data collected yet
      return res.json([]);
    }

    if (!resp.ok) throw new Error(`GitHub returned ${resp.status}`);

    const text = await resp.text();
    const latest = new Map();
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let row;
      try { row = JSON.parse(trimmed); } catch { continue; }
      if (!row.filename || !row.platform) continue;
      const key = `${row.filename}::${row.platform}`;
      const existing = latest.get(key);
      if (!existing || new Date(row.collectedAt || row.date) > new Date(existing.collectedAt || existing.date)) {
        latest.set(key, row);
      }
    }

    res.setHeader('Cache-Control', 'no-store');
    res.json(Array.from(latest.values()));
  } catch (err) {
    res.status(503).json({
      error: 'Could not read performance data from GitHub',
      detail: err.message,
      hint: 'Performance data syncs to GitHub after the daily collector runs.',
    });
  }
};
