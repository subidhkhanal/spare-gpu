import unittest

from textkit.counts import most_common, total_words
from textkit.normalize import same_title
from textkit.report import report
from textkit.slug import slugify


class TextkitTest(unittest.TestCase):
    def test_basics(self):
        self.assertTrue(same_title("Deep Learning!", "deep  learning"))
        self.assertEqual(total_words("a b a"), 3)
        self.assertEqual(most_common("a b a c a b", 2), ["a", "b"])
        self.assertIn("Words: 3", report("a b a"))
        self.assertEqual(slugify("Hello, World!"), "hello-world")


if __name__ == "__main__":
    unittest.main()
