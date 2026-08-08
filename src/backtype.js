export function getBackType(backTypeSelectEl) {
  return (backTypeSelectEl && backTypeSelectEl.value) || null;
}

export function normalizeBackType(value) {
  if (value === 'japanese') return 'japanese';
  if (value === 'english-style') return 'english-style';
  return 'english-style';
}
