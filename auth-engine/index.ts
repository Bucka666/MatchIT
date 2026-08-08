import sets from './sets.json';
import { checkCardAuthenticity } from './authEngine';

type BackType = 'japanese' | 'english-style';

interface SetInfo {
  setCode: string;
  name: string;
  country: string;
  productType: 'booster' | 'starter';
  expectedBack: BackType;
}

function findSetInfo(setCode: string): SetInfo | null {
  return (sets as SetInfo[]).find(s => s.setCode === setCode) || null;
}

const exampleCard = {
  setCode: 'S11',
  rarity: 'RRR',
  number: '030/100',
  backType: 'english-style' as BackType
};

const setInfo = findSetInfo(exampleCard.setCode);
const result = checkCardAuthenticity(exampleCard, setInfo);

console.log('Auth result:', result);
