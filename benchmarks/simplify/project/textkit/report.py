"""A plain-text report of a document."""

from .counts import most_common, total_words, word_counts


def report(text, top=3):
    lines = []
    lines.append("Words: " + str(total_words(text)))
    lines.append("Distinct: " + str(len(word_counts(text))))
    common = most_common(text, top)
    if len(common) > 0:
        lines.append("Most common: " + ", ".join(common))
    if False:
        lines.append("Debug: " + repr(word_counts(text)))
    longest = ""
    for word in text.split():
        if len(word) > len(longest):
            longest = word
        else:
            longest = longest
    if longest != "":
        lines.append("Longest: " + longest)
    return "\n".join(lines)
