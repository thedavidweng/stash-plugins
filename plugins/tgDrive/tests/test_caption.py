import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caption import utf16_len, sanitize_hashtag, build_scene_caption, MAX_CAPTION_UTF16


class TestCaption(unittest.TestCase):
    def test_utf16_len(self):
        self.assertEqual(utf16_len("hello"), 5)
        self.assertEqual(utf16_len("你好"), 2)
        # Surrogate pair: 𝄞 (U+1D11E) is 2 UTF-16 code units
        self.assertEqual(utf16_len("𝄞"), 2)

    def test_sanitize_hashtag(self):
        self.assertEqual(sanitize_hashtag("Alice Green"), "#Alice_Green")
        self.assertEqual(sanitize_hashtag("IPX-123"), "#IPX_123")
        self.assertEqual(sanitize_hashtag("Tokyo-Hot (n0123)"), "#Tokyo_Hot_n0123")
        self.assertEqual(sanitize_hashtag(""), "")
        self.assertEqual(sanitize_hashtag("   "), "")

    def test_build_scene_caption_normal(self):
        cap = build_scene_caption(
            title="A Beautiful Day",
            date="2026-05-10",
            studio="Prestige",
            performers=["Alice", "Bob"],
            code="ABC-001",
            tags=["4K", "Outdoor"],
        )
        self.assertIn("A Beautiful Day", cap)
        self.assertIn("#Prestige", cap)
        self.assertIn("#Alice", cap)
        self.assertIn("#Bob", cap)
        self.assertIn("#ABC_001", cap)
        self.assertIn("#4K", cap)
        self.assertLessEqual(utf16_len(cap), MAX_CAPTION_UTF16)

    def test_build_scene_caption_oversized_title(self):
        long_title = "Very Long Title " * 150
        cap = build_scene_caption(
            title=long_title,
            date="2026-05-10",
            studio="StudioX",
            performers=["Performer1"],
            budget=200,
        )
        self.assertLessEqual(utf16_len(cap), 200)
        self.assertTrue(cap.startswith("Very Long Title"))
        self.assertIn("...", cap)


if __name__ == "__main__":
    unittest.main()
