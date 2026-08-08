import importlib.util
import pathlib
import unittest


APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("app_under_test", APP_PATH)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class AuthResultTests(unittest.TestCase):
    def test_custom_set_is_reported_as_custom_non_official(self):
        result = app._build_auth_result_for_result(
            {"profile": {"set_name": "GrailSweep Custom Set"}},
            card_payload={
                "setCode": "CUSTOM-GS-001",
                "rarity": "R",
                "number": "1",
                "backType": "english-style",
                "confidence": 0.9,
            },
        )
        self.assertEqual(result["status"], "custom_non_official")
        self.assertEqual(result["display_status"], "Custom / non-official")

    def test_invalid_rarity_is_reported_as_counterfeit(self):
        result = app._build_auth_result_for_result(
            {"profile": {"set_name": "Scarlet & Violet Base"}},
            card_payload={
                "setCode": "EN-SV-BASE",
                "rarity": "ZZ",
                "number": "1",
                "backType": "english-style",
                "confidence": 0.9,
            },
        )
        self.assertEqual(result["status"], "counterfeit")
        self.assertEqual(result["display_status"], "Likely counterfeit")

    def test_english_expansion_set_is_recognized_as_official(self):
        result = app._build_auth_result_for_result(
            {"profile": {"set_name": "Sword & Shield Base"}},
            card_payload={
                "setCode": "SWSH1",
                "rarity": "RR",
                "number": "1",
                "backType": "english-style",
                "confidence": 0.9,
            },
        )
        self.assertEqual(result["status"], "official_booster")
        self.assertEqual(result["display_status"], "Official booster")

    def test_normalized_set_code_is_resolved_for_japanese_booster(self):
        result = app._build_auth_result_for_result(
            {"profile": {}},
            card_payload={
                "setCode": "svl",
                "rarity": "RR",
                "number": "1",
                "backType": "japanese",
                "confidence": 0.9,
            },
        )
        self.assertEqual(result["status"], "official_booster")
        self.assertEqual(result["display_status"], "Official booster")

    def test_special_product_is_flagged_for_review(self):
        result = app._build_auth_result_for_result(
            {"profile": {}},
            card_payload={
                "setCode": "ETB-SP",
                "rarity": "R",
                "number": "1",
                "backType": "english-style",
                "confidence": 0.9,
            },
        )
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["needsReview"])

    def test_recent_english_booster_set_is_recognized(self):
        result = app._build_auth_result_for_result(
            {"profile": {}},
            card_payload={
                "setCode": "OBF",
                "rarity": "RR",
                "number": "1",
                "backType": "english-style",
                "confidence": 0.9,
            },
        )
        self.assertEqual(result["status"], "official_booster")
        self.assertEqual(result["display_status"], "Official booster")

    def test_recent_pokemon_set_family_codes_are_recognized(self):
        for set_code in ["SV8", "SV9", "SV10", "PAR", "TEF", "TWM", "PAL"]:
            with self.subTest(set_code=set_code):
                result = app._build_auth_result_for_result(
                    {"profile": {}},
                    card_payload={
                        "setCode": set_code,
                        "rarity": "RR",
                        "number": "1",
                        "backType": "english-style",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(result["status"], "official_booster")
                self.assertEqual(result["display_status"], "Official booster")

    def test_result_payload_includes_ui_metadata(self):
        result = app._build_auth_result_for_result(
            {"profile": {"set_name": "Sword & Shield Base"}},
            card_payload={
                "setCode": "SWSH1",
                "rarity": "RR",
                "number": "1",
                "backType": "english-style",
                "confidence": 0.9,
            },
        )
        self.assertEqual(result["setInfo"]["name"], "Sword & Shield Base")
        self.assertEqual(result["setInfo"]["setCode"], "SWSH1")
        self.assertEqual(result["card"]["back"], "english-style")
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
