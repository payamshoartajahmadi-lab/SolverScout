"""Shared palette for Streamlit widgets and SolverScout's layout."""
from __future__ import annotations

CANVAS = "#181A2F"
SURFACE = "#242E49"
OUTLINE = "#37415C"
ACCENT = "#FDA481"
ERROR = "#B4182D"
ERROR_SURFACE = "#54162B"
TEXT = "#F7F5FC"
MUTED = "#BBC4D9"

_THEME = {
    "base": "dark",
    "primaryColor": ACCENT,
    "backgroundColor": CANVAS,
    "secondaryBackgroundColor": SURFACE,
    "textColor": TEXT,
    "borderColor": OUTLINE,
    "sidebar.backgroundColor": SURFACE,
    "sidebar.secondaryBackgroundColor": OUTLINE,
    "linkColor": ACCENT,
    "codeBackgroundColor": SURFACE,
    "redColor": ERROR,
    "redBackgroundColor": ERROR_SURFACE,
    "redTextColor": TEXT,
    "orangeColor": ACCENT,
    "orangeBackgroundColor": OUTLINE,
    "orangeTextColor": ACCENT,
    "greenColor": ACCENT,
    "greenBackgroundColor": SURFACE,
    "greenTextColor": TEXT,
    "baseRadius": "0.625rem",
    "showWidgetBorder": "true",
}
STREAMLIT_THEME_ARGS = tuple(f"--theme.{key}={value}" for key, value in _THEME.items())

UI_CSS = f"""
<style>
[data-testid="stMainBlockContainer"], [data-testid="stSidebar"] {{
    direction: rtl;
    text-align: start;
}}
[data-testid="stMainBlockContainer"] {{
    max-width: 76rem;
    padding: 3.1rem clamp(1.15rem, 3.5vw, 3.5rem) 3rem;
}}
[data-testid="stMainBlockContainer"] h1 {{
    padding: 0;
    font-size: clamp(2rem, 4vw, 2.9rem);
    font-weight: 750;
    line-height: 1.35;
}}
[data-testid="stMainBlockContainer"] h3 {{
    font-size: 1.18rem;
}}
[data-testid="stMainBlockContainer"] :is(h1, h2, h3, h4, h5, h6),
[data-testid="stMainBlockContainer"] :is([data-testid="stMarkdownContainer"], [data-testid="stCaptionContainer"]) {{
    text-align: right;
}}
[data-testid="stCaptionContainer"] p {{ color: {MUTED}; text-align: right; }}
[data-testid="stText"], [data-testid="stText"] > div {{
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    text-align: start;
}}
.st-key-workbench-brand {{
    direction: ltr;
    text-align: left;
    border-bottom: 1px solid {OUTLINE};
    padding-bottom: 0.8rem;
}}
.st-key-workbench-brand p {{
    font-size: 1.5rem;
    font-weight: 750;
}}
[data-testid="stSidebar"] [role="radiogroup"] label {{
    padding: 0.55rem 0.7rem;
    border-inline-start: 3px solid transparent;
    border-radius: 0.5rem;
}}
[data-testid="stSidebar"] [role="radiogroup"] label:hover,
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {{
    background: {OUTLINE};
}}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {{
    border-inline-start-color: {ACCENT};
}}
.st-key-problem-input, .st-key-run-method, .st-key-result-summary, .st-key-run-overview {{
    background: {SURFACE};
    border: 1px solid {OUTLINE};
    border-radius: 0.75rem;
    padding: 1.15rem clamp(0.85rem, 2vw, 1.4rem);
}}
.st-key-problem-input textarea {{ min-height: 6rem; }}
[data-testid="stExpander"] details {{
    background: {SURFACE};
    border-color: {OUTLINE};
}}
[data-testid="stHeadingWithActionElements"] {{ direction: rtl; }}
[data-testid="stAppDeployButton"] {{ display: none; }}
button[kind="primary"], button[kind="primaryFormSubmit"] {{
    color: {CANVAS};
    font-weight: 750;
    min-height: 2.75rem;
    min-width: 9rem;
}}
button[kind="primary"]:not(:disabled), button[kind="primaryFormSubmit"]:not(:disabled) {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: {CANVAS};
}}
button[kind="primary"]:not(:disabled):hover,
button[kind="primaryFormSubmit"]:not(:disabled):hover {{ filter: brightness(1.08); }}
.st-key-cancel-run button:not(:disabled) {{ border-color: {ERROR}; }}
.st-key-cancel-run button:not(:disabled):hover {{
    background: {ERROR_SURFACE};
    color: {TEXT};
}}
[data-testid="stCode"], [data-testid="stJson"], [data-testid="stDataFrame"],
[data-testid="stNumberInput"] input, input[type="password"], input[placeholder*="HTTPS"],
.st-key-new_expr input, .st-key-new_params input {{
    direction: ltr;
    unicode-bidi: isolate;
    text-align: start;
}}
@media (max-width: 640px) {{
    [data-testid="stMainBlockContainer"] {{ padding: 3rem 1rem 2rem; }}
    .st-key-problem-input [role="radiogroup"] {{ flex-wrap: wrap; }}
}}
</style>
"""
