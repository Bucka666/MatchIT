export function updateConfidenceBar(score, auth, barEl, labelEl, pillEl) {
  const pct = Math.max(0, Math.min(100, Math.round((Number(score) || 0) * 100)));
  const status = (auth && auth.status) || 'unknown';
  let color = '#ff3b30';
  let label = 'Needs more signal';

  if (status === 'official_booster' || status === 'official_starter') {
    color = '#34c759';
    label = 'Looks official';
  } else if (status === 'counterfeit') {
    color = '#ff3b30';
    label = 'Needs caution';
  } else if (auth && auth.needsReview) {
    color = '#ffcc00';
    label = 'Needs review';
  } else if (pct >= 80) {
    color = '#34c759';
    label = 'Strong signal';
  } else if (pct >= 50) {
    color = '#ffcc00';
    label = 'Moderate';
  }

  if (barEl) {
    barEl.style.width = pct + '%';
    barEl.style.background = color;
  }
  if (labelEl) {
    labelEl.textContent = pct + '%';
  }
  if (pillEl) {
    pillEl.textContent = label;
    pillEl.style.background = status === 'counterfeit' ? 'rgba(255,59,48,0.16)' : (status === 'official_booster' || status === 'official_starter' ? 'rgba(52,199,89,0.16)' : (auth && auth.needsReview ? 'rgba(255,204,0,0.16)' : 'rgba(0,229,255,0.16)'));
    pillEl.style.color = status === 'counterfeit' ? '#ffb4ab' : (status === 'official_booster' || status === 'official_starter' ? '#94f2ab' : (auth && auth.needsReview ? '#ffd766' : '#bff7ff'));
  }
}
