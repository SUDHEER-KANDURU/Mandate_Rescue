/**
 * Profile panel logic for Mandate Rescue dashboard.
 * Handles: open/close, tab switching, view/edit profile,
 * change password, change email, notification preferences,
 * security events, test email, logout.
 */
'use strict';

(function () {

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  let _merchant       = null;
  let _activeTab      = 'profile';
  let _pendingNewEmail = '';

  // ---------------------------------------------------------------------------
  // DOM refs — resolved lazily so this script is safe before full page load
  // ---------------------------------------------------------------------------
  const $  = (id) => document.getElementById(id);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---------------------------------------------------------------------------
  // Panel open / close
  // ---------------------------------------------------------------------------
  document.addEventListener('click', (e) => {
    const openBtn = e.target.closest('#btn-open-profile');
    if (openBtn) { openPanel(); return; }

    const closeBtn = e.target.closest('#btn-close-profile');
    if (closeBtn) { closePanel(); return; }

    const overlay = e.target.closest('#profile-overlay');
    if (overlay) { closePanel(); return; }
  });

  function openPanel() {
    $('profile-panel').classList.add('open');
    $('profile-panel').setAttribute('aria-hidden', 'false');
    $('profile-overlay').classList.add('open');
    $('profile-overlay').setAttribute('aria-hidden', 'false');
    $('btn-open-profile').setAttribute('aria-expanded', 'true');
    loadProfile();
    if (_activeTab === 'security') loadSecurityEvents();
  }

  function closePanel() {
    $('profile-panel').classList.remove('open');
    $('profile-panel').setAttribute('aria-hidden', 'true');
    $('profile-overlay').classList.remove('open');
    $('profile-overlay').setAttribute('aria-hidden', 'true');
    $('btn-open-profile').setAttribute('aria-expanded', 'false');
  }

  // ---------------------------------------------------------------------------
  // Tab switching
  // ---------------------------------------------------------------------------
  document.addEventListener('click', (e) => {
    const tab = e.target.closest('[data-profile-tab]');
    if (!tab) return;
    switchTab(tab.dataset.profileTab);
  });

  function switchTab(name) {
    _activeTab = name;
    $$('[data-profile-tab]').forEach(t => {
      const active = t.dataset.profileTab === name;
      t.classList.toggle('active', active);
      t.setAttribute('aria-selected', active);
    });
    $$('.profile-tab-content').forEach(el => el.classList.add('hidden'));
    const target = $('ptab-' + name);
    if (target) target.classList.remove('hidden');
    if (name === 'security') loadSecurityEvents();
    if (name === 'notifications') loadNotifPrefs();
  }

  // ---------------------------------------------------------------------------
  // Load profile
  // ---------------------------------------------------------------------------
  async function loadProfile() {
    try {
      const resp = await fetch('/api/auth/me');
      if (!resp.ok) {
        if (resp.status === 401) { window.location.href = '/'; return; }
        return;
      }
      const body = await resp.json();
      if (!body.ok) return;
      _merchant = body.merchant;
      renderProfile(_merchant);
      updateAvatarBtn(_merchant);
    } catch (e) { /* ignore */ }
  }

  function renderProfile(m) {
    if (!m) return;
    setText('p-full-name', m.full_name || '—');
    setText('p-email', m.email || '—');
    const verEl = $('p-verified');
    if (verEl) {
      verEl.textContent = m.email_verified ? '✓ Verified' : '✗ Not verified';
      verEl.className   = 'profile-row-value ' + (m.email_verified ? 'badge-verified' : 'badge-unverified');
    }
    setText('p-phone', m.phone || '—');
    setText('p-created-at', m.created_at ? m.created_at.slice(0, 10) : '—');
    setText('p-biz-name', m.business_name || '—');
    setText('p-biz-type', m.business_type || '—');
    setText('p-website', m.business_website || '—');
    setText('p-city', (m.city ? m.city + (m.state_region ? ', ' + m.state_region : '') : '—'));
  }

  function updateAvatarBtn(m) {
    const el = $('profile-avatar-initials');
    if (!el) return;
    const initials = (m.full_name || '?').split(' ').slice(0, 2)
      .map(w => w[0]).join('').toUpperCase();
    el.textContent = initials || '?';
  }

  function setText(id, val) {
    const el = $(id);
    if (el) el.textContent = val;
  }

  // On page load try to set avatar from existing session
  (async function initAvatar() {
    try {
      const resp = await fetch('/api/auth/me');
      if (!resp.ok) return;
      const body = await resp.json();
      if (body.ok && body.merchant) {
        _merchant = body.merchant;
        updateAvatarBtn(body.merchant);
      }
    } catch (e) { /* not logged in */ }
  })();

  // ---------------------------------------------------------------------------
  // Edit profile
  // ---------------------------------------------------------------------------
  document.addEventListener('click', (e) => {
    if (e.target.closest('#btn-edit-profile')) startEditProfile();
    if (e.target.closest('#btn-cancel-edit'))  cancelEditProfile();
  });

  function startEditProfile() {
    if (!_merchant) return;
    const f = _merchant;
    setVal('pe-full-name',    f.full_name || '');
    setVal('pe-phone',        f.phone || '');
    setVal('pe-biz-name',     f.business_name || '');
    setVal('pe-biz-website',  f.business_website || '');
    setVal('pe-city',         f.city || '');
    setVal('pe-state',        f.state_region || '');
    $('profile-edit-section').classList.remove('hidden');
    $('profile-view-actions').classList.add('hidden');
  }

  function cancelEditProfile() {
    $('profile-edit-section').classList.add('hidden');
    $('profile-view-actions').classList.remove('hidden');
  }

  const editForm = $('profile-edit-form');
  if (editForm) {
    editForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('btn-save-profile');
      btn && (btn.disabled = true);
      const banner = $('profile-banner');
      clearBanner(banner);
      const payload = {
        full_name:        $('pe-full-name').value.trim(),
        phone:            $('pe-phone').value.trim(),
        business_name:    $('pe-biz-name').value.trim(),
        business_website: $('pe-biz-website').value.trim(),
        city:             $('pe-city').value.trim(),
        state_region:     $('pe-state').value.trim(),
      };
      try {
        const resp = await fetch('/api/profile', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const body = await resp.json();
        if (body.ok) {
          _merchant = body.merchant;
          renderProfile(_merchant);
          updateAvatarBtn(_merchant);
          cancelEditProfile();
          showBanner(banner, 'Profile updated.', 'success');
        } else {
          showBanner(banner, body.message || 'Update failed.', 'error');
        }
      } catch (err) {
        showBanner(banner, 'Network error.', 'error');
      }
      btn && (btn.disabled = false);
    });
  }

  // ---------------------------------------------------------------------------
  // Logout
  // ---------------------------------------------------------------------------
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#btn-logout-profile')) return;
    doLogout();
  });

  async function doLogout() {
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } finally {
      window.location.href = '/';
    }
  }

  // ---------------------------------------------------------------------------
  // Change password
  // ---------------------------------------------------------------------------
  document.addEventListener('click', async (e) => {
    if (!e.target.closest('#btn-request-pw-change')) return;
    const btn    = $('btn-request-pw-change');
    const banner = $('security-banner');
    clearBanner(banner);
    btn.disabled = true;
    try {
      const resp = await fetch('/api/profile/change-password/request', { method: 'POST' });
      const body = await resp.json();
      if (body.ok) {
        $('sec-pw-step1').classList.add('hidden');
        $('sec-pw-step2').classList.remove('hidden');
        showBanner(banner, body.message, 'success');
      } else {
        showBanner(banner, body.message || 'Error.', 'error');
      }
    } catch (err) {
      showBanner(banner, 'Network error.', 'error');
    }
    btn.disabled = false;
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#btn-cancel-pw-change')) return;
    $('sec-pw-step2').classList.add('hidden');
    $('sec-pw-step1').classList.remove('hidden');
    $('change-pw-form').reset();
    clearBanner($('security-banner'));
  });

  // OTP digits only
  const cpwOtpEl = $('cpw-otp');
  if (cpwOtpEl) cpwOtpEl.addEventListener('input', () => {
    cpwOtpEl.value = cpwOtpEl.value.replace(/\D/g, '').slice(0, 6);
  });

  const cpwForm = $('change-pw-form');
  if (cpwForm) {
    cpwForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn    = $('btn-confirm-pw-change');
      const banner = $('security-banner');
      clearBanner(banner);
      $('err-cpw-otp').textContent = '';
      $('err-cpw-new').textContent = '';
      const otp  = $('cpw-otp').value.trim();
      const pw   = $('cpw-new').value;
      const cpw  = $('cpw-confirm').value;
      if (otp.length !== 6) { $('err-cpw-otp').textContent = 'Enter the 6-digit code.'; return; }
      if (pw !== cpw)        { $('err-cpw-new').textContent = 'Passwords do not match.'; return; }
      btn.disabled = true;
      try {
        const resp = await fetch('/api/profile/change-password/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ otp, new_password: pw, confirm_password: cpw }),
        });
        const body = await resp.json();
        if (body.ok) {
          showBanner(banner, body.message, 'success');
          cpwForm.reset();
          $('sec-pw-step2').classList.add('hidden');
          $('sec-pw-step1').classList.remove('hidden');
        } else {
          showBanner(banner, body.message || 'Error.', 'error');
        }
      } catch (err) {
        showBanner(banner, 'Network error.', 'error');
      }
      btn.disabled = false;
    });
  }

  // ---------------------------------------------------------------------------
  // Change email
  // ---------------------------------------------------------------------------
  const ceForm1 = $('change-email-form1');
  if (ceForm1) {
    ceForm1.addEventListener('submit', async (e) => {
      e.preventDefault();
      const newEmail = $('ce-new-email').value.trim();
      const banner   = $('security-banner');
      clearBanner(banner);
      if (!newEmail) return;
      _pendingNewEmail = newEmail;
      try {
        const resp = await fetch('/api/profile/change-email/request', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ new_email: newEmail }),
        });
        const body = await resp.json();
        if (body.ok) {
          showBanner(banner, body.message, 'success');
          $('sec-email-step1').classList.add('hidden');
          $('sec-email-step2').classList.remove('hidden');
        } else {
          showBanner(banner, body.message || 'Error.', 'error');
        }
      } catch (err) {
        showBanner(banner, 'Network error.', 'error');
      }
    });
  }

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#btn-cancel-email-change')) return;
    $('sec-email-step2').classList.add('hidden');
    $('sec-email-step1').classList.remove('hidden');
    clearBanner($('security-banner'));
  });

  const ceOtpEl = $('ce-otp');
  if (ceOtpEl) ceOtpEl.addEventListener('input', () => {
    ceOtpEl.value = ceOtpEl.value.replace(/\D/g, '').slice(0, 6);
  });

  const ceForm2 = $('change-email-form2');
  if (ceForm2) {
    ceForm2.addEventListener('submit', async (e) => {
      e.preventDefault();
      const otp    = $('ce-otp').value.trim();
      const banner = $('security-banner');
      clearBanner(banner);
      if (otp.length !== 6) return;
      try {
        const resp = await fetch('/api/profile/change-email/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ new_email: _pendingNewEmail, otp }),
        });
        const body = await resp.json();
        if (body.ok) {
          showBanner(banner, body.message, 'success');
          _merchant = body.merchant;
          renderProfile(_merchant);
          updateAvatarBtn(_merchant);
          $('sec-email-step2').classList.add('hidden');
          $('sec-email-step1').classList.remove('hidden');
          ceForm2.reset();
        } else {
          showBanner(banner, body.message || 'Error.', 'error');
        }
      } catch (err) {
        showBanner(banner, 'Network error.', 'error');
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Security events
  // ---------------------------------------------------------------------------
  async function loadSecurityEvents() {
    const container = $('security-events-list');
    if (!container) return;
    try {
      const resp = await fetch('/api/profile/security-events');
      if (!resp.ok) return;
      const body = await resp.json();
      if (!body.ok || !body.events.length) {
        container.innerHTML = '<div style="font-size:12px;color:#475569">No events yet.</div>';
        return;
      }
      container.innerHTML = body.events.slice(0, 10).map(ev => `
        <div class="security-event-row">
          <div class="security-event-type">${_fmtEventType(ev.event_type)}</div>
          <div class="security-event-time">${ev.created_at ? ev.created_at.slice(0, 16).replace('T', ' ') + ' UTC' : ''}</div>
        </div>
      `).join('');
    } catch (e) {
      container.innerHTML = '<div style="font-size:12px;color:#475569">Could not load events.</div>';
    }
  }

  function _fmtEventType(t) {
    return (t || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  // ---------------------------------------------------------------------------
  // Test email
  // ---------------------------------------------------------------------------
  document.addEventListener('click', async (e) => {
    if (!e.target.closest('#btn-test-email')) return;
    const btn    = $('btn-test-email');
    const result = $('test-email-result');
    btn.disabled = true;
    if (result) result.textContent = 'Sending…';
    try {
      const resp = await fetch('/api/profile/send-test-email', { method: 'POST' });
      const body = await resp.json();
      if (result) {
        result.textContent = body.message || 'Done.';
        result.style.color = body.status === 'SENT' ? '#4ade80' : '#94a3b8';
      }
    } catch (err) {
      if (result) result.textContent = 'Network error.';
    }
    btn.disabled = false;
  });

  // ---------------------------------------------------------------------------
  // Notification preferences
  // ---------------------------------------------------------------------------
  async function loadNotifPrefs() {
    try {
      const resp = await fetch('/api/profile/notification-preferences');
      if (!resp.ok) return;
      const body = await resp.json();
      if (!body.ok) return;
      const p = body.preferences;
      setCheck('notif-recovery-escalations', p.recovery_escalations);
      setCheck('notif-anomaly-alerts',       p.anomaly_alerts);
      setCheck('notif-policy-recommendations', p.policy_recommendations);
      setCheck('notif-system-failures',      p.system_failures);
      setCheck('notif-weekly-digest',        p.weekly_digest);
    } catch (e) { /* ignore */ }
  }

  document.addEventListener('click', async (e) => {
    if (!e.target.closest('#btn-save-notif-prefs')) return;
    const btn    = $('btn-save-notif-prefs');
    const banner = $('notif-banner');
    clearBanner(banner);
    btn.disabled = true;
    const payload = {
      recovery_escalations:   $('notif-recovery-escalations').checked,
      anomaly_alerts:         $('notif-anomaly-alerts').checked,
      policy_recommendations: $('notif-policy-recommendations').checked,
      system_failures:        $('notif-system-failures').checked,
      weekly_digest:          $('notif-weekly-digest').checked,
    };
    try {
      const resp = await fetch('/api/profile/notification-preferences', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await resp.json();
      showBanner(banner, body.message || 'Saved.', body.ok ? 'success' : 'error');
    } catch (err) {
      showBanner(banner, 'Network error.', 'error');
    }
    btn.disabled = false;
  });

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function setVal(id, val) { const el = $(id); if (el) el.value = val; }
  function setCheck(id, val) { const el = $(id); if (el) el.checked = Boolean(val); }

})();
