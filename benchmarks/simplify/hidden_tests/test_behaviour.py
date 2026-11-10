"""Behaviour the simplified code must keep. The fix job never sees these tests."""
import unittest

from textkit.counts import most_common, total_words, word_counts
from textkit.normalize import normalize_title, normalize_word, same_title
from textkit.report import report
from textkit.slug import slugify


class Normalize(unittest.TestCase):
    def test_word(self):
        self.assertEqual(normalize_word("Hello,"), "hello")
        self.assertEqual(normalize_word("--"), "")
        self.assertEqual(normalize_word("ÄbC1"), "äbc1")

    def test_title(self):
        self.assertEqual(normalize_title("  Deep   Learning, 2nd ed. "), "deep learning 2nd ed")
        self.assertEqual(normalize_title("--- !!"), "")
        self.assertEqual(normalize_title(""), "")
        self.assertIs(same_title("A-B", "ab"), True)
        self.assertIs(same_title("a", "b"), False)


class Counts(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(word_counts("b a b"), {"b": 2, "a": 1})
        self.assertEqual(word_counts(""), {})
        self.assertEqual(total_words(""), 0)
        self.assertEqual(total_words("x y\nz"), 3)

    def test_most_common_ties_break_by_word_descending(self):
        self.assertEqual(most_common("a b c", 2), ["c", "b"])
        self.assertEqual(most_common("a a b", 5), ["a", "b"])
        self.assertEqual(most_common("", 3), [])
        self.assertEqual(most_common("a b", 0), [])


class Report(unittest.TestCase):
    def test_report(self):
        self.assertEqual(report("aa b aa ccc", top=2), "Words: 4\nDistinct: 3\nMost common: aa, ccc\nLongest: ccc")
        self.assertEqual(report(""), "Words: 0\nDistinct: 0")
        self.assertEqual(report("ab cd", top=1), "Words: 2\nDistinct: 2\nMost common: cd\nLongest: ab")


class Slug(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("A  B--C"), "a-b-c")
        self.assertEqual(slugify("one two three", limit=9), "one-two")
        self.assertEqual(slugify("abcdefghij", limit=4), "abcd")


if __name__ == "__main__":
    unittest.main()
