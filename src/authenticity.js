function normalizeAuthPayload(auth = {}) {
  const base = auth || {};
  const setInfo = base.setInfo || {};
  const warnings = Array.isArray(base.warnings)
    ? base.warnings
    : (Array.isArray(base.flags) ? base.flags : (Array.isArray(base.alerts) ? base.alerts : []));
  const confidenceScore = typeof base.confidence === 'number'
    ? base.confidence
    : (typeof base.confidence_score === 'number' ? base.confidence_score : 0.5);

  return {
    setName: base.set_name || base.setName || setInfo.name || 'Unknown set',
    setCode: base.set_code || base.setCode || setInfo.setCode || '-',
    region: base.region || setInfo.region || 'Unknown',
    type: base.product_type || base.type || setInfo.type || 'Unknown',
    back: base.back_type || base.backType || (base.card && base.card.back) || 'english-style',
    confidence: confidenceScore,
    warnings,
    reason: base.reason || base.summary || base.message || 'No authenticity details available.',
    status: base.status || base.auth_status || 'unknown',
    needsReview: !!(base.needsReview || base.needs_review),
    signalStrength: base.signal_strength,
  };
}

export function buildAuthPayload(auth = {}, backType = null) {
  const normalized = normalizeAuthPayload(auth);
  const back = backType || normalized.back || 'english-style';

  return {
    setName: normalized.setName,
    setCode: normalized.setCode,
    region: normalized.region,
    type: normalized.type,
    back,
    confidence: normalized.confidence,
    warnings: normalized.warnings,
    reason: normalized.reason,
    status: normalized.status,
    needsReview: normalized.needsReview,
    signalStrength: normalized.signalStrength,
  };
}

export async function runAuthenticityPipeline(cardImageBlob) {
  if (!cardImageBlob) {
    return buildAuthPayload({
      status: 'unknown',
      confidence: 0.35,
      needsReview: true,
      reason: 'Authenticity review unavailable. Please try again.',
      set_name: 'Unknown',
      set_code: '-',
      region: 'Unknown',
      product_type: 'Unknown',
      back_type: 'english-style',
      warnings: ['Authenticity review unavailable'],
    });
  }

  const form = new FormData();
  form.append('image', cardImageBlob);

  const res = await fetch('/api/authenticate', {
    method: 'POST',
    body: form,
  });

  if (!res.ok) {
    throw new Error(`Authenticity request failed (${res.status})`);
  }

  const data = await res.json();
  return buildAuthPayload(data);
}
