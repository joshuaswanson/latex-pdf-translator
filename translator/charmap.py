# rsfs script letter mapping (rsfs extracts as plain letters, need Unicode script)
RSFS_CHAR_MAP = {
    "A": "\U0001D49C", "B": "\u212C", "C": "\U0001D49E",
    "D": "\U0001D49F", "E": "\u2130", "F": "\u2131",
    "G": "\U0001D4A2", "H": "\u210B", "I": "\u2110",
    "J": "\U0001D4A5", "K": "\U0001D4A6", "L": "\u2112",
    "M": "\u2133", "N": "\U0001D4A9", "O": "\U0001D4AA",
    "P": "\U0001D4AB", "Q": "\U0001D4AC", "R": "\u211B",
    "S": "\U0001D4AE", "T": "\U0001D4AF", "U": "\U0001D4B0",
    "V": "\U0001D4B1", "W": "\U0001D4B2", "X": "\U0001D4B3",
    "Y": "\U0001D4B4", "Z": "\U0001D4B5",
}

# Build math italic letter mapping (a-z -> U+1D44E..., A-Z -> U+1D434...)
# These are the Unicode "Mathematical Italic" code points
MATH_ITALIC_MAP = {}
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    cp = 0x1D434 + i
    MATH_ITALIC_MAP[ch] = chr(cp)
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    cp = 0x1D44E + i
    if cp == 0x1D455:  # 'h' is at a different position (planck constant)
        MATH_ITALIC_MAP[ch] = "\u210E"
    else:
        MATH_ITALIC_MAP[ch] = chr(cp)

# Math italic Greek mapping
_GREEK_ITALIC_START = 0x1D6FC  # alpha
_GREEK_LOWER = "\u03b1\u03b2\u03b3\u03b4\u03b5\u03b6\u03b7\u03b8\u03b9\u03ba\u03bb\u03bc\u03bd\u03be\u03bf\u03c0\u03c1\u03c2\u03c3\u03c4\u03c5\u03c6\u03c7\u03c8\u03c9"
for i, ch in enumerate(_GREEK_LOWER):
    MATH_ITALIC_MAP[ch] = chr(_GREEK_ITALIC_START + i)
# Additional Greek variants
MATH_ITALIC_MAP["\u03d5"] = "\U0001D719"  # phi variant
MATH_ITALIC_MAP["\u00b5"] = "\U0001D707"  # mu (from micro sign)

# Math bold letter mapping
MATH_BOLD_MAP = {}
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    MATH_BOLD_MAP[ch] = chr(0x1D400 + i)
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    MATH_BOLD_MAP[ch] = chr(0x1D41A + i)
for i, ch in enumerate("0123456789"):
    MATH_BOLD_MAP[ch] = chr(0x1D7CE + i)

# Euler Fraktur (EUFM) letter mapping
EUFM_CHAR_MAP = {}
_FRAKTUR_UPPER = 0x1D504
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    cp = _FRAKTUR_UPPER + i
    # Unicode assigns some Fraktur letters to different code points
    if ch == "C": cp = 0x212D
    elif ch == "H": cp = 0x210C
    elif ch == "I": cp = 0x2111
    elif ch == "R": cp = 0x211C
    elif ch == "Z": cp = 0x2128
    EUFM_CHAR_MAP[ch] = chr(cp)
_FRAKTUR_LOWER = 0x1D51E
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    EUFM_CHAR_MAP[ch] = chr(_FRAKTUR_LOWER + i)

# Fixes for Google Translate mistakes in English math prose. Keys are
# case-insensitive regexes anchored at a word start; the replacement copies the
# capitalization of the matched text.
TERM_FIXES = {
    r"variable (\{M\d+\})-adic\b": r"\1-adic variable",
    r"temperate distribution": "tempered distribution",
    r"temperature distribution": "tempered distribution",
    r"measurement": "measure",
    r"class functions(?= \{M\d+\})": "functions of class",
    r"summary\. \u2014": "abstract. \u2014",
    r"mirabol(?:ous|ique)\b": "mirabolic",
    r"demonstrate the results\b": "prove the results",
    r"let(?: u|')s demonstrate\b": "let us prove",
    r"we demonstrate\b": "we prove",
    r"one demonstrates\b": "one proves",
    r"demonstration": "proof",
    r"locally analytical\b": "locally analytic",
    r"analytical function": "analytic function",
    r"distribution operations\b": "operations on distributions",
    r"point support distributions\b": "distributions with point support",
    r"compact open(?=\s*(?:[.,;:)]|$|\{M\d+\}))": "compact open set",
    r"whatever(?= (?:the )?\{M\d+\})": "for all",
    r"(?<!tends )(?<!tend )(?<!tending )(?<!goes )(?<!going )(?<!diverges )to infinity\b": "at infinity",
    r"th\u00e9or\u00e8me\b": "theorem",
    r"corollaire\b": "corollary",
    r"remarque\b": "remark",
    r"d\u00e9finition\b": "definition",
    r"lemme\b": "lemma",
}
