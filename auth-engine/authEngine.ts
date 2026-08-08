import sets from './sets.json';

export type BackType = 'japanese' | 'english-style';

export type Status =
  | 'official_booster'
  | 'official_starter'
  | 'custom_non_official'
  | 'counterfeit'
  | 'unknown';

export interface CardInput {
  setCode: string;
  rarity: string;
  number: string;
  backType: BackType;
}

export interface SetInfo {
  setCode: string;
  name: string;
  country: string;
  productType: 'booster' | 'starter' | 'custom';
  expectedBack: BackType;
  allowedRarities?: string[];
  minNumber?: number;
  maxNumber?: number;
}

export interface AuthResult {
  status: Status;
  reason: string;
  confidence?: number;
  flags?: string[];
  needsReview?: boolean;
}

export interface RegistrySetInfo {
  setCode: string;
  name: string;
  region: 'EN' | 'JP';
  type: 'expansion' | 'subset' | 'special' | 'promo' | 'deck' | 'collection' | 'etb';
  expectedBack: BackType;
}

function toRegistrySetInfo(entry: Record<string, unknown>): RegistrySetInfo {
  const rawRegion = (entry.region as 'EN' | 'JP' | undefined) ?? (entry.country as 'EN' | 'JP' | undefined);
  const region = rawRegion === 'JP' ? 'JP' : 'EN';

  const rawType = (entry.type as RegistrySetInfo['type'] | undefined) ?? (entry.productType as string | undefined);
  const type = rawType === 'promo'
    ? 'promo'
    : rawType === 'subset'
      ? 'subset'
      : rawType === 'special'
        ? 'special'
        : rawType === 'deck'
          ? 'deck'
          : rawType === 'collection'
            ? 'collection'
            : rawType === 'etb'
              ? 'etb'
              : rawType === 'starter'
                ? 'deck'
                : 'expansion';

  return {
    setCode: String(entry.setCode ?? ''),
    name: String(entry.name ?? ''),
    region,
    type,
    expectedBack: (entry.expectedBack as BackType | undefined) ?? (region === 'JP' ? 'japanese' : 'english-style')
  };
}

const MASTER_REGISTRY_ENTRIES: RegistrySetInfo[] = [
  { setCode: 'SV1', name: 'Scarlet ex', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'SV2', name: 'Violet ex', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'SV3', name: 'Raging Surf', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'SV4', name: 'Ancient Roar', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'SV4a', name: 'Shiny Treasure ex', region: 'JP', type: 'special', expectedBack: 'japanese' },
  { setCode: 'CRI', name: 'Crimson Haze', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'SV-P', name: 'Scarlet & Violet Promo', region: 'JP', type: 'promo', expectedBack: 'japanese' },
  { setCode: 'S1H', name: 'Sword', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'S1W', name: 'Shield', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'S4a', name: 'Shiny Star V', region: 'JP', type: 'special', expectedBack: 'japanese' },
  { setCode: 'S-P', name: 'Sword & Shield Promo', region: 'JP', type: 'promo', expectedBack: 'japanese' },
  { setCode: 'SM1S', name: 'Collection Sun', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'SM1M', name: 'Collection Moon', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'SM-P', name: 'Sun & Moon Promo', region: 'JP', type: 'promo', expectedBack: 'japanese' },
  { setCode: 'XY1', name: 'Collection X', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'XY-P', name: 'XY Promo', region: 'JP', type: 'promo', expectedBack: 'japanese' },
  { setCode: 'BW1', name: 'Black Collection', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'BW-P', name: 'Black & White Promo', region: 'JP', type: 'promo', expectedBack: 'japanese' },
  { setCode: 'DP1', name: 'Space-Time Creation', region: 'JP', type: 'expansion', expectedBack: 'japanese' },
  { setCode: 'P-P', name: 'DP/Platinum/HGSS Promo', region: 'JP', type: 'promo', expectedBack: 'japanese' },
  { setCode: 'DRG', name: 'Dragon Vault', region: 'EN', type: 'special', expectedBack: 'english-style' },
  { setCode: 'DTK', name: 'Detective Pikachu', region: 'EN', type: 'special', expectedBack: 'english-style' },
  { setCode: 'CEL', name: 'Celebrations', region: 'EN', type: 'special', expectedBack: 'english-style' },
  { setCode: 'CRZ', name: 'Crown Zenith', region: 'EN', type: 'special', expectedBack: 'english-style' },
  { setCode: 'WOTC-P', name: 'Wizards Promo', region: 'EN', type: 'promo', expectedBack: 'english-style' },
  { setCode: 'NP', name: 'Nintendo Black Star Promo', region: 'EN', type: 'promo', expectedBack: 'english-style' },
  { setCode: 'SVP', name: 'Scarlet & Violet Promo', region: 'EN', type: 'promo', expectedBack: 'english-style' },
  { setCode: 'PBL', name: 'Mega Evolution — Pitch Black', region: 'EN', type: 'special', expectedBack: 'english-style' },
  { setCode: 'PO', name: 'Mega Evolution — Perfect Order', region: 'EN', type: 'special', expectedBack: 'english-style' },
  { setCode: 'ETB-SP', name: 'Elite Trainer Box Exclusive Series', region: 'EN', type: 'etb', expectedBack: 'english-style' }
];

export const SETS_REGISTRY: RegistrySetInfo[] = (() => {
  const merged: RegistrySetInfo[] = [];
  const seen = new Set<string>();

  for (const entry of [...MASTER_REGISTRY_ENTRIES, ...(sets as Record<string, unknown>[]).map(toRegistrySetInfo)]) {
    if (seen.has(entry.setCode)) continue;
    seen.add(entry.setCode);
    merged.push(entry);
  }

  return merged;
})();

export function normaliseSetCode(input: string | null | undefined): string {
  if (!input) return '';

  let code = input.toUpperCase().trim();
  code = code.replace(/[\s\-\/]/g, '');
  code = code.replace(/I/g, '1');
  code = code.replace(/O/g, '0');
  code = code.replace(/L$/g, '1');
  code = code.replace(/A$/g, 'a');

  return code;
}

export function getSetInfo(rawCode: string | null | undefined): RegistrySetInfo | null {
  const code = normaliseSetCode(rawCode);
  return SETS_REGISTRY.find(s => normaliseSetCode(s.setCode) === code) || null;
}

export function validateRegistry(): string[] {
  const seen = new Set<string>();
  const errors: string[] = [];

  for (const set of SETS_REGISTRY) {
    if (seen.has(set.setCode)) {
      errors.push(`Duplicate setCode: ${set.setCode}`);
    }
    seen.add(set.setCode);

    if (!['EN', 'JP'].includes(set.region)) {
      errors.push(`Invalid region: ${set.region} (${set.setCode})`);
    }

    if (!['expansion', 'subset', 'special', 'promo', 'deck', 'collection', 'etb'].includes(set.type)) {
      errors.push(`Invalid type: ${set.type} (${set.setCode})`);
    }

    if (!['english-style', 'japanese'].includes(set.expectedBack)) {
      errors.push(`Invalid expectedBack: ${set.expectedBack} (${set.setCode})`);
    }
  }

  return errors;
}

export function validateProductRules(
  card: { back: BackType; number?: number; rarityFamily?: string },
  setInfo: RegistrySetInfo | null
): string[] {
  const errors: string[] = [];

  if (!setInfo) {
    errors.push('Unknown setCode');
    return errors;
  }

  if (setInfo.region === 'JP' && card.back !== 'japanese') {
    errors.push('Back mismatch: JP set requires japanese back');
  }

  if (setInfo.region === 'EN' && card.back !== 'english-style') {
    errors.push('Back mismatch: EN set requires english-style back');
  }

  if (setInfo.type === 'promo' && (card.number ?? 0) > 999) {
    errors.push('Promo sets cannot have booster-style numbering');
  }

  if (setInfo.type === 'deck' && card.rarityFamily === 'booster') {
    errors.push('Deck sets should not use booster rarity families');
  }

  if ((setInfo.type === 'special' || setInfo.type === 'collection') && (card.number ?? 0) > 100 && card.rarityFamily === 'booster') {
    errors.push('Special/collection sets should not use booster-style numbering');
  }

  if (setInfo.type === 'etb' && card.rarityFamily === 'booster') {
    errors.push('ETB-exclusive sets cannot use booster rarity families');
  }

  return errors;
}

export function runAuthenticityPipeline(card: {
  setCode: string;
  back: BackType;
  number?: number;
  rarityFamily?: string;
}): {
  setInfo: RegistrySetInfo | null;
  errors: string[];
  isAuthentic: boolean;
} {
  const setInfo = getSetInfo(card.setCode);
  const errors = validateProductRules(card, setInfo);

  return {
    setInfo,
    errors,
    isAuthentic: errors.length === 0
  };
}

function isNumberInRange(number: string, setInfo: SetInfo): boolean {
  const [numPart] = number.split('/');
  const n = parseInt(numPart, 10);
  if (Number.isNaN(n)) return false;
  return n >= (setInfo.minNumber ?? 1) && n <= (setInfo.maxNumber ?? Number.MAX_SAFE_INTEGER);
}

function isRarityAllowed(rarity: string, setInfo: SetInfo): boolean {
  return !setInfo.allowedRarities || setInfo.allowedRarities.includes(rarity);
}

export function checkCardAuthenticity(
  card: CardInput,
  setInfo: SetInfo | null
): AuthResult {
  const flags: string[] = [];
  let confidence = 1.0;
  let needsReview = false;

  if (!setInfo) {
    return {
      status: 'unknown',
      reason: `Set code ${card.setCode} not found in official database`,
      confidence: 0.2,
      flags: ['set_mismatch'],
      needsReview: true
    };
  }

  if (!isRarityAllowed(card.rarity, setInfo)) {
    flags.push('rarity_mismatch');
    confidence -= 0.4;
  }

  if (!isNumberInRange(card.number, setInfo)) {
    flags.push('number_mismatch');
    confidence -= 0.4;
  }

  if (card.backType !== setInfo.expectedBack) {
    flags.push('back_mismatch');
    confidence -= 0.3;
  }

  if (flags.length > 0) {
    needsReview = true;
  }

  if (flags.length > 0) {
    return {
      status: 'counterfeit',
      reason: 'One or more mismatches detected',
      confidence: Math.max(0, confidence),
      flags,
      needsReview
    };
  }

  if (setInfo.productType === 'custom') {
    return {
      status: 'custom_non_official',
      reason: `Custom set detected (${setInfo.name})`,
      confidence: 0.95,
      flags: [],
      needsReview: false
    };
  }

  const isBooster =
    setInfo.productType === 'booster' &&
    ['AR', 'SAR', 'CHR', 'RR', 'RRR', 'SSR', 'UR'].includes(card.rarity);

  const isStarter = setInfo.productType === 'starter';

  if (isBooster) {
    if (card.backType === 'japanese') {
      return {
        status: 'official_booster',
        reason: `Booster-set ${setInfo.name} card with correct Japanese back`,
        confidence: 0.95,
        flags: [],
        needsReview: false
      };
    }

    if (card.backType === 'english-style' && setInfo.country === 'EN') {
      return {
        status: 'official_booster',
        reason: `Booster-set ${setInfo.name} card with correct English-style back`,
        confidence: 0.95,
        flags: [],
        needsReview: false
      };
    }

    return {
      status: 'custom_non_official',
      reason: `Booster-set ${setInfo.name} card with English-style back (commonly custom/non-official)`,
      confidence: 0.8,
      flags: ['back_mismatch'],
      needsReview: true
    };
  }

  if (isStarter) {
    if (card.backType === 'english-style') {
      return {
        status: 'official_starter',
        reason: `Starter-deck ${setInfo.name} card with correct English-style back`,
        confidence: 0.95,
        flags: [],
        needsReview: false
      };
    } else {
      return {
        status: 'counterfeit',
        reason: `Starter-deck ${setInfo.name} card with Japanese back (unexpected)`,
        confidence: 0.7,
        flags: ['back_mismatch'],
        needsReview: true
      };
    }
  }

  return {
    status: 'unknown',
    reason: 'No matching rule for this combination',
    confidence: 0.5,
    flags: [],
    needsReview: false
  };
}
