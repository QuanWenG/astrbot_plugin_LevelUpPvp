import unittest

from services.battle_image_renderer import RENDERER_REVISION
from services.image_renderer import COLORS, _text_units, _wrap_text


class _FakeDraw:
    def textbbox(self, position, text, font):
        return (0, 0, len(text) * 10, 20)


class SharedImageRendererTests(unittest.TestCase):
    def test_light_theme_uses_white_page_and_readable_text(self):
        self.assertEqual((255, 255, 255), COLORS["page"])
        self.assertEqual((255, 255, 255), COLORS["page_top"])
        self.assertGreater(sum(COLORS["card"]), 700)
        self.assertLess(sum(COLORS["text"]), 180)
        self.assertGreater(
            sum(COLORS["card"]) - sum(COLORS["text"]),
            500,
        )

    def test_wrapping_preserves_emoji_units_and_blank_lines(self):
        draw = _FakeDraw()
        font = object()
        self.assertEqual(["甲", "❤️‍🔥", "乙", "⚔️"], _text_units("甲❤️‍🔥乙⚔️"))
        self.assertEqual([""], _wrap_text(draw, "", font, 30))
        self.assertEqual(["甲乙丙", "丁"], _wrap_text(draw, "甲乙丙丁", font, 30))

    def test_battle_report_revision_identifies_light_theme(self):
        self.assertEqual("astrbot-card-v3-light", RENDERER_REVISION)


if __name__ == "__main__":
    unittest.main()
