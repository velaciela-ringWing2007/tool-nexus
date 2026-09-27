"""サイバーパンク風ダークテーマのCSSと、HTML片の生成.

LIST NEXUS の styles.py を土台にしている（配色・行レイアウト・列幅の固定）。
CSSは機能ロジックから分離し、このモジュールに閉じ込める。
HTMLへユーザー入力を出力する場合は、必ず escape_html() を通す
（TOOL NEXUS はコマンド文字列やパスを画面に出すため特に重要）。
"""

from __future__ import annotations

import html
from typing import Iterable

import streamlit as st

from tool_nexus.core.constants import APP_NAME

# 配色（可読性を最優先し、彩度の高い色は輪郭と強調にのみ使う）
COLOR_BACKGROUND = "#070b16"
COLOR_PANEL = "#0e1526"
COLOR_PANEL_ALT = "#131c31"
COLOR_ACCENT = "#22d3ee"
COLOR_ACCENT_SUB = "#f472d0"
COLOR_TEXT = "#dbe6ff"
COLOR_MUTED = "#8fa0c0"
COLOR_WARN = "#fbbf24"
COLOR_DANGER = "#f87171"

_CSS = f"""
<style>
:root {{
    --tn-bg: {COLOR_BACKGROUND};
    --tn-panel: {COLOR_PANEL};
    --tn-panel-alt: {COLOR_PANEL_ALT};
    --tn-accent: {COLOR_ACCENT};
    --tn-accent-sub: {COLOR_ACCENT_SUB};
    --tn-text: {COLOR_TEXT};
    --tn-muted: {COLOR_MUTED};
    --tn-warn: {COLOR_WARN};
    --tn-danger: {COLOR_DANGER};
    --tn-border: rgba(34, 211, 238, 0.28);
}}

/* ---------- 全体 ---------- */
[data-testid="stAppViewContainer"] {{
    background-color: var(--tn-bg);
    background-image:
        linear-gradient(rgba(34, 211, 238, 0.05) 1px, transparent 1px),
        linear-gradient(90deg, rgba(34, 211, 238, 0.05) 1px, transparent 1px);
    background-size: 44px 44px;
    color: var(--tn-text);
}}
[data-testid="stHeader"] {{
    background: transparent;
}}
[data-testid="stAppViewContainer"] h1,
[data-testid="stAppViewContainer"] h2,
[data-testid="stAppViewContainer"] h3,
[data-testid="stAppViewContainer"] h4 {{
    color: var(--tn-text);
    letter-spacing: 0.04em;
}}

/* ---------- 画面上部のバー ---------- */
.tn-appbar {{
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    white-space: nowrap;
    overflow: hidden;
}}
.tn-brand__name {{
    font-size: 1.9rem;
    font-weight: 800;
    letter-spacing: 0.18em;
    color: var(--tn-accent);
    text-shadow: 0 0 12px rgba(34, 211, 238, 0.45);
}}
.tn-brand__sub {{
    font-size: 0.8rem;
    color: var(--tn-muted);
    letter-spacing: 0.16em;
}}
@media (max-width: 1500px) {{
    .tn-appbar .tn-brand__sub {{ display: none; }}
}}
[class*="st-key-tn-topbar"] {{
    border-bottom: 1px solid var(--tn-border);
    padding: 0.1rem 0 0.5rem 0;
    margin-bottom: 0.9rem;
}}
/* 種別タブは文字幅に合わせた固定幅にし、残りを余白にする（比率配分だと「Streamlit」が省略される） */
[class*="st-key-tn-tabs"] [data-testid="stHorizontalBlock"] {{
    flex-wrap: nowrap;
    gap: 0.4rem;
}}
[class*="st-key-tn-tabs"] [data-testid="stColumn"] {{
    flex: 0 0 6.4rem !important;
    min-width: 0 !important;
}}
[class*="st-key-tn-tabs"] [data-testid="stColumn"]:last-child {{
    flex: 1 1 0 !important;
}}
[class*="st-key-tn-tabs"] .stButton > button {{
    white-space: nowrap;
    border-radius: 6px 6px 0 0;
    border-bottom-width: 2px;
    font-size: 0.9rem;
    font-weight: 600;
}}

/* ---------- 左ナビ（自前の2列レイアウト。st.sidebar は使わない） ---------- */
[class*="st-key-tn-nav"] {{
    background-color: var(--tn-panel);
    border: 1px solid var(--tn-border);
    border-radius: 8px;
    padding: 0.6rem 0.7rem 0.9rem 0.7rem;
    position: sticky;
    top: 0.5rem;
}}
[class*="st-key-tn-nav"] .stButton > button {{
    text-align: left;
    justify-content: flex-start;
    font-size: 0.85rem;
    padding: 0.28rem 0.6rem;
}}
[class*="st-key-tn-nav"] h3 {{
    font-size: 0.95rem;
    margin: 0.6rem 0 0.3rem 0;
    color: var(--tn-muted);
    letter-spacing: 0.08em;
}}

/* ---------- ボタン ---------- */
.stButton > button,
.stDownloadButton > button {{
    border: 1px solid var(--tn-border);
    background: rgba(34, 211, 238, 0.07);
    color: var(--tn-text);
    border-radius: 6px;
    font-size: 0.82rem;
    transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}}
.stButton > button:hover,
.stDownloadButton > button:hover {{
    border-color: var(--tn-accent);
    background: rgba(34, 211, 238, 0.16);
    color: #ffffff;
    box-shadow: 0 0 10px rgba(34, 211, 238, 0.28);
}}
.stButton > button[kind="primary"] {{
    border-color: var(--tn-accent);
    background: rgba(34, 211, 238, 0.2);
    font-weight: 700;
}}
.stButton > button:disabled {{
    opacity: 0.4;
    box-shadow: none;
}}

/* ---------- 入力系 ---------- */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stNumberInput"] input {{
    background-color: rgba(7, 11, 22, 0.85);
    color: var(--tn-text);
    border: 1px solid var(--tn-border);
}}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {{
    border-color: var(--tn-accent);
    box-shadow: 0 0 8px rgba(34, 211, 238, 0.3);
}}

/* ---------- リンク（素のアンカー。st.link_button はウィジェットになり重い） ---------- */
.tn-links {{
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: 0.35rem;
}}
.tn-links a {{
    flex: 0 0 auto;
    text-align: center;
    padding: 0.18rem 0.55rem;
    border: 1px solid rgba(34, 211, 238, 0.55);
    border-radius: 6px;
    background: rgba(34, 211, 238, 0.07);
    color: var(--tn-text) !important;
    font-size: 0.76rem;
    font-weight: 700;
    text-decoration: none;
    white-space: nowrap;
    transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}}
.tn-links a:hover {{
    border-color: var(--tn-accent);
    background: rgba(34, 211, 238, 0.16);
    color: #ffffff !important;
    box-shadow: 0 0 10px rgba(34, 211, 238, 0.28);
}}
.tn-port {{
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 0.85rem;
    color: var(--tn-text);
    min-width: 2.8rem;
}}
.tn-port--none {{
    color: var(--tn-muted);
}}

/* ---------- ツール行 ---------- */
[class*="st-key-tn-row-"] {{
    border-bottom: 1px solid rgba(34, 211, 238, 0.14);
    padding: 0.15rem 0.35rem;
    transition: background 0.12s ease;
}}
[class*="st-key-tn-row-"]:hover {{
    background: rgba(34, 211, 238, 0.06);
}}
.tn-row-main {{
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.15rem 0;
    min-width: 0;
}}
.tn-row-name {{
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.98rem;
    font-weight: 700;
    line-height: 1.4;
    color: var(--tn-text);
    word-break: break-word;
    overflow-wrap: anywhere;
}}
.tn-row-meta {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.7rem;
    align-items: center;
    font-size: 0.74rem;
    color: var(--tn-muted);
    padding-left: 1.2rem;
}}
.tn-row-meta__path {{
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    word-break: break-all;
}}
.tn-row-status {{
    font-size: 0.74rem;
    font-weight: 600;
    padding-left: 1.2rem;
}}
.tn-row-status--failed {{ color: var(--tn-danger); }}
.tn-row-status--starting {{ color: var(--tn-warn); }}
[class*="st-key-tn-row-"] .stButton > button {{
    padding: 0.25rem 0.4rem;
    min-height: 2rem;
}}

/* ---------- 状態の丸 ---------- */
.tn-dot {{
    flex: 0 0 auto;
    display: inline-block;
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 50%;
    background: var(--tn-muted);
    opacity: 0.55;
}}
.tn-dot--running {{
    background: var(--tn-accent);
    opacity: 1;
    box-shadow: 0 0 8px rgba(34, 211, 238, 0.8);
}}
.tn-dot--starting {{
    background: var(--tn-warn);
    opacity: 1;
    box-shadow: 0 0 8px rgba(251, 191, 36, 0.7);
    animation: tn-pulse 1.2s ease-in-out infinite;
}}
.tn-dot--failed {{
    background: var(--tn-danger);
    opacity: 1;
}}
.tn-dot--unknown {{
    background: transparent;
    border: 1px solid var(--tn-muted);
}}
@keyframes tn-pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.35; }}
}}

/* ---------- 一覧の見出し（件数・最終確認・再チェック） ---------- */
.tn-summary {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 1rem;
    align-items: baseline;
    font-size: 0.82rem;
    color: var(--tn-muted);
}}
.tn-summary strong {{
    color: var(--tn-accent);
    font-size: 1rem;
}}

/* ---------- 列幅の固定 ----------
   Streamlitの列は比率で配分されるため、画面幅が変わるとボタン列の幅まで
   変わってボタンの位置がずれる（LIST NEXUS実測: 幅1324pxでアイコン列が20pxになった）。
   ボタン列は固定幅にし、伸縮は名前の列だけに担わせる。
   min-width: 0 が無いと中身の最小幅を主張して後続の列を押し出す。 */
[class*="st-key-tn-row-"] [data-testid="stHorizontalBlock"],
[class*="st-key-tn-header"] [data-testid="stHorizontalBlock"],
[class*="st-key-tn-listhead"] [data-testid="stHorizontalBlock"] {{
    gap: 0.5rem;
    flex-wrap: nowrap;
}}
[class*="st-key-tn-row-"] [data-testid="stColumn"],
[class*="st-key-tn-header"] [data-testid="stColumn"],
[class*="st-key-tn-listhead"] [data-testid="stColumn"] {{
    min-width: 0 !important;
}}

/* ツール行: 名前 / ポート・開く / 起動・停止 / ログ / 編集 */
[class*="st-key-tn-row-"] [data-testid="stColumn"]:nth-child(1) {{ flex: 1 1 0 !important; }}
[class*="st-key-tn-row-"] [data-testid="stColumn"]:nth-child(2) {{ flex: 0 0 7.5rem !important; }}
[class*="st-key-tn-row-"] [data-testid="stColumn"]:nth-child(3) {{ flex: 0 0 4.6rem !important; }}
[class*="st-key-tn-row-"] [data-testid="stColumn"]:nth-child(4),
[class*="st-key-tn-row-"] [data-testid="stColumn"]:nth-child(5) {{ flex: 0 0 2.3rem !important; }}
[class*="st-key-tn-row-"] [data-testid="stColumn"]:nth-child(n+3) .stButton > button {{
    width: 100%;
    padding: 0.25rem 0;
}}

/* ヘッダー: 検索 / まとめて起動 / 検出 / 追加 */
[class*="st-key-tn-header"] [data-testid="stColumn"]:nth-child(1) {{ flex: 1 1 0 !important; }}
[class*="st-key-tn-header"] [data-testid="stColumn"]:nth-child(2),
[class*="st-key-tn-header"] [data-testid="stColumn"]:nth-child(3) {{ flex: 0 0 2.3rem !important; }}
[class*="st-key-tn-header"] [data-testid="stColumn"]:nth-child(2) button,
[class*="st-key-tn-header"] [data-testid="stColumn"]:nth-child(3) button {{
    width: 100%;
    padding: 0.25rem 0;
}}
[class*="st-key-tn-header"] [data-testid="stColumn"]:nth-child(4) {{ flex: 0 0 6.5rem !important; }}

/* 一覧の見出し: 件数 / 再チェック */
[class*="st-key-tn-listhead"] [data-testid="stColumn"]:nth-child(1) {{ flex: 1 1 0 !important; }}
[class*="st-key-tn-listhead"] [data-testid="stColumn"]:nth-child(2) {{ flex: 0 0 7.5rem !important; }}

/* ---------- 通知 ---------- */
.tn-note {{
    border-left: 3px solid var(--tn-accent);
    background: rgba(34, 211, 238, 0.07);
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    font-size: 0.85rem;
    color: var(--tn-text);
}}
.tn-note--warn {{
    border-left-color: var(--tn-warn);
    background: rgba(251, 191, 36, 0.09);
}}
.tn-note--error {{
    border-left-color: var(--tn-danger);
    background: rgba(248, 113, 113, 0.09);
}}

@media (max-width: 640px) {{
    .tn-brand__name {{ font-size: 1.4rem; }}
}}
</style>
"""


def apply_styles() -> None:
    """アプリ全体のCSSを適用する。CSSは固定文字列のみ。"""
    st.markdown(_CSS, unsafe_allow_html=True)


def escape_html(value: object) -> str:
    """HTMLへ埋め込む前にユーザー入力をエスケープする。"""
    return html.escape("" if value is None else str(value), quote=True)


def render_app_bar() -> None:
    """画面上部のアプリ名を描画する。"""
    st.markdown(
        '<div class="tn-appbar">'
        f'<span class="tn-brand__name">{escape_html(APP_NAME)}</span>'
        '<span class="tn-brand__sub">LOCAL TOOL LAUNCHER</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def status_dot(status: str, label: str) -> str:
    """状態の丸のHTML片。status は health.Status の値（固定の語）。"""
    return (
        f'<span class="tn-dot tn-dot--{escape_html(status)}" '
        f'title="{escape_html(label)}"></span>'
    )


def render_tool_summary(
    *,
    name: str,
    status: str,
    status_label: str,
    meta: Iterable[str],
    path: str,
    notice: str = "",
) -> None:
    """ツール行の主要部分（状態・名前・メタ情報）を1つのHTMLで描画する（0ウィジェット）。"""
    meta_html = "".join(f"<span>{escape_html(item)}</span>" for item in meta if item)
    parts = [
        f'<div class="tn-row-name">{status_dot(status, status_label)}'
        f"<span>{escape_html(name)}</span></div>",
        f'<div class="tn-row-meta"><span class="tn-row-meta__path">{escape_html(path)}</span>'
        f"{meta_html}</div>",
    ]
    if notice:
        parts.append(
            f'<div class="tn-row-status tn-row-status--{escape_html(status)}">'
            f"{escape_html(notice)}</div>"
        )
    st.markdown(f'<div class="tn-row-main">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_port_link(port: int | None, url: str | None) -> None:
    """ポート番号と「開く」リンクを描画する（0ウィジェット）。

    url は呼び出し側で http://127.0.0.1:<port> から組み立てたものに限る。
    """
    if port is None:
        st.markdown('<div class="tn-links"><span class="tn-port tn-port--none">—</span></div>',
                    unsafe_allow_html=True)
        return
    link = (
        f'<a href="{escape_html(url)}" target="_blank" rel="noopener noreferrer">開く</a>'
        if url
        else ""
    )
    st.markdown(
        f'<div class="tn-links"><span class="tn-port">{escape_html(port)}</span>{link}</div>',
        unsafe_allow_html=True,
    )


def render_summary(parts: Iterable[str]) -> None:
    """一覧の見出し（件数など）を描画する。parts はエスケープ済みのHTML片。"""
    st.markdown(f'<div class="tn-summary">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_note(message: str, level: str = "info") -> None:
    """簡易メッセージを描画する。message はエスケープする。"""
    modifier = {"warn": " tn-note--warn", "error": " tn-note--error"}.get(level, "")
    st.markdown(
        f'<div class="tn-note{modifier}">{escape_html(message)}</div>',
        unsafe_allow_html=True,
    )
