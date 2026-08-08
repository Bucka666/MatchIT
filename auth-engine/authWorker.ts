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

export default {
  async fetch(request: Request): Promise<Response> {
    if (request.method !== 'POST') {
      return new Response('Use POST', { status: 405 });
    }

    const body = (await request.json()) as CardJson;
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

    return new Response(JSON.stringify(result), {
      headers: { 'Content-Type': 'application/json' }
    });
  }
};
