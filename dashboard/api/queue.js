// Vercel serverless function — reads queue-data.json from GitHub.
// Local server pushes this file after every queue status change.
const RAW_URL =
  'https://raw.githubusercontent.com/RV2929/ContentOS/main/dashboard/public/queue-data.json';

module.exports = async function handler(req, res) {
  try {
    const resp = await fetch(RAW_URL, {
      headers: { 'User-Agent': 'contentos-dashboard', 'Cache-Control': 'no-cache' },
      signal: AbortSignal.timeout(8000),
    });
    if (resp.status === 404) return res.json({ queue: [] });
    if (!resp.ok) throw new Error(`GitHub returned ${resp.status}`);
    const data = await resp.json();
    res.setHeader('Cache-Control', 'no-store');
    res.json(data && Array.isArray(data.queue) ? data : { queue: [] });
  } catch (err) {
    res.status(503).json({ queue: [], error: err.message });
  }
};
