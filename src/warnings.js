export function mapWarningLabel(code) {
  switch (code) {
    case 'back_mismatch': return 'Back mismatch';
    case 'number_out_of_range': return 'Number out of range';
    case 'product_mismatch': return 'Product mismatch';
    case 'unknown_set': return 'Unknown set';
    case 'special_product': return 'Special product';
    case 'rarity_mismatch': return 'Rarity mismatch';
    case 'number_mismatch': return 'Number mismatch';
    default: return String(code || 'Warning');
  }
}

export function renderWarnings(warnings, container) {
  if (!container) return;
  container.innerHTML = '';
  const list = Array.isArray(warnings) ? warnings : [];
  if (!list.length) {
    container.innerHTML = '<span class="warning">No major issues</span>';
    return;
  }
  list.forEach((code) => {
    const chip = document.createElement('span');
    chip.className = 'warning';
    chip.textContent = mapWarningLabel(code);
    container.appendChild(chip);
  });
}
