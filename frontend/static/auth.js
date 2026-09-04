/**
 * Shared utilities for all auth pages (login, register, verify, forgot-password).
 * No external dependencies — plain ES2020.
 */
'use strict';

// ── Banner helpers ────────────────────────────────────────────────────────
function showBanner(el, message, type) {
  // type: 'success' | 'error' | 'warning'
  el.textContent = message;
  el.className = 'auth-banner auth-banner--' + (type || 'error');
  el.classList.remove('hidden');
  el.setAttribute('role', 'alert');
}

function clearBanner(el) {
  if (!el) return;
  el.textContent = '';
  el.className = 'auth-banner hidden';
}

// ── Button loading state ──────────────────────────────────────────────────
function setLoading(btn, loading) {
  if (!btn) return;
  const label   = btn.querySelector('.btn-label');
  const spinner = btn.querySelector('.btn-spinner');
  btn.disabled = loading;
  if (label)   label.classList.toggle('hidden', loading);
  if (spinner) spinner.classList.toggle('hidden', !loading);
}

// ── Password toggle visibility ────────────────────────────────────────────
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.toggle-pw');
  if (!btn) return;
  const targetId = btn.dataset.target;
  const input    = document.getElementById(targetId);
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
  btn.setAttribute('aria-pressed', input.type === 'text');
});

// ── Expose globally ───────────────────────────────────────────────────────
window.showBanner  = showBanner;
window.clearBanner = clearBanner;
window.setLoading  = setLoading;
