import json
import modal

app = modal.App("grailsweep-auth-engine")


def check_card(card, set_info):
    if not set_info:
        return {
            "status": "unknown",
            "reason": f"Set code {card['setCode']} not found"
        }

    booster = (
        set_info["productType"] == "booster"
        and card["rarity"] in ["AR", "SAR", "CHR", "RR", "RRR", "SSR", "UR"]
    )

    starter = set_info["productType"] == "starter"

    if booster:
        if card["backType"] == "japanese":
            return {
                "status": "official_booster",
                "reason": f"{set_info['name']} booster card with correct JP back"
            }
        return {
            "status": "custom_non_official",
            "reason": f"{set_info['name']} booster card with EN-style back"
        }

    if starter:
        if card["backType"] == "english-style":
            return {
                "status": "official_starter",
                "reason": f"{set_info['name']} starter card with correct EN back"
            }
        return {
            "status": "counterfeit",
            "reason": f"{set_info['name']} starter card with JP back"
        }

    return {
        "status": "unknown",
        "reason": "No matching rule"
    }


@app.function()
@modal.web_endpoint(method="POST")
def auth_check(request):
    card = request.json

    with open("sets.json", "r", encoding="utf-8") as f:
        sets = json.load(f)

    set_info = next((s for s in sets if s["setCode"] == card["setCode"]), None)
    return check_card(card, set_info)
