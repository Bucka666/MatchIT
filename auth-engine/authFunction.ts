import { checkCardAuthenticity, type SetInfo } from './authEngine';
import sets from './sets.json';

type BackType = 'japanese' | 'english-style';

interface CardJson {
  setCode: string;
  rarity: string;
  number: string;
  backType: BackType;
}

function findSetInfo(setCode: string): SetInfo | null {
  return (sets as SetInfo[]).find(s => s.setCode === setCode) || null;
}

export const authCheck = (req: any, res: any) => {
  if (req.method !== 'POST') {
    res.status(405).send('Use POST');
    return;
  }

  const body = req.body as CardJson;
  const setInfo = findSetInfo(body.setCode);
  const result = checkCardAuthenticity(
    {
      setCode: body.setCode,
      rarity: body.rarity,
      number: body.number,
      backType: body.backType
    },
    setInfo
  );

  res.json(result);
};
