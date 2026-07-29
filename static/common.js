// Shared helpers across all four pages: Input, Approve, History, Settings

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toast(msg, type = '') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = `toast show${type ? ' ' + type : ''}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 3500);
}

function highlightNav() {
  const page = window.location.pathname.replace(/^\//, '') || 'input';
  document.querySelectorAll('.nav-link').forEach(a => {
    a.classList.toggle('active', a.getAttribute('data-page') === page);
  });
}
document.addEventListener('DOMContentLoaded', highlightNav);

// ── Draft handoff between Input and Approve ─────────────────────────────────
// A "draft" is this month's in-progress newsletter: setup fields + blocks,
// and once generated, the AI copy too. Approve is the single source of truth
// for "what's pending" — once pushed to Brevo, the draft is cleared and Brevo
// itself becomes the record (surfaced on History).
// Backed by the DB (/api/draft) so it survives across browsers/devices —
// requires DATABASE_URL to be set; with no DB configured, save/clear are
// harmless no-ops and load always returns null (matches having no draft yet).
const DRAFT_API = '/api/draft';

async function loadDraft() {
  try {
    const res = await fetch(DRAFT_API);
    if (!res.ok) return null;
    const data = await res.json();
    return (data && Object.keys(data).length) ? data : null;
  } catch (e) {
    console.error('Failed to load draft:', e);
    return null;
  }
}

async function saveDraft(draft) {
  try {
    await fetch(DRAFT_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draft),
    });
  } catch (e) {
    console.error('Failed to save draft:', e);
  }
}

async function clearDraft() {
  try {
    await fetch(DRAFT_API, { method: 'DELETE' });
  } catch (e) {
    console.error('Failed to clear draft:', e);
  }
}
