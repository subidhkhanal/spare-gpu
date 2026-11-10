"""Normalising words and titles."""


def normalize_word(word):
    result = ""
    for ch in word:
        if ch.isalnum():
            result = result + ch.lower()
        else:
            result = result + ""
    return result


def normalize_title(title):
    words = []
    for word in title.split(" "):
        cleaned = ""
        for ch in word:
            if ch.isalnum():
                cleaned = cleaned + ch.lower()
        if cleaned != "":
            words.append(cleaned)
        else:
            pass
    return " ".join(words)


def same_title(a, b):
    if normalize_title(a) == normalize_title(b):
        return True
    else:
        return False
