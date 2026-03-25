"""
Postprocess Markdown notes to fix math rendering pitfalls:
- Convert code-fenced LaTeX blocks (``` ... ```) to $$ ... $$ display math
- Convert inline code containing LaTeX to $ ... $ inline math
- Normalize some common math notations (||f|| -> \lVert f \rVert, |x| -> \lvert x \rvert)
- De-escape over-escaped backslashes within math spans (\\frac -> \frac)

This is conservative: only transforms when LaTeX-like patterns are detected.
"""
from __future__ import annotations

import re
from typing import Callable

# --- Heuristics ---

# Detect LaTeX-like content (needs at least one LaTeX macro or typical math token)
LATEX_MACRO_RE = re.compile(
    r"\\(begin\{[^}]+\}|frac|sum|prod|int|oint|lim|infty|alpha|beta|gamma|delta|epsilon|theta|lambda|mu|pi|phi|psi|omega|sqrt|cdot|times|leq|geq|neq|approx|rightarrow|left|right|partial|nabla|vec|bm|mathbb|mathcal|operatorname)",
    re.IGNORECASE,
)
# Additional signal: many carets/underscores or typical set/interval tokens
MATH_SIG_RE = re.compile(r"(\^|_|\\l?Vert|\\l?vert|\\sum|\\int|\\lim|\\cdot|\\times|\\infty|\\to|\\mapsto|\\cap|\\cup|\\subset|\\supset|\\forall|\\exists|\\Rightarrow)")

# Tokens that strongly suggest the fenced block is code, not math
CODE_LIKE_RE = re.compile(r"\b(def|class|import|function|var|let|const|if|else|for|while|return|#include|int |float |printf\(|System\.out|public |package )\b")


def _looks_like_latex(text: str) -> bool:
    if CODE_LIKE_RE.search(text):
        return False
    if LATEX_MACRO_RE.search(text):
        return True
    # Require at least two math signals to avoid false positives
    return len(MATH_SIG_RE.findall(text)) >= 2


def _de_escape_backslashes(s: str) -> str:
    # Replace double backslashes with single inside math only
    # But avoid turning \\ (newline) into \ (keep one newline escape): reduce 2+ to 1
    return re.sub(r"\\{2,}", r"\\", s)


def _normalize_norm_abs(s: str) -> str:
    # Replace ||x|| -> \lVert x \rVert; |x| -> \lvert x \rvert (only simple cases)
    # Avoid replacing within existing \lVert/\lvert
    s = re.sub(r"(?<!\\)\|\|\s*([^|\n]+?)\s*\|\|", r"\\lVert \1 \\rVert", s)
    s = re.sub(r"(?<!\\)\|\s*([^|\n]+?)\s*\|", r"\\lvert \1 \\rvert", s)
    return s


def _transform_inline_code(md: str) -> str:
    # Transform `...` that looks like LaTeX into $...$
    pattern = re.compile(r"`([^`\n]+)`")

    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if _looks_like_latex(inner):
            inner2 = _de_escape_backslashes(inner)
            inner2 = _normalize_norm_abs(inner2)
            # Ensure we don't introduce stray $ pairs
            inner2 = inner2.replace("$", "\\$")
            return f"${inner2}$"
        return m.group(0)

    return pattern.sub(repl, md)


def _transform_fenced_blocks(md: str) -> str:
    # Handle triple backtick fences, with or without language
    fence_re = re.compile(r"(^|\n)(```[^\n]*\n)(.*?)(\n```)(?=\n|$)", re.DOTALL)

    def repl(m: re.Match) -> str:
        prefix = m.group(1)
        fence_head = m.group(2)
        body = m.group(3)
        fence_tail = m.group(4)
        # If looks like LaTeX, convert to display math
        if _looks_like_latex(body.strip()):
            body2 = _de_escape_backslashes(body.strip())
            body2 = _normalize_norm_abs(body2)
            # Strip possible surrounding $$ already added by LLM
            body2 = body2.strip()
            if body2.startswith("$$") and body2.endswith("$$"):
                body2 = body2.strip("$")
            return f"{prefix}$$\n{body2}\n$$"
        # Otherwise leave as-is
        return m.group(0)

    return fence_re.sub(repl, md)


def postprocess_math(md: str) -> str:
    """Apply math fixes to a Markdown document and return the new text."""
    # First, transform fenced blocks to $$
    md2 = _transform_fenced_blocks(md)
    # Then inline code to $ $
    md3 = _transform_inline_code(md2)
    return md3


if __name__ == "__main__":
    import sys
    content = sys.stdin.read()
    print(postprocess_math(content))
