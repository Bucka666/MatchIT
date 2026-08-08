const assert = require('assert');
const { normaliseSetCode, getSetInfo, validateRegistry, validateProductRules, runAuthenticityPipeline } = require('./authEngine');

const normalised = normaliseSetCode(' svl ');
assert.strictEqual(normalised, 'SV1');

const sv1 = getSetInfo('sv1');
assert.ok(sv1, 'SV1 should resolve from the registry');
assert.strictEqual(sv1.setCode, 'SV1');

const obf = getSetInfo('OBF');
assert.ok(obf, 'OBF should resolve from the registry');
assert.strictEqual(obf.setCode, 'OBF');

const modernSet = getSetInfo('SV8');
assert.ok(modernSet, 'SV8 should resolve from the registry');
assert.strictEqual(modernSet.setCode, 'SV8');

const modernPrintedSet = getSetInfo('PAL');
assert.ok(modernPrintedSet, 'PAL should resolve from the registry');
assert.strictEqual(modernPrintedSet.setCode, 'PAL');

const registryErrors = validateRegistry();
assert.deepStrictEqual(registryErrors, []);

const productErrors = validateProductRules(
  { back: 'japanese', number: 12, rarityFamily: 'booster' },
  sv1
);
assert.deepStrictEqual(productErrors, []);

const etbSet = getSetInfo('ETB-SP');
assert.ok(etbSet, 'ETB set should resolve from the registry');
const etbErrors = validateProductRules(
  { back: 'english-style', number: 1, rarityFamily: 'booster' },
  etbSet
);
assert.ok(etbErrors.some(error => error.includes('ETB-exclusive')), 'ETB sets should reject booster rarity families');

const specialErrors = validateProductRules(
  { back: 'english-style', number: 150, rarityFamily: 'booster' },
  getSetInfo('CEL')
);
assert.ok(specialErrors.some(error => error.includes('Special/collection sets')), 'special sets should reject high booster numbering');

const pipelineResult = runAuthenticityPipeline({
  setCode: 'svl',
  back: 'japanese',
  number: 12,
  rarityFamily: 'booster'
});
assert.ok(pipelineResult.setInfo, 'pipeline should resolve a setInfo object');
assert.strictEqual(pipelineResult.isAuthentic, true);
assert.deepStrictEqual(pipelineResult.errors, []);

console.log('registry helper tests passed');
