export type BackType = 'japanese' | 'english-style' | 'unknown';

export interface FrontGuess {
  setCode?: string;
  rarity?: string;
  number?: string;
}

export async function classifyBack(_image: Buffer): Promise<BackType> {
  // TODO: Replace with CLIP/DiVo model or a tiny CNN later.
  return 'unknown';
}

export async function classifyFront(_image: Buffer): Promise<FrontGuess> {
  // TODO: plug in OCR + CLIP + DiVo fusion later.
  return {};
}

export async function processScan(image: Buffer) {
  const backType = await classifyBack(image);
  const frontGuess = await classifyFront(image);

  const cardJson = {
    setCode: frontGuess.setCode ?? 'S11',
    rarity: frontGuess.rarity ?? 'RRR',
    number: frontGuess.number ?? '030/100',
    backType
  };

  return cardJson;
}
