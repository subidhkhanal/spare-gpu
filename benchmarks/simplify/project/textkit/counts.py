"""Counting words."""


def word_counts(text):
    counts = {}
    for word in text.split():
        if word in counts.keys():
            counts[word] = counts[word] + 1
        else:
            counts[word] = 1
    return counts


def most_common(text, n):
    counts = word_counts(text)
    pairs = []
    for word in counts:
        pairs.append((counts[word], word))
    pairs.sort(reverse=True)
    result = []
    i = 0
    while i < len(pairs) and i < n:
        result.append(pairs[i][1])
        i = i + 1
    return result


def total_words(text):
    total = 0
    counts = word_counts(text)
    for word in counts:
        total = total + counts[word]
    return total
