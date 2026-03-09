# CMEX control characters -> Unicode equivalents
CMEX_CHAR_MAP = {
    "\x00": "(", "\x01": ")", "\x02": "[", "\x03": "]",
    "\x04": "\u230A", "\x05": "\u230B",  # floor brackets
    "\x06": "\u2308", "\x07": "\u2309",  # ceiling brackets
    "\x08": "{", "\x09": "}",
    "\x0A": "\u27E8", "\x0B": "\u27E9",  # angle brackets
    "\x0C": "|",
    "\x10": "\u239B", "\x11": "\u239D",  # left paren top/bottom
    "\x12": "\u239E", "\x13": "\u23A0",  # right paren top/bottom
    "(": "(", " ": " ",
    "P": "\u2211",  # summation (text size)
    "Q": "\u220F",  # product (text size)
    "R": "\u222B",  # integral (text size)
    "X": "\u2211",  # summation (display size)
    "Y": "\u220F",  # product (display size)
    "Z": "\u222B",  # integral (display size)
    "\uf8f1": "\u23A7",  # left curly brace upper
    "\uf8f2": "\u23A8",  # left curly brace middle
    "\uf8f3": "\u23A9",  # left curly brace lower
    "\uf8f4": "\u23AB",  # right curly brace upper
}

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

# Post-translation terminology fixes (applied after {M0} markers restored)
TERM_FIXES = {
    # Title word order fix
    "VARIABLE {M0}-ADIC": "{M0}-ADIC VARIABLE",
    "Variable {M0}-adic": "{M0}-adic Variable",
    "variable {M0}-adic": "{M0}-adic variable",
    # Math terminology
    "temperate distributions": "tempered distributions",
    "Temperate distributions": "Tempered distributions",
    "temperate distribution": "tempered distribution",
    "Temperate distribution": "Tempered distribution",
    "temperature distributions": "tempered distributions",
    "Temperature distributions": "Tempered distributions",
    "temperature distribution": "tempered distribution",
    "Temperature distribution": "Tempered distribution",
    "measurements": "measures",
    "Measurements": "Measures",
    "Table of contents": "Table of Contents",
    "table of contents": "Table of Contents",
    "TABLE OF CONTENTS": "TABLE OF CONTENTS",
    "Class functions": "Functions of class",
    "class functions": "functions of class",
    "Summary. \u2014": "Abstract. \u2014",
    "mirabolous": "mirabolic",
    "Mirabolous": "Mirabolic",
    "mirabolique": "mirabolic",
    "Mirabolique": "Mirabolic",
    "to infinity": "at infinity",
    "demonstrate the results": "prove the results",
    "Locally analytical": "Locally analytic",
    "locally analytical": "locally analytic",
    "Analytical functions": "Analytic functions",
    "analytical functions": "analytic functions",
    "analytical function": "analytic function",
    "Distribution operations": "Operations on distributions",
    "Point Support Distributions": "Distributions with point support",
    "point support distributions": "distributions with point support",
    "Point support distributions": "Distributions with point support",
    "compact open": "compact open set",
    "let us demonstrate": "let us prove",
    "let's demonstrate": "let us prove",
    "we demonstrate": "we prove",
    "one demonstrates": "one proves",
    "whatever ": "for all ",
    "Whatever ": "For all ",
    "Demonstration": "Proof",
    "demonstration": "proof",
    "Th\u00e9or\u00e8me": "Theorem",
    "th\u00e9or\u00e8me": "theorem",
    "Corollaire": "Corollary",
    "corollaire": "corollary",
    "Remarque": "Remark",
    "remarque": "remark",
    "Proposition": "Proposition",
    "D\u00e9finition": "Definition",
    "d\u00e9finition": "definition",
    "Lemme": "Lemma",
    "lemme": "lemma",
}
