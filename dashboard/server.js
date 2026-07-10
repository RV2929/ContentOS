const express = require('express');
const fs = require('fs');
const path = require('path');
const { exec, spawn } = require('child_process');
const { promisify } = require('util');
const execAsync = promisify(exec);
const { google } = require('googleapis');

const app = express();
app.use(express.json());
const PORT = process.env.PORT || 3000;
// PUBLIC_URL is set when running behind a tunnel (e.g. https://contentos.yourdomain.com).
// Falls back to localhost for local-only use.
const PUBLIC_URL = process.env.PUBLIC_URL || `http://localhost:${PORT}`;
// Clips are split by channel to keep storage organized: clips/podcast/, clips/football/.
const CLIPS_BASE_DIR = path.join(process.env.HOME, 'Desktop', 'ContentOS', 'clips');
function clipsDirFor(channel) {
  return path.join(CLIPS_BASE_DIR, normalizeChannel(channel));
}

// Resolve a clip filename to its on-disk path. Prefers the channel tagged in
// schedule.json/state.json (fast path — one stat), but falls back to checking
// both channel subfolders and the legacy flat clips/ dir so a missing/stale
// channel tag, or a file that hasn't been migrated yet, can never turn into a
// false 404.
function resolveClipPath(filename) {
  filename = path.basename(filename); // prevent path traversal via ../
  const sched = loadSchedule()[filename];
  const state = loadJSON(STATE_FILE, {})[filename];
  const taggedChannel = sched?.channel || state?.channel;
  if (taggedChannel) {
    const preferred = path.join(clipsDirFor(taggedChannel), filename);
    if (fs.existsSync(preferred)) return preferred;
  }
  for (const dir of [clipsDirFor('podcast'), clipsDirFor('football'), CLIPS_BASE_DIR]) {
    const p = path.join(dir, filename);
    if (fs.existsSync(p)) return p;
  }
  return path.join(clipsDirFor(taggedChannel || 'podcast'), filename);
}

// Scans clips/podcast/, clips/football/, and (for safety) the legacy flat
// clips/ dir. Returns Map<filename, { channel, dir }> — folder location is
// authoritative for channel when known, since schedule.json may not have an
// entry yet (freshly clipped) or ever (manually-uploaded clips).
function listAllClipFiles() {
  const found = new Map();
  for (const channel of ['podcast', 'football']) {
    const dir = clipsDirFor(channel);
    let files = [];
    try { files = fs.readdirSync(dir).filter(f => /\.(mp4|mov|webm|avi)$/i.test(f)); } catch (_) { /* not created yet */ }
    for (const f of files) found.set(f, { channel, dir });
  }
  let legacy = [];
  try { legacy = fs.readdirSync(CLIPS_BASE_DIR).filter(f => /\.(mp4|mov|webm|avi)$/i.test(f)); } catch (_) { /* clips dir may not exist yet */ }
  for (const f of legacy) {
    if (!found.has(f)) found.set(f, { channel: null, dir: CLIPS_BASE_DIR });
  }
  return found;
}

const STATE_FILE = path.join(__dirname, 'state.json');
const CONFIG_FILE = path.join(__dirname, 'config.json');
const CREDENTIALS_FILE = path.join(__dirname, 'yt-credentials.json');
const LEGACY_TOKENS_FILE = path.join(__dirname, 'yt-tokens.json');
// Each connected YouTube channel gets its own token file so multiple accounts
// can be authorized and used simultaneously.
function tokensFileFor(accountId) {
  return path.join(__dirname, `yt-tokens-${accountId}.json`);
}
const SCHEDULE_FILE  = path.join(__dirname, 'schedule.json');
const QUEUE_FILE     = path.join(__dirname, 'queue.json');
const QUEUE_DATA_FILE = path.join(__dirname, 'public', 'queue-data.json');
const CONTENTOS_DIR      = path.join(__dirname, '..');
const RUN_SH             = path.join(CONTENTOS_DIR, 'run.sh');
const VENV_PYTHON        = path.join(CONTENTOS_DIR, 'venv', 'bin', 'python');
const BUFFER_POSTER      = path.join(CONTENTOS_DIR, 'buffer_poster.py');
const PERFORMANCE_COLLECTOR   = path.join(CONTENTOS_DIR, 'collect_performance.py');
const PERFORMANCE_STATE_FILE  = path.join(__dirname, 'performance-state.json');
const PERFORMANCE_LOG_FILE    = path.join(__dirname, 'performance.log');

// youtube (full scope) is required for both videos.insert and videos.update (cross-linking)
const SCOPES = [
  'https://www.googleapis.com/auth/youtube',
];

const DEFAULT_CONFIG = {
  accounts: [
    { id: 'yt-1', platform: 'youtube', name: 'Main YouTube' },
    { id: 'tt-1', platform: 'tiktok', name: 'Clipperz291' },
    { id: 'ig-1', platform: 'instagram', name: 'Main Instagram' }
  ]
};

// Which YouTube account each channel tab uploads to.
const CHANNEL_ACCOUNT_MAP = {
  podcast:  'yo-1782235160731', // Clipperz29
  football: 'yo-1783518807860', // Footy29
};
function accountIdForChannel(channel) {
  return CHANNEL_ACCOUNT_MAP[channel] || CHANNEL_ACCOUNT_MAP.podcast;
}
function normalizeChannel(channel) {
  return channel === 'football' ? 'football' : 'podcast';
}

// In-memory upload jobs: jobId → { status, percent, filename, videoId?, error? }
const uploadJobs = new Map();

// Filenames the scheduler is currently uploading (prevents double-firing)
const schedulingInProgress = new Set();

// Filenames currently being sent to Buffer
const bufferInProgress = new Set();

// Filenames currently being sent to Buffer/TikTok (tracked separately from
// Instagram so the two platforms can post independently)
const tiktokBufferInProgress = new Set();

// ── GitHub sync ───────────────────────────────────────────────────────────────
// Writes clips-data.json and pushes to GitHub so Vercel reads fresh data
// without needing a tunnel. Debounced so rapid back-to-back changes only
// trigger one push.

const CLIPS_DATA_FILE = path.join(__dirname, 'public', 'clips-data.json');
let _syncTimer = null;

function scheduleSyncToGitHub(reason) {
  if (_syncTimer) clearTimeout(_syncTimer);
  _syncTimer = setTimeout(() => {
    _syncTimer = null;
    syncToGitHub(reason).catch(err => console.error('[sync]', err.message));
  }, 3000);
}

async function syncToGitHub(label) {
  try {
    const clipFiles = listAllClipFiles();
    const files = Array.from(clipFiles.keys()).sort();

    const state    = loadJSON(STATE_FILE, {});
    const schedule = loadSchedule();

    const clips = files.map(filename => {
      const stem = filename.replace(/\.[^.]+$/, '');
      const s    = state[filename]    || {};
      const sch  = schedule[filename] || {};
      const pending = sch.status === 'pending' || sch.status === 'uploading';
      const thumbFile = path.join(THUMBNAILS_DIR, `${stem}.jpg`);
      return {
        filename,
        status:        s.status    || 'ready',
        youtubeId:     s.youtubeId || '',
        scheduledAt:   pending ? sch.scheduledAt : null,
        scheduleStatus: sch.status || null,
        title:         sch.title   || '',
        channel:       clipFiles.get(filename)?.channel || sch.channel || s.channel || 'podcast',
        thumbnailPath: fs.existsSync(thumbFile) ? `/thumbnails/${stem}.jpg` : null,
      };
    });

    fs.writeFileSync(
      CLIPS_DATA_FILE,
      JSON.stringify({ lastUpdated: new Date().toISOString(), clips }, null, 2),
    );

    await execAsync('git add public/clips-data.json public/thumbnails/ public/queue-data.json', { cwd: __dirname });
    try {
      await execAsync(`git commit -m "sync: ${label}"`, { cwd: __dirname });
      await execAsync('git push', { cwd: __dirname });
      console.log(`[sync] Pushed to GitHub — ${clips.length} clip(s)`);
    } catch (e) {
      const out = (e.stderr || '') + (e.stdout || '');
      if (!out.includes('nothing to commit') && !out.includes('up to date')) {
        console.error('[sync] git error:', out.trim());
      }
    }
  } catch (err) {
    console.error('[sync] failed:', err.message);
  }
}
// ── URL Queue ─────────────────────────────────────────────────────────────────

function loadQueue()    { return loadJSON(QUEUE_FILE, { queue: [] }); }
function saveQueue(q)   { saveJSON(QUEUE_FILE, q); }

function updateQueueItem(id, updates) {
  const q = loadQueue();
  const item = q.queue.find(i => i.id === id);
  if (item) Object.assign(item, updates);
  saveQueue(q);
}

function writeQueueData() {
  const q = loadQueue();
  fs.writeFileSync(QUEUE_DATA_FILE, JSON.stringify(q, null, 2));
}

// Extracts the YouTube video ID from any standard URL format.
// Returns null if it cannot be determined.
function extractVideoId(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.replace(/^www\./, '');
    if (host === 'youtu.be') return u.pathname.slice(1).split('?')[0] || null;
    if (host === 'youtube.com' || host === 'm.youtube.com') {
      if (u.pathname.startsWith('/shorts/')) return u.pathname.slice(8).split('/')[0] || null;
      if (u.pathname.startsWith('/live/'))   return u.pathname.slice(6).split('/')[0] || null;
      return u.searchParams.get('v') || null;
    }
  } catch { /* ignore malformed URLs */ }
  return null;
}

// GET /api/queue — full queue state
app.get('/api/queue', (req, res) => res.json(loadQueue()));

// POST /api/queue — add a URL
app.post('/api/queue', (req, res) => {
  const { url, channel } = req.body || {};
  if (!url || !/^https?:\/\//i.test(url.trim())) {
    return res.status(400).json({ error: 'Valid YouTube URL required' });
  }

  const trimmed = url.trim();
  const newId   = extractVideoId(trimmed);
  const q       = loadQueue();

  // Reject duplicates against any non-failed item (failed items can be retried)
  const duplicate = q.queue.find(item => {
    if (item.status === 'failed') return false;
    if (newId) return extractVideoId(item.url) === newId;
    return item.url === trimmed; // fallback: exact URL match
  });
  if (duplicate) {
    return res.status(409).json({ error: 'This video has already been processed or is in the queue' });
  }

  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  q.queue.push({
    id, url: trimmed, title: '', status: 'queued',
    channel: normalizeChannel(channel),
    addedAt: new Date().toISOString(), startedAt: null,
    completedAt: null, clipsCount: 0, batchId: null, error: null,
  });
  saveQueue(q);
  writeQueueData();
  scheduleSyncToGitHub('queue: added URL');
  processQueue();
  res.json({ ok: true, id });
});

// DELETE /api/queue/:id — remove an item (not while processing)
app.delete('/api/queue/:id', (req, res) => {
  const q = loadQueue();
  const item = q.queue.find(i => i.id === req.params.id);
  if (!item) return res.status(404).json({ error: 'Not found' });
  if (['downloading', 'transcribing', 'generating_clips'].includes(item.status)) {
    return res.status(409).json({ error: 'Cannot remove — item is currently processing' });
  }
  q.queue = q.queue.filter(i => i.id !== req.params.id);
  saveQueue(q);
  writeQueueData();
  scheduleSyncToGitHub('queue: removed item');
  res.json({ ok: true });
});

// ── Queue processor ───────────────────────────────────────────────────────────

let queueBusy = false;

async function processQueue() {
  if (queueBusy) return;
  const q = loadQueue();
  const next = q.queue.find(i => i.status === 'queued');
  if (!next) return;

  queueBusy = true;
  const { id, url } = next;
  const channel = normalizeChannel(next.channel);
  console.log(`[queue] Starting: ${url} (channel: ${channel})`);

  try {
    updateQueueItem(id, { status: 'downloading', startedAt: new Date().toISOString() });
    writeQueueData();
    scheduleSyncToGitHub(`queue: downloading`);

    // Snapshot schedule keys so we can detect newly added batch after run
    const schedBefore = new Set(Object.keys(loadSchedule()));

    await new Promise((resolve, reject) => {
      const proc = spawn('/bin/zsh', [RUN_SH, url, '--channel', channel], { cwd: CONTENTOS_DIR });
      let stderr = '';

      proc.stdout.on('data', chunk => {
        const text = chunk.toString();
        process.stdout.write(text);

        // Detect pipeline step from _header() output and update status
        if      (text.includes('Step 2:')) { updateQueueItem(id, { status: 'transcribing' });    writeQueueData(); scheduleSyncToGitHub('queue: transcribing'); }
        else if (text.includes('Step 3:') ||
                 text.includes('Step 4:')) { updateQueueItem(id, { status: 'generating_clips' }); writeQueueData(); scheduleSyncToGitHub('queue: generating clips'); }
      });

      proc.stderr.on('data', chunk => { stderr += chunk.toString(); process.stderr.write(chunk); });
      proc.on('close', code => {
        if (code === 0) resolve();
        else reject(new Error(stderr.slice(-400) || `exited ${code}`));
      });
    });

    // Detect new schedule entries to get batchId and clip count
    const schedAfter = loadSchedule();
    const newFiles   = Object.keys(schedAfter).filter(f => !schedBefore.has(f));
    const batchId    = newFiles.length ? schedAfter[newFiles[0]].batchId || null : null;

    updateQueueItem(id, { status: 'scheduled', completedAt: new Date().toISOString(), clipsCount: newFiles.length, batchId });
    writeQueueData();
    scheduleSyncToGitHub(`queue: scheduled ${newFiles.length} clip(s)`);
    console.log(`[queue] Done: ${url} → ${newFiles.length} clip(s) scheduled`);

  } catch (err) {
    console.error('[queue] Failed:', err.message);
    updateQueueItem(id, { status: 'failed', completedAt: new Date().toISOString(), error: err.message.slice(0, 300) });
    writeQueueData();
    scheduleSyncToGitHub('queue: failed');
  } finally {
    queueBusy = false;
    setTimeout(processQueue, 2000); // pick up next item if any
  }
}

// Called after cross-linking completes to mark queue item 'done'
function markQueueBatchDone(batchId) {
  if (!batchId) return;
  const q = loadQueue();
  const item = q.queue.find(i => i.batchId === batchId && i.status === 'scheduled');
  if (!item) return;
  item.status = 'done';
  item.completedAt = new Date().toISOString();
  saveQueue(q);
  writeQueueData();
  scheduleSyncToGitHub(`queue: done batch ${batchId}`);
}

// Reset any items stuck mid-processing (e.g. after server restart)
(function recoverStuckItems() {
  const q = loadQueue();
  const stuck = ['downloading', 'transcribing', 'generating_clips'];
  let changed = false;
  for (const item of q.queue) {
    if (stuck.includes(item.status)) { item.status = 'queued'; item.startedAt = null; changed = true; }
  }
  if (changed) { saveQueue(q); console.log('[queue] Reset stuck items to queued'); }
})();

// ── Batch IDs currently being cross-linked (prevents duplicate runs) ──────────
const crossLinkInProgress = new Set();

// ── Helpers ──────────────────────────────────────────────────────────────────

function loadJSON(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch { return fallback; }
}

function saveJSON(file, data) {
  fs.writeFileSync(file, JSON.stringify(data, null, 2));
}

function ensureConfig() {
  if (!fs.existsSync(CONFIG_FILE)) saveJSON(CONFIG_FILE, DEFAULT_CONFIG);
}

function getYoutubeAccounts() {
  ensureConfig();
  return (loadJSON(CONFIG_FILE, DEFAULT_CONFIG).accounts || []).filter(a => a.platform === 'youtube');
}

// One-time migration: the old single-account setup stored tokens at
// yt-tokens.json. Move it onto the first configured YouTube account so
// existing connections keep working after upgrading to multi-account support.
function migrateLegacyTokens() {
  if (!fs.existsSync(LEGACY_TOKENS_FILE)) return;
  const firstYt = getYoutubeAccounts()[0];
  if (!firstYt) return; // no account to migrate into yet — leave legacy file alone
  const dest = tokensFileFor(firstYt.id);
  if (!fs.existsSync(dest)) {
    fs.copyFileSync(LEGACY_TOKENS_FILE, dest);
    console.log(`[migrate] yt-tokens.json → yt-tokens-${firstYt.id}.json (${firstYt.name})`);
  }
  fs.unlinkSync(LEGACY_TOKENS_FILE);
}
migrateLegacyTokens();

// One-time migration: tag any pre-existing queue/schedule entries created
// before multi-channel support with the "podcast" channel so they keep
// routing to the original Clipperz29 account.
function migrateChannelTags() {
  const q = loadJSON(QUEUE_FILE, { queue: [] });
  let queueChanged = false;
  for (const item of q.queue) {
    if (!item.channel) { item.channel = 'podcast'; queueChanged = true; }
  }
  if (queueChanged) saveJSON(QUEUE_FILE, q);

  const sched = loadJSON(SCHEDULE_FILE, {});
  let schedChanged = false;
  for (const entry of Object.values(sched)) {
    if (!entry.channel) { entry.channel = 'podcast'; schedChanged = true; }
  }
  if (schedChanged) saveJSON(SCHEDULE_FILE, sched);
}
migrateChannelTags();

function loadSchedule() {
  return loadJSON(SCHEDULE_FILE, {});
}

function loadCredentials() {
  const raw = loadJSON(CREDENTIALS_FILE, null);
  if (!raw) return null;
  // Support { installed: {...} }, { web: {...} }, or flat { client_id, client_secret }
  const c = raw.installed || raw.web || raw;
  return (c.client_id && c.client_secret) ? c : null;
}

function getOAuthClient() {
  const creds = loadCredentials();
  if (!creds) throw new Error('No YouTube credentials configured');
  return new google.auth.OAuth2(
    creds.client_id,
    creds.client_secret,
    `${PUBLIC_URL}/auth/youtube/callback`
  );
}

function getAuthedClient(accountId) {
  if (!accountId) throw new Error('accountId required');
  const client = getOAuthClient();
  const tokensFile = tokensFileFor(accountId);
  const tokens = loadJSON(tokensFile, null);
  if (!tokens) throw new Error('That YouTube account is not connected — please authorize it first');
  client.setCredentials(tokens);
  // Persist refreshed tokens automatically
  client.on('tokens', (fresh) => {
    const current = loadJSON(tokensFile, {});
    saveJSON(tokensFile, { ...current, ...fresh });
  });
  return client;
}

// ── Express setup ─────────────────────────────────────────────────────────────

app.use(express.static(path.join(__dirname, 'public')));

// ── Video streaming (range request support for seeking) ───────────────────────

app.get('/clips/:filename', (req, res) => {
  const filename = decodeURIComponent(req.params.filename);
  const filePath = resolveClipPath(filename);
  if (!filePath.startsWith(CLIPS_BASE_DIR + path.sep))
    return res.status(403).send('Forbidden');
  if (!fs.existsSync(filePath)) return res.status(404).send('Not found');

  const { size } = fs.statSync(filePath);
  const range = req.headers.range;
  if (range) {
    const [s, e] = range.replace(/bytes=/, '').split('-');
    const start = parseInt(s, 10);
    const end = e ? parseInt(e, 10) : size - 1;
    res.writeHead(206, {
      'Content-Range': `bytes ${start}-${end}/${size}`,
      'Accept-Ranges': 'bytes',
      'Content-Length': end - start + 1,
      'Content-Type': 'video/mp4',
    });
    fs.createReadStream(filePath, { start, end }).pipe(res);
  } else {
    res.writeHead(200, { 'Content-Length': size, 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes' });
    fs.createReadStream(filePath).pipe(res);
  }
});

// ── Clips ─────────────────────────────────────────────────────────────────────

const THUMBNAILS_DIR = path.join(__dirname, 'public', 'thumbnails');

app.get('/api/clips', (req, res) => {
  const clipFiles = listAllClipFiles();
  const files = Array.from(clipFiles.keys()).sort();

  const state = loadJSON(STATE_FILE, {});
  const schedule = loadSchedule();
  res.json(files.map(filename => {
    const sched = schedule[filename];
    const schedPending = sched?.status === 'pending' || sched?.status === 'uploading';
    const stem = filename.replace(/\.[^.]+$/, '');
    const thumbFile = path.join(THUMBNAILS_DIR, `${stem}.jpg`);
    const thumbnailUrl = fs.existsSync(thumbFile)
      ? `${PUBLIC_URL}/thumbnails/${stem}.jpg`
      : null;
    return {
      filename,
      status: state[filename]?.status || 'ready',
      platform: state[filename]?.platform || '',
      account: state[filename]?.account || '',
      youtubeId: state[filename]?.youtubeId || '',
      scheduledAt: schedPending ? sched.scheduledAt : null,
      scheduleStatus: sched?.status || null,
      bufferStatus: sched?.bufferStatus || null,
      tiktokBufferStatus: sched?.tiktokBufferStatus || null,
      channel: clipFiles.get(filename)?.channel || sched?.channel || state[filename]?.channel || 'podcast',
      thumbnailUrl,
    };
  }));
});

app.put('/api/clips/:filename', (req, res) => {
  const filename = decodeURIComponent(req.params.filename);
  const state = loadJSON(STATE_FILE, {});
  state[filename] = { ...(state[filename] || {}), ...req.body };
  saveJSON(STATE_FILE, state);
  res.json({ ok: true });
});

// ── Accounts ──────────────────────────────────────────────────────────────────

app.get('/api/accounts', (req, res) => {
  ensureConfig();
  res.json(loadJSON(CONFIG_FILE, DEFAULT_CONFIG).accounts || []);
});

app.post('/api/accounts', (req, res) => {
  const { platform, name } = req.body;
  if (!platform || !name?.trim()) return res.status(400).json({ error: 'platform and name required' });
  ensureConfig();
  const config = loadJSON(CONFIG_FILE, DEFAULT_CONFIG);
  const id = `${platform.slice(0, 2)}-${Date.now()}`;
  const account = { id, platform: platform.toLowerCase(), name: name.trim() };
  config.accounts.push(account);
  saveJSON(CONFIG_FILE, config);
  res.json({ ok: true, account });
});

app.delete('/api/accounts/:id', (req, res) => {
  ensureConfig();
  const config = loadJSON(CONFIG_FILE, DEFAULT_CONFIG);
  config.accounts = (config.accounts || []).filter(a => a.id !== req.params.id);
  saveJSON(CONFIG_FILE, config);
  res.json({ ok: true });
});

// ── YouTube OAuth ──────────────────────────────────────────────────────────────

app.get('/auth/youtube', (req, res) => {
  const { accountId } = req.query;
  if (!accountId) return res.redirect('/?yt_error=' + encodeURIComponent('No account selected to connect'));
  try {
    const url = getOAuthClient().generateAuthUrl({
      access_type: 'offline',
      scope: SCOPES,
      prompt: 'consent', // always get refresh_token
      state: accountId, // carried through the redirect so the callback knows which account this is
    });
    res.redirect(url);
  } catch (e) {
    res.redirect('/?yt_error=' + encodeURIComponent(e.message));
  }
});

app.get('/auth/youtube/callback', async (req, res) => {
  const { code, error, state } = req.query;
  if (error) return res.redirect('/?yt_error=' + encodeURIComponent(error));
  if (!code) return res.redirect('/?yt_error=no_code');
  if (!state) return res.redirect('/?yt_error=' + encodeURIComponent('Missing account reference'));
  try {
    const { tokens } = await getOAuthClient().getToken(code);
    saveJSON(tokensFileFor(state), tokens);
    res.redirect('/?yt_connected=' + encodeURIComponent(state));
  } catch (e) {
    res.redirect('/?yt_error=' + encodeURIComponent(e.message));
  }
});

// ── YouTube API endpoints ──────────────────────────────────────────────────────

// Returns connection status for every configured YouTube account so the
// dashboard can manage multiple channels at once.
app.get('/api/youtube/status', async (req, res) => {
  const hasCreds = !!loadCredentials();
  const ytAccounts = getYoutubeAccounts();

  if (!hasCreds) {
    return res.json({
      hasCredentials: false,
      accounts: ytAccounts.map(a => ({ id: a.id, name: a.name, connected: false })),
    });
  }

  const accounts = await Promise.all(ytAccounts.map(async (a) => {
    const tokens = loadJSON(tokensFileFor(a.id), null);
    if (!tokens) return { id: a.id, name: a.name, connected: false };
    try {
      const client = getOAuthClient();
      client.setCredentials(tokens);
      const yt = google.youtube({ version: 'v3', auth: client });
      const { data } = await yt.channels.list({ part: ['snippet'], mine: true });
      const channelName = data.items?.[0]?.snippet?.title || 'Your Channel';
      return { id: a.id, name: a.name, connected: true, channelName };
    } catch (e) {
      return { id: a.id, name: a.name, connected: false, error: e.message };
    }
  }));

  res.json({ hasCredentials: true, accounts });
});

app.post('/api/youtube/credentials', (req, res) => {
  const { client_id, client_secret } = req.body;
  if (!client_id?.trim() || !client_secret?.trim())
    return res.status(400).json({ error: 'client_id and client_secret are required' });
  saveJSON(CREDENTIALS_FILE, { web: { client_id: client_id.trim(), client_secret: client_secret.trim() } });
  // Clear stale tokens for every account — refresh tokens are tied to the OAuth client
  for (const a of getYoutubeAccounts()) {
    const f = tokensFileFor(a.id);
    if (fs.existsSync(f)) fs.unlinkSync(f);
  }
  res.json({ ok: true });
});

// Reset credentials entirely (removes the OAuth app + every account's tokens)
app.delete('/api/youtube/credentials', (req, res) => {
  if (fs.existsSync(CREDENTIALS_FILE)) fs.unlinkSync(CREDENTIALS_FILE);
  for (const a of getYoutubeAccounts()) {
    const f = tokensFileFor(a.id);
    if (fs.existsSync(f)) fs.unlinkSync(f);
  }
  res.json({ ok: true });
});

// Disconnect a single YouTube account without touching the others
app.delete('/api/youtube/disconnect/:accountId', (req, res) => {
  const f = tokensFileFor(req.params.accountId);
  if (fs.existsSync(f)) fs.unlinkSync(f);
  res.json({ ok: true });
});

// ── Upload ────────────────────────────────────────────────────────────────────

// platform: 'instagram' (default) | 'tiktok' — TikTok posts track status
// under separate tiktokBufferStatus/tiktokBufferError keys (and a separate
// in-progress set) so a manual retry of one platform never clobbers the
// other's state, matching how the background scheduler keeps them independent.
app.post('/api/upload/buffer', (req, res) => {
  const { filename, platform = 'instagram' } = req.body;
  if (!filename) return res.status(400).json({ error: 'filename required' });
  if (!['instagram', 'tiktok'].includes(platform)) {
    return res.status(400).json({ error: 'platform must be "instagram" or "tiktok"' });
  }

  const filePath = resolveClipPath(filename);
  if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'Clip not found' });

  const bufferToken = readBufferToken();
  if (!bufferToken) return res.status(400).json({ error: 'BUFFER_ACCESS_TOKEN not set in .env' });

  const inProgressSet = platform === 'tiktok' ? tiktokBufferInProgress : bufferInProgress;
  const statusKey = platform === 'tiktok' ? 'tiktokBufferStatus' : 'bufferStatus';
  const errorKey  = platform === 'tiktok' ? 'tiktokBufferError'  : 'bufferError';
  const postIdKey = platform === 'tiktok' ? 'tiktokBufferPostId' : 'bufferPostId';

  if (inProgressSet.has(filename)) return res.status(409).json({ error: `Already posting to Buffer (${platform})` });

  const entry = loadSchedule()[filename] || {};
  const title  = entry.title || path.basename(filename, path.extname(filename)).replace(/_/g, ' ');
  const caption = platform === 'tiktok' ? buildTikTokCaption(title) : buildBufferCaption(title);

  const s = loadSchedule();
  if (!s[filename]) s[filename] = {};
  s[filename][statusKey] = 'uploading';
  saveJSON(SCHEDULE_FILE, s);

  inProgressSet.add(filename);
  res.json({ ok: true });

  doBufferPost(filePath, filename, caption, platform)
    .then((result) => {
      const s2 = loadSchedule();
      if (!s2[filename]) s2[filename] = {};
      s2[filename][statusKey] = 'done';
      if (result?.updateId) s2[filename][postIdKey] = result.updateId;
      delete s2[filename][errorKey];
      saveJSON(SCHEDULE_FILE, s2);
      console.log(`[buffer:${platform}] Manual post done: ${filename}`);
      scheduleSyncToGitHub(`buffer ${platform} done ${filename}`);
    })
    .catch(err => {
      console.error(`[buffer:${platform}] Manual post failed ${filename}: ${err.message}`);
      const s2 = loadSchedule();
      if (!s2[filename]) s2[filename] = {};
      s2[filename][statusKey] = 'failed';
      s2[filename][errorKey]  = err.message.slice(0, 200);
      saveJSON(SCHEDULE_FILE, s2);
      scheduleSyncToGitHub(`buffer ${platform} failed ${filename}`);
    })
    .finally(() => inProgressSet.delete(filename));
});

app.post('/api/upload/youtube', (req, res) => {
  const { filename, title, description, visibility, accountId } = req.body;
  if (!filename) return res.status(400).json({ error: 'filename required' });
  if (!accountId) return res.status(400).json({ error: 'Choose which YouTube account to upload to' });

  const filePath = resolveClipPath(filename);
  if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'Clip not found' });
  if (!loadJSON(tokensFileFor(accountId), null)) return res.status(401).json({ error: 'That YouTube account is not connected' });

  const jobId = `yt-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  uploadJobs.set(jobId, { status: 'starting', percent: 0, filename });

  res.json({ jobId });

  doUpload(jobId, filePath, filename, { title, description, visibility, accountId }).catch(console.error);
});

async function doUpload(jobId, filePath, filename, meta) {
  const set = (update) => uploadJobs.set(jobId, { ...uploadJobs.get(jobId), ...update });
  try {
    set({ status: 'uploading', percent: 0 });

    const client = getAuthedClient(meta.accountId);
    const youtube = google.youtube({ version: 'v3', auth: client });
    const fileSize = fs.statSync(filePath).size;

    const rawDesc = meta.description?.trim() || '';
    // Auto-generated descriptions already contain #Shorts — don't duplicate it
    const desc = rawDesc
      ? (rawDesc.toLowerCase().includes('#shorts') ? rawDesc : rawDesc + '\n\n#Shorts')
      : '#Shorts';

    const { data } = await youtube.videos.insert(
      {
        part: ['snippet', 'status'],
        requestBody: {
          snippet: {
            title: meta.title || path.basename(filePath, path.extname(filePath)).replace(/_/g, ' '),
            description: desc,
            categoryId: '22', // People & Blogs
          },
          status: {
            privacyStatus: meta.visibility || 'private',
            selfDeclaredMadeForKids: false,
          },
        },
        media: { mimeType: 'video/mp4', body: fs.createReadStream(filePath) },
      },
      {
        onUploadProgress: (evt) => {
          if (fileSize > 0 && evt.bytesUploaded) {
            set({ percent: Math.min(99, Math.round(evt.bytesUploaded / fileSize * 100)) });
          }
        },
      }
    );

    // Persist to state
    const state = loadJSON(STATE_FILE, {});
    state[filename] = { ...(state[filename] || {}), status: 'posted', youtubeId: data.id, ytAccountId: meta.accountId };
    saveJSON(STATE_FILE, state);

    set({ status: 'complete', percent: 100, videoId: data.id });
  } catch (err) {
    const msg = err?.errors?.[0]?.message || err.message || 'Upload failed';
    set({ status: 'error', error: msg });

    const state = loadJSON(STATE_FILE, {});
    if (state[filename]) { state[filename].status = 'failed'; saveJSON(STATE_FILE, state); }
  }

  // Keep job result in memory for 10 min so SSE clients can retrieve it
  setTimeout(() => uploadJobs.delete(jobId), 10 * 60 * 1000);
}

// ── Upload progress (Server-Sent Events) ──────────────────────────────────────

app.get('/api/upload-progress/:jobId', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
  });

  const { jobId } = req.params;
  let closed = false;

  const send = (data) => { if (!closed) res.write(`data: ${JSON.stringify(data)}\n\n`); };

  let timer;
  const tick = () => {
    const job = uploadJobs.get(jobId);
    if (!job) { send({ status: 'not_found' }); clearInterval(timer); res.end(); return; }
    send(job);
    if (job.status === 'complete' || job.status === 'error') { clearInterval(timer); res.end(); }
  };

  tick();
  timer = setInterval(tick, 600);
  req.on('close', () => { closed = true; clearInterval(timer); });
});

// ── Schedule ──────────────────────────────────────────────────────────────────

app.get('/api/schedule', (req, res) => res.json(loadSchedule()));

app.delete('/api/schedule/:filename', (req, res) => {
  const filename = decodeURIComponent(req.params.filename);
  if (schedulingInProgress.has(filename))
    return res.status(409).json({ error: 'Upload already in progress' });
  const schedule = loadSchedule();
  delete schedule[filename];
  saveJSON(SCHEDULE_FILE, schedule);
  res.json({ ok: true });
});

// ── Cross-linking ─────────────────────────────────────────────────────────────

async function checkAndCrossLink(batchId) {
  if (crossLinkInProgress.has(batchId)) return;

  const schedule = loadSchedule();
  const batch = Object.entries(schedule).filter(([, e]) => e.batchId === batchId);
  if (!batch.length) return;

  // Already done or not all clips finished yet
  if (batch.some(([, e]) => e.crossLinked)) return;
  if (!batch.every(([, e]) => e.status === 'done' && e.videoId)) return;

  crossLinkInProgress.add(batchId);
  console.log(`[cross-link] All clips done for batch "${batchId}" — updating descriptions`);
  try {
    await crossLinkBatch(batchId, batch);
    markQueueBatchDone(batchId);
    scheduleSyncToGitHub(`cross-linked ${batchId}`);
  } finally {
    crossLinkInProgress.delete(batchId);
  }
}

async function crossLinkBatch(batchId, batch) {
  const accountId = batch.find(([, e]) => e.accountId)?.[1]?.accountId
    || accountIdForChannel(normalizeChannel(batch[0]?.[1]?.channel));
  let client;
  try { client = getAuthedClient(accountId); } catch (e) {
    console.error(`[cross-link] Cannot get auth client: ${e.message}`);
    return;
  }
  const youtube = google.youtube({ version: 'v3', auth: client });

  // Sort by clipIndex so the list reads Clip 1, 2, 3…
  const sorted = [...batch].sort((a, b) => (a[1].clipIndex || 0) - (b[1].clipIndex || 0));

  const seriesLines = sorted
    .map(([, e]) => `Clip ${e.clipIndex}: https://www.youtube.com/shorts/${e.videoId}`)
    .join('\n');
  const seriesFooter = `\n\n--- Watch the full series ---\n${seriesLines}`;

  for (const [filename, entry] of sorted) {
    try {
      const { data } = await youtube.videos.list({ part: ['snippet'], id: [entry.videoId] });
      const video = data.items?.[0];
      if (!video) { console.log(`[cross-link] Not found on YouTube: ${entry.videoId}`); continue; }

      const snippet = video.snippet;
      // Guard against running twice if the footer is already there
      if ((snippet.description || '').includes('--- Watch the full series ---')) {
        const s = loadSchedule();
        if (s[filename]) { s[filename].crossLinked = true; saveJSON(SCHEDULE_FILE, s); }
        continue;
      }

      await youtube.videos.update({
        part: ['snippet'],
        requestBody: {
          id: entry.videoId,
          snippet: {
            title: snippet.title,
            description: (snippet.description || '') + seriesFooter,
            categoryId: snippet.categoryId || '22',
            ...(snippet.tags      && { tags: snippet.tags }),
            ...(snippet.defaultLanguage && { defaultLanguage: snippet.defaultLanguage }),
          },
        },
      });

      const s = loadSchedule();
      if (s[filename]) { s[filename].crossLinked = true; saveJSON(SCHEDULE_FILE, s); }
      console.log(`[cross-link] Updated clip ${entry.clipIndex}: ${entry.videoId}`);
    } catch (err) {
      if (err.code === 403 || err.message?.includes('insufficientPermissions')) {
        console.error('[cross-link] Insufficient scope — disconnect and reconnect YouTube to grant update permission');
        break;
      }
      console.error(`[cross-link] Failed for ${filename}: ${err.message}`);
    }
  }
}

// ── Buffer helpers ────────────────────────────────────────────────────────────

// Read BUFFER_ACCESS_TOKEN from .env at call time so the server doesn't need
// a restart after the user fills in their token.
function readBufferToken() {
  try {
    const envPath = path.join(CONTENTOS_DIR, '.env');
    if (!fs.existsSync(envPath)) return '';
    for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
      const m = line.trim().match(/^BUFFER_ACCESS_TOKEN=(.+)$/);
      if (m) return m[1].trim().replace(/^['"]|['"]$/g, '');
    }
  } catch (_) { }
  return '';
}

function buildBufferCaption(title) {
  const clean = title.split(' ').filter(w => !w.startsWith('#')).join(' ');
  return `${clean}\n\n#Reels #Instagram #FYP #Viral`;
}

function buildTikTokCaption(title) {
  const clean = title.split(' ').filter(w => !w.startsWith('#')).join(' ');
  return `${clean}\n\n#TikTok #FYP #Viral`;
}

// platform: 'instagram' | 'tiktok' — routed to musichub29_ on Buffer, which is
// podcast-only, so callers must gate TikTok posts on channel === 'podcast'.
function doBufferPost(filePath, filename, caption, platform = 'instagram') {
  return new Promise((resolve, reject) => {
    const proc = spawn(VENV_PYTHON, [BUFFER_POSTER, filePath, caption, '--platform', platform]);
    let out = '', err = '';
    proc.stdout.on('data', d => { const t = d.toString(); out += t; process.stdout.write(t); });
    proc.stderr.on('data', d => { err += d.toString(); process.stderr.write(d); });
    proc.on('close', code => {
      const lines = out.trim().split('\n').reverse();
      const jsonLine = lines.find(l => l.trim().startsWith('{'));
      try {
        const result = jsonLine ? JSON.parse(jsonLine) : null;
        if (result?.ok) resolve(result);
        else reject(new Error(result?.error || err.trim() || `exit ${code}`));
      } catch (_) {
        reject(new Error(err.trim() || out.trim() || `exit ${code}`));
      }
    });
  });
}

// ── Daily performance collector ────────────────────────────────────────────
// Runs collect_performance.py at most once per calendar day (gated via
// performance-state.json so a restart mid-day can't re-trigger it), piped
// into performance.log so failures/skips are visible without digging
// through the dashboard's own stdout.

function todayLocalDateString() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function runPerformanceCollector() {
  return new Promise((resolve, reject) => {
    const logStream = fs.createWriteStream(PERFORMANCE_LOG_FILE, { flags: 'a' });
    const proc = spawn(VENV_PYTHON, [PERFORMANCE_COLLECTOR]);
    proc.stdout.pipe(logStream, { end: false });
    proc.stderr.pipe(logStream, { end: false });
    proc.stdout.on('data', d => process.stdout.write(d));
    proc.stderr.on('data', d => process.stderr.write(d));
    proc.on('close', code => {
      logStream.end();
      if (code === 0) resolve();
      else reject(new Error(`collect_performance.py exited with code ${code}`));
    });
    proc.on('error', err => { logStream.end(); reject(err); });
  });
}

let performanceCollectorRunning = false;

function maybeRunPerformanceCollector() {
  if (performanceCollectorRunning) return;
  const today = todayLocalDateString();
  const state = loadJSON(PERFORMANCE_STATE_FILE, {});
  if (state.lastCollectedDate === today) return;

  performanceCollectorRunning = true;
  saveJSON(PERFORMANCE_STATE_FILE, { ...state, lastCollectedDate: today });
  console.log(`[performance] Running daily collector for ${today}…`);

  runPerformanceCollector()
    .then(() => console.log(`[performance] Collector finished for ${today}`))
    .catch(err => console.error(`[performance] Collector failed: ${err.message}`))
    .finally(() => { performanceCollectorRunning = false; });
}

// ── Background scheduler ──────────────────────────────────────────────────────

async function runScheduler() {
  const schedule = loadSchedule();
  const now = new Date();

  const anyConnected = getYoutubeAccounts().some(a => fs.existsSync(tokensFileFor(a.id)));
  if (!anyConnected) return; // no connected YouTube account yet

  for (const [filename, entry] of Object.entries(schedule)) {
    if (entry.status !== 'pending') continue;
    if (schedulingInProgress.has(filename)) continue;
    if (new Date(entry.scheduledAt) > now) continue;

    // Route to the account for this clip's channel (podcast/football).
    // Entries created before multi-account support won't have a channel —
    // normalizeChannel() defaults those to "podcast".
    const accountId = entry.accountId || accountIdForChannel(normalizeChannel(entry.channel));
    if (!fs.existsSync(tokensFileFor(accountId))) continue; // chosen account got disconnected — skip until reconnected

    const filePath = resolveClipPath(filename);
    if (!fs.existsSync(filePath)) {
      entry.status = 'failed';
      entry.error = 'File not found';
      saveJSON(SCHEDULE_FILE, loadSchedule()); // reload to avoid stomping concurrent writes
      const s = loadSchedule();
      s[filename] = { ...s[filename], status: 'failed', error: 'File not found' };
      saveJSON(SCHEDULE_FILE, s);
      continue;
    }

    schedulingInProgress.add(filename);
    console.log(`[scheduler] Starting upload: ${filename} (account: ${accountId})`);

    // Mark as uploading so the dashboard reflects it immediately
    const s0 = loadSchedule();
    if (s0[filename]) { s0[filename].status = 'uploading'; saveJSON(SCHEDULE_FILE, s0); }

    const jobId = `sched-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    uploadJobs.set(jobId, { status: 'starting', percent: 0, filename });

    doUpload(jobId, filePath, filename, {
      title: entry.title || path.basename(filename, path.extname(filename)).replace(/_/g, ' '),
      description: entry.description || '',
      visibility: entry.visibility || 'public',
      accountId,
    }).then(async () => {
      const job = uploadJobs.get(jobId);
      const finalStatus = job?.status === 'complete' ? 'done' : 'failed';
      const s = loadSchedule();
      if (s[filename]) {
        s[filename].status = finalStatus;
        if (job?.videoId) s[filename].videoId = job.videoId;
        if (job?.error)   s[filename].error   = job.error;
        saveJSON(SCHEDULE_FILE, s);
      }
      console.log(`[scheduler] ${filename}: ${finalStatus}`);
      scheduleSyncToGitHub(`${filename} ${finalStatus}`);

      // When a clip succeeds, check if the whole batch is done and cross-link
      if (finalStatus === 'done' && entry.batchId) {
        await checkAndCrossLink(entry.batchId);
      }
    }).finally(() => {
      schedulingInProgress.delete(filename);
    });
  }

  // ── Buffer auto-post (Instagram + TikTok) ─────────────────────────────────
  // Fires on the same tick as the YouTube upload: entry.status === 'pending'
  // means the clip is due now (same scheduledAt gate below). Instagram posts
  // for every channel; TikTok (musichub29_) is podcast-only, so football
  // clips never reach it. The two platforms post independently — one failing
  // doesn't block or retry the other — and track status under separate keys
  // (bufferStatus vs tiktokBufferStatus).
  const bufferToken = readBufferToken();
  if (bufferToken) {
    for (const [filename, entry] of Object.entries(schedule)) {
      if (entry.status !== 'pending') continue;      // same trigger as YouTube
      if (new Date(entry.scheduledAt) > now) continue;

      const filePath = resolveClipPath(filename);
      const channel  = normalizeChannel(entry.channel);
      const title    = entry.title || path.basename(filename, path.extname(filename)).replace(/_/g, ' ');

      // ── Instagram ──
      if (!['uploading', 'done', 'failed'].includes(entry.bufferStatus) && !bufferInProgress.has(filename)) {
        if (!fs.existsSync(filePath)) {
          const s = loadSchedule();
          if (s[filename]) {
            s[filename].bufferStatus = 'failed';
            s[filename].bufferError  = 'File not found';
            saveJSON(SCHEDULE_FILE, s);
          }
        } else {
          bufferInProgress.add(filename);
          const caption = buildBufferCaption(title);

          const s0 = loadSchedule();
          if (s0[filename]) { s0[filename].bufferStatus = 'uploading'; saveJSON(SCHEDULE_FILE, s0); }
          console.log(`[buffer:instagram] Posting: ${filename}`);

          doBufferPost(filePath, filename, caption, 'instagram')
            .then((result) => {
              const s = loadSchedule();
              if (s[filename]) {
                s[filename].bufferStatus = 'done';
                if (result?.updateId) s[filename].bufferPostId = result.updateId;
                saveJSON(SCHEDULE_FILE, s);
              }
              console.log(`[buffer:instagram] Done: ${filename}`);
              scheduleSyncToGitHub(`buffer done ${filename}`);
            })
            .catch(err => {
              console.error(`[buffer:instagram] Failed ${filename}: ${err.message}`);
              const s = loadSchedule();
              if (s[filename]) {
                s[filename].bufferStatus = 'failed';
                s[filename].bufferError  = err.message.slice(0, 200);
                saveJSON(SCHEDULE_FILE, s);
              }
              scheduleSyncToGitHub(`buffer failed ${filename}`);
            })
            .finally(() => bufferInProgress.delete(filename));
        }
      }

      // ── TikTok (podcast only) ──
      if (channel === 'podcast'
          && !['uploading', 'done', 'failed'].includes(entry.tiktokBufferStatus)
          && !tiktokBufferInProgress.has(filename)) {
        if (!fs.existsSync(filePath)) {
          const s = loadSchedule();
          if (s[filename]) {
            s[filename].tiktokBufferStatus = 'failed';
            s[filename].tiktokBufferError  = 'File not found';
            saveJSON(SCHEDULE_FILE, s);
          }
        } else {
          tiktokBufferInProgress.add(filename);
          const caption = buildTikTokCaption(title);

          const s0 = loadSchedule();
          if (s0[filename]) { s0[filename].tiktokBufferStatus = 'uploading'; saveJSON(SCHEDULE_FILE, s0); }
          console.log(`[buffer:tiktok] Posting: ${filename}`);

          doBufferPost(filePath, filename, caption, 'tiktok')
            .then((result) => {
              const s = loadSchedule();
              if (s[filename]) {
                s[filename].tiktokBufferStatus = 'done';
                if (result?.updateId) s[filename].tiktokBufferPostId = result.updateId;
                saveJSON(SCHEDULE_FILE, s);
              }
              console.log(`[buffer:tiktok] Done: ${filename}`);
              scheduleSyncToGitHub(`tiktok buffer done ${filename}`);
            })
            .catch(err => {
              console.error(`[buffer:tiktok] Failed ${filename}: ${err.message}`);
              const s = loadSchedule();
              if (s[filename]) {
                s[filename].tiktokBufferStatus = 'failed';
                s[filename].tiktokBufferError  = err.message.slice(0, 200);
                saveJSON(SCHEDULE_FILE, s);
              }
              scheduleSyncToGitHub(`tiktok buffer failed ${filename}`);
            })
            .finally(() => tiktokBufferInProgress.delete(filename));
        }
      }
    }
  }

  maybeRunPerformanceCollector();
}

// Check 5 s after startup then every 60 s
setTimeout(runScheduler, 5000);
setInterval(runScheduler, 60 * 1000);

// Start queue processor — picks up any pending items
setTimeout(processQueue, 3000);

// ── Start ─────────────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  const local = `http://localhost:${PORT}`;
  const pub   = PUBLIC_URL !== local ? `\n  Public URL  → ${PUBLIC_URL}` : '';
  console.log(`\n  ContentOS Dashboard → ${local}${pub}\n`);
});
