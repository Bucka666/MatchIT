import { buildAuthPayload } from './authenticity.js';

function applyConfidenceStyling(pct, barEl) {
  if (!barEl) return;
  barEl.classList.remove('auth-green', 'auth-amber', 'auth-red');
  if (pct >= 85) {
    barEl.classList.add('auth-green');
  } else if (pct >= 50) {
    barEl.classList.add('auth-amber');
  } else {
    barEl.classList.add('auth-red');
  }
}

function getAuthenticityBadge(pct, flags = []) {
  if (pct >= 85 && (!flags || flags.length === 0)) return 'Authentic';
  if (pct >= 50) return 'Needs Review';
  return 'Suspect';
}

function classifyWarning(w) {
  const critical = ['counterfeit', 'fake', 'mismatch', 'tampered'];
  const caution = ['low_signal', 'uncertain', 'blurry'];
  const lower = String(w || '').toLowerCase();

  if (critical.some((c) => lower.includes(c))) return 'critical';
  if (caution.some((c) => lower.includes(c))) return 'caution';
  return 'info';
}

function detectBackMismatch(auth) {
  const expected = String(auth?.region || '').toLowerCase();
  const actual = String(auth?.back_type || auth?.back || '').toLowerCase();

  if (!expected || !actual) return null;
  if (expected === actual) return null;
  return `Back-type mismatch: expected ${expected.toUpperCase()}, got ${actual.toUpperCase()}`;
}

function renderWarnings(warnings, container) {
  if (!container) return;
  container.innerHTML = '';
  const list = Array.isArray(warnings) ? warnings : [];

  if (!list.length) {
    const item = document.createElement('div');
    item.className = 'auth-warning-item info';
    item.textContent = 'No major issues';
    container.appendChild(item);
    return;
  }

  list.forEach((warning) => {
    const item = document.createElement('div');
    item.className = 'auth-warning-item ' + classifyWarning(warning);
    item.textContent = warning;
    container.appendChild(item);
  });
}

export function renderOverlay(payload, elements) {
  const { setNameEl, setCodeEl, regionEl, typeEl, backEl, summaryEl, evidenceEl, warningsEl, confidenceBarEl, confidenceLabelEl, statusPillEl } = elements;
  const warnings = [...(Array.isArray(payload.flags) ? payload.flags : []), ...(Array.isArray(payload.warnings) ? payload.warnings : [])];
  const pct = Math.round((Number(payload.confidence) || 0) * 100);

  if (setNameEl) setNameEl.textContent = payload.setName || 'Unknown Set';
  if (setCodeEl) setCodeEl.textContent = payload.setCode ? `(${payload.setCode})` : '';
  if (regionEl) regionEl.textContent = payload.region || '-';
  if (typeEl) typeEl.textContent = payload.type || '-';
  if (backEl) backEl.textContent = payload.back || '-';
  if (summaryEl) summaryEl.textContent = payload.reason || 'No authenticity details available.';

  if (evidenceEl) {
    const chips = [];
    const status = payload.status || 'unknown';
    if (status === 'official_booster' || status === 'official_starter') {
      chips.push({ text: 'Official set match', tone: 'positive' });
    } else if (status === 'counterfeit') {
      chips.push({ text: 'Mismatch detected', tone: 'warning' });
    } else if (payload.needsReview) {
      chips.push({ text: 'Needs review', tone: 'warning' });
    } else {
      chips.push({ text: 'Review pending', tone: 'neutral' });
    }
    if (payload.back) {
      chips.push({ text: 'Back: ' + (payload.back === 'japanese' ? 'Japanese' : 'English'), tone: payload.back === 'japanese' ? 'neutral' : 'positive' });
    }
    if (payload.setCode && payload.setCode !== '-') {
      chips.push({ text: 'Set: ' + payload.setCode, tone: 'neutral' });
    }
    if (warnings.length) {
      chips.push({ text: warnings.length + ' caution point' + (warnings.length > 1 ? 's' : ''), tone: 'warning' });
    } else {
      chips.push({ text: 'No major issues', tone: 'positive' });
    }
    evidenceEl.innerHTML = chips.map((chip) => '<span class="chip is-' + chip.tone + '">' + chip.text + '</span>').join('');
  }

  const badgeEl = document.getElementById('auth-badge');
  if (badgeEl) badgeEl.textContent = getAuthenticityBadge(pct, warnings);
  if (warningsEl) {
    const warningList = [...warnings];
    const mismatch = detectBackMismatch(payload);
    if (mismatch) warningList.push(mismatch);
    renderWarnings(warningList, warningsEl);
  }

  if (confidenceBarEl) {
    confidenceBarEl.style.width = pct + '%';
    confidenceBarEl.textContent = pct + '%';
    applyConfidenceStyling(pct, confidenceBarEl);
  }
  if (confidenceLabelEl) confidenceLabelEl.textContent = pct + '%';
  if (statusPillEl) {
    const status = payload.status || 'unknown';
    statusPillEl.textContent = status === 'official_booster' || status === 'official_starter' ? 'Looks official' : (status === 'counterfeit' ? 'Needs caution' : (payload.needsReview ? 'Needs review' : 'Checking…'));
  }
}

export function showAuthLoading() {
  const overlayEl = document.getElementById('auth-result');
  if (overlayEl) {
    overlayEl.classList.add('visible');
    overlayEl.classList.remove('gs-hidden');
    overlayEl.classList.add('loading');
  }
}

export function hideAuthLoading() {
  const overlayEl = document.getElementById('auth-result');
  if (overlayEl) {
    overlayEl.classList.remove('loading');
  }
}

export function setAuthenticityLoading(isLoading) {
  if (isLoading) {
    showAuthLoading();
  } else {
    hideAuthLoading();
  }
}

export function renderAuthenticityOverlay(auth) {
  const payload = buildAuthPayload(auth);
  const setNameEl = document.getElementById('auth-set-name');
  const setCodeEl = document.getElementById('auth-set-code');
  const regionEl = document.getElementById('auth-region');
  const typeEl = document.getElementById('auth-type');
  const backEl = document.getElementById('auth-back');
  const summaryEl = document.getElementById('auth-summary');
  const evidenceEl = document.getElementById('auth-evidence');
  const warningsEl = document.getElementById('auth-warnings');
  const confidenceBarEl = document.getElementById('auth-confidence-bar');
  const confidenceLabelEl = document.getElementById('auth-confidence-label');
  const statusPillEl = document.getElementById('auth-status-pill');

  renderOverlay(payload, { setNameEl, setCodeEl, regionEl, typeEl, backEl, summaryEl, evidenceEl, warningsEl, confidenceBarEl, confidenceLabelEl, statusPillEl });

  const overlayEl = document.getElementById('auth-result');
  if (overlayEl) {
    overlayEl.classList.add('visible');
    overlayEl.classList.remove('gs-hidden');
    overlayEl.classList.remove('loading');
  }
}

export function showAuthResult(auth) {
  renderAuthenticityOverlay(auth);
}

export function hideAuthResult() {
  const overlayEl = document.getElementById('auth-result');
  if (overlayEl) {
    overlayEl.classList.remove('visible');
    overlayEl.classList.add('gs-hidden');
    overlayEl.classList.remove('loading');
  }
}
