import { checkCardAuthenticity, type AuthResult, type SetInfo } from './authEngine';
import sets from './sets.json';

export type BackType = 'japanese' | 'english-style';

export interface CardJson {
  setCode: string;
  rarity: string;
  number: string;
  backType: BackType;
  regulation?: string;
  language?: string;
  matchConfidence?: number;
  auth?: AuthResult;
}

export function runAuthCheck(cardJson: CardJson) {
  const setInfo = (sets as SetInfo[]).find(s => s.setCode === cardJson.setCode) || null;

  return checkCardAuthenticity(
    {
      setCode: cardJson.setCode,
      rarity: cardJson.rarity,
      number: cardJson.number,
      backType: cardJson.backType
    },
    setInfo
  );
}

export function onScanComplete(cardJson: CardJson) {
  const auth = runAuthCheck(cardJson);
  cardJson.auth = auth;
  return auth;
}

export function onCheckAuthenticityClick(cardJson: CardJson) {
  const auth = runAuthCheck(cardJson);
  cardJson.auth = auth;
  return auth;
}
