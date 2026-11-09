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
@font-face {{
    font-family: Vazirmatn;
    src: url("/app/static/Vazirmatn.woff2") format("woff2");
    font-weight: 100 900;
    font-display: swap;
}}
:is([data-testid="stMainBlockContainer"], [data-testid="stSidebar"], [data-testid="stPopoverBody"],
    [role="listbox"]) :is(p, h1, h2, h3, h4, li, label, button, input, textarea, [role="option"]) {{
    font-family: Vazirmatn, Tahoma, Arial, sans-serif;
}}
[data-testid="stMainBlockContainer"], [data-testid="stSidebar"] {{
    direction: rtl;
    text-align: start;
}}
[data-testid="stMain"] {{
    order: 1;
    min-width: 0;
}}
[data-testid="stSidebar"] {{
    order: 2;
    border-left: 1px solid {OUTLINE};
    border-right: 0;
}}
[data-testid="stSidebarContent"] {{ padding-inline: 0.6rem; }}
[data-testid="stSidebarCollapseButton"], [data-testid="stExpandSidebarButton"],
[data-testid="stToolbar"] {{ display: none; }}
[data-testid="stMainBlockContainer"] {{
    max-width: 72rem;
    padding: 2.85rem clamp(1.15rem, 3.5vw, 3.5rem) 3rem;
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
[data-testid="stCaptionContainer"] p {{ color: {MUTED}; text-align: right; font-size: 0.9rem; }}
[data-testid="stText"], [data-testid="stText"] > div {{
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    text-align: start;
}}
.st-key-workbench-brand {{
    direction: ltr;
    text-align: left;
    padding: 0.45rem 0.55rem 1.1rem;
}}
.st-key-workbench-brand p {{
    font-size: 1.5rem;
    font-weight: 750;
}}
[data-testid="stSidebar"] button[kind="tertiary"],
[class*="st-key-mobile-nav-"] button[kind="tertiary"] {{
    justify-content: flex-start;
    padding: 0.65rem 0.85rem;
    border-inline-start: 3px solid transparent;
    border-radius: 0.55rem;
    min-height: 2.75rem;
    text-align: start;
}}
[data-testid="stSidebar"] button[kind="tertiary"] p {{
    text-align: start;
    font-size: 1rem;
}}
[data-testid="stSidebar"] button[kind="tertiary"]:hover,
[class*="st-key-desktop-nav-selected-"] button[kind="tertiary"],
[class*="st-key-mobile-nav-selected-"] button[kind="tertiary"] {{
    background: {OUTLINE};
    color: {TEXT};
}}
[data-testid="stSidebar"] [class*="st-key-desktop-nav-selected-"] button[kind="tertiary"],
[class*="st-key-mobile-nav-selected-"] button[kind="tertiary"] {{
    border-inline-start-color: {ACCENT};
    font-weight: 700;
}}
.st-key-mobile-navigation {{ display: none; }}
button:focus-visible, [role="radio"]:focus-visible, [role="tab"]:focus-visible {{
    outline: 2px solid {ACCENT};
    outline-offset: 2px;
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
.st-key-new_expr input, .st-key-new_params input, .st-key-wizard-formula input {{
    direction: ltr;
    unicode-bidi: isolate;
    text-align: start;
}}
@media (max-width: 767px) {{
    [data-testid="stSidebar"] {{ display: none; }}
    .st-key-mobile-navigation {{
        display: flex;
        align-items: center;
        padding-bottom: 0.8rem;
        border-bottom: 1px solid {OUTLINE};
        margin-bottom: 0.75rem;
    }}
    .st-key-mobile-navigation p {{ font-size: 1.05rem; }}
    .st-key-mobile-navigation button {{ min-height: 2.75rem; }}
    [data-testid="stMainBlockContainer"] {{ padding: 1rem 1rem 2rem; }}
    [data-testid="stMainBlockContainer"] h1 {{ font-size: clamp(1.55rem, 6vw, 2rem); }}
    .st-key-problem-input [role="radiogroup"] {{ flex-wrap: wrap; }}
}}
</style>
"""
