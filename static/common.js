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
const DRAFT_KEY = 'nlb_draft';

function loadDraft() {
  try {
    return JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null');
  } catch (e) {
    return null;
  }
}

function saveDraft(draft) {
  localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
}

function clearDraft() {
  localStorage.removeItem(DRAFT_KEY);
}
