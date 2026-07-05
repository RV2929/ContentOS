// Vercel serverless function — reads clips-data.json from GitHub.
// ContentOS pushes this file automatically after each run and each upload.
// No tunnel or local server connection required.
const RAW_URL =
  'https://raw.githubusercontent.com/RV2929/ContentOS/main/dashboard/public/clips-data.json';

module.exports = async function handler(req, res) {
  try {
    const resp = await fetch(RAW_URL, {
      headers: { 'User-Agent': 'contentos-dashboard', 'Cache-Control': 'no-cache' },
      signal: AbortSignal.timeout(8000),
    });

    if (resp.status === 404) {
      // File not yet pushed — no clips processed yet
      return res.json([]);
    }

    if (!resp.ok) throw new Error(`GitHub returned ${resp.status}`);

    const data = await resp.json();
    res.setHeader('Cache-Control', 'no-store');
    res.json(Array.isArray(data.clips) ? data.clips : []);
  } catch (err) {
    res.status(503).json({
      error: 'Could not read clips data from GitHub',
      detail: err.message,
      hint: 'Clips data syncs to GitHub after each ContentOS run or YouTube upload.',
    });
  }
};
