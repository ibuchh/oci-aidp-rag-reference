"""Tailwind-inspired presentation. No external fonts, scripts or stylesheets."""
from html import escape
import base64
from pathlib import Path

import streamlit as st


CSS = '''<style>
:root {
  --poc-blue: #2563eb;
  --poc-ink: #0f172a;
  --poc-muted: #64748b;
  --poc-border: #e2e8f0;
}
.block-container {
  max-width: 1180px;
  padding: 2.25rem 3rem 6.5rem;
}
[data-testid="stAppDeployButton"] {display: none;}
[data-testid="stHeader"] {background: rgba(255,255,255,.96);}
[data-testid="stSidebar"] {border-right: 1px solid var(--poc-border);}
[data-testid="stSidebar"] > div:first-child {padding-top: 2rem;}
[data-testid="stSidebar"] h2 {font-size: 1rem; font-weight: 650; letter-spacing: -.02em;}
[data-testid="stSidebar"] [data-testid="stCode"] {
  background: #fff; border: 1px solid var(--poc-border); border-radius: 8px;
}
[data-testid="stSidebar"] [data-testid="stCode"] pre {font-size: .74rem;}
[data-testid="stSidebar"] label p {font-size: .82rem; font-weight: 500;}
h1 {font-size: 2.65rem !important; line-height: 1.12 !important;
    font-weight: 750 !important; letter-spacing: -.055em !important; color: var(--poc-ink);}
h2,h3 {letter-spacing: -.025em;}
[data-testid="stCaptionContainer"] {color: var(--poc-muted);}
[data-testid="stExpander"] {border-color: var(--poc-border); background: #fff; border-radius: 9px;}
[data-testid="stExpander"] summary {font-weight: 550; font-size: .87rem;}
[data-testid="stButton"] button, [data-testid="stLinkButton"] a,
[data-testid="stDownloadButton"] button {border-radius: 7px; font-weight: 550;}
[data-testid="stMetric"] {
  background: #fff; border: 1px solid var(--poc-border); border-radius: 10px;
  padding: 1rem 1.1rem; box-shadow: 0 1px 2px rgba(15,23,42,.025);
}
[data-testid="stMetricLabel"] {color: var(--poc-muted); font-size: .78rem;}
[data-testid="stMetricValue"] {font-weight: 650; letter-spacing: -.04em;}
[data-testid="stChatMessage"] {
  background: #fff; border: 1px solid var(--poc-border); border-radius: 12px;
  padding: 1.15rem 1.2rem; margin-bottom: .7rem;
}
[data-testid="stChatMessageAvatarImage"] {
  border: none !important; background: transparent !important;
  border-radius: 50% !important; box-shadow: none !important;
}
[data-testid="stChatInput"] {border-radius: 12px; border-color: #cbd5e1;
  box-shadow: 0 2px 10px rgba(15,23,42,.04);}
.poc-brand {display:flex;align-items:center;gap:10px;padding-bottom:1.15rem;
  margin-bottom:1.3rem;border-bottom:1px solid var(--poc-border);}
.poc-brand-icon {display:grid;place-items:center;background:var(--poc-blue);color:white;
  height:32px;width:32px;border-radius:8px;font-size:22px;}
.poc-brand-name {color:var(--poc-ink);font-size:1.02rem;font-weight:700;letter-spacing:-.03em;}
.poc-brand-sub {color:var(--poc-muted);font-size:.62rem;letter-spacing:.09em;margin-top:3px;}
.poc-topline {display:flex;align-items:center;justify-content:space-between;gap:12px;
  margin:0 0 1.2rem;color:var(--poc-muted);font-size:.68rem;font-weight:600;letter-spacing:.1em;}
.poc-mode {font-size:.7rem;letter-spacing:0;font-weight:500;color:#475569;
  background:#f8fafc;border:1px solid var(--poc-border);border-radius:6px;padding:5px 9px;}
.poc-subtitle {font-size:1rem;color:var(--poc-muted);line-height:1.65;margin:.2rem 0 1.6rem;}
.poc-summary {display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin:0 0 1.65rem;}
.poc-summary-card {border:1px solid var(--poc-border);border-radius:9px;padding:18px 20px;background:white;}
.poc-summary-label {font-size:.68rem;font-weight:600;letter-spacing:.07em;color:var(--poc-muted);}
.poc-summary-value {font-size:1.35rem;font-weight:700;letter-spacing:-.035em;color:var(--poc-ink);margin:8px 0 5px;}
.poc-summary-help {font-size:.75rem;line-height:1.4;color:var(--poc-muted);}
.poc-section {display:flex;align-items:center;justify-content:space-between;margin:1.35rem 0 .85rem;}
.poc-section-title {font-size:.93rem;font-weight:650;color:var(--poc-ink);}
.poc-section-note {font-size:.73rem;color:var(--poc-muted);}
.poc-empty {border:1px solid var(--poc-border);border-radius:12px;padding:2rem 1.6rem;
  text-align:center;background:linear-gradient(180deg,#fff 0%,#fbfdff 100%);margin:.15rem 0 1.15rem;}
.poc-empty-icon {display:inline-grid;place-items:center;width:44px;height:44px;border-radius:12px;
  border:1px solid #dbeafe;background:#eff6ff;color:var(--poc-blue);font-size:24px;}
.poc-empty h3 {font-size:1.18rem;font-weight:650;color:var(--poc-ink);margin:13px 0 6px;padding:0;}
.poc-empty p {color:var(--poc-muted);font-size:.86rem;line-height:1.6;margin:0;}
.poc-examples {display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:1.5rem;text-align:left;}
.poc-example {border:1px solid var(--poc-border);background:#fff;border-radius:8px;padding:13px 14px;}
.poc-example strong {display:block;color:#334155;font-size:.77rem;font-weight:600;margin-bottom:5px;}
.poc-example span {display:block;color:var(--poc-muted);font-size:.75rem;line-height:1.5;}
@media(max-width:900px) {
  .block-container {padding:1.6rem 1.4rem 6rem;}
  h1 {font-size:2.1rem !important;}
  .poc-summary {gap:9px;}
  .poc-summary-card {padding:14px;}
  .poc-summary-value {font-size:1.1rem;}
}
@media(max-width:600px) {
  .poc-summary,.poc-examples {grid-template-columns:1fr;}
  .poc-summary-card {display:grid;grid-template-columns:1fr 1fr;align-items:center;gap:4px 8px;}
  .poc-summary-value {margin:0;text-align:right;}
  .poc-summary-help {grid-column:1 / -1;}
  .poc-empty {padding:1.5rem 1rem;}
  .poc-mode {max-width:52%;font-size:.65rem;}
  .poc-section-note {font-size:.67rem;}
}
</style>'''


def apply_theme():
    st.markdown(CSS, unsafe_allow_html=True)


def chat_avatar(role):
    """Local Heroicons SVGs, with explicit colors for image rendering."""
    name, color, background = (
        ('user-circle.svg', '#475569', '#F1F5F9') if role == 'user'
        else ('sparkles.svg', '#2563EB', '#EFF6FF')
    )
    icon = (Path(__file__).parent/'icons'/name).read_text()
    icon = icon.replace('<svg ', '<svg x="5" y="5" width="22" height="22" ', 1)
    icon = icon.replace('stroke="currentColor"', f'stroke="{color}"')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" '
           'viewBox="0 0 32 32">'
           f'<circle cx="16" cy="16" r="16" fill="{background}"/>'
           + icon + '</svg>')
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()


def brand():
    st.markdown('''<div class="poc-brand"><div class="poc-brand-icon">◇</div>
<div><div class="poc-brand-name">Private AI</div>
<div class="poc-brand-sub">OCI INFERENCE WORKSPACE</div></div></div>''', unsafe_allow_html=True)


def header(model, context_limit, location):
    mode = 'Monitoring workspace' if 'Monitoring' in location else 'Mac workspace'
    st.markdown(f'<div class="poc-topline"><span>WORKSPACE / PRIVATE CHAT</span><span class="poc-mode">{escape(mode)}</span></div>', unsafe_allow_html=True)
    st.title('AI Inference with vLLM and NVIDIA A10 GPUs')
    st.markdown('<p class="poc-subtitle">Ask a question, tune the response, and explore your private AI on OCI.</p>', unsafe_allow_html=True)
    cards = [
        ('SELECTED MODEL', escape(model), 'Served by your private vLLM endpoint'),
        ('CONTEXT CAPACITY', f'{context_limit:,} tokens', 'Prompt, conversation and response'),
        ('CONVERSATION HISTORY', 'Session only', 'Export a copy when you choose'),
    ]
    html = '<div class="poc-summary">'
    for label, value, note in cards:
        html += f'<div class="poc-summary-card"><div class="poc-summary-label">{label}</div><div class="poc-summary-value">{value}</div><div class="poc-summary-help">{note}</div></div>'
    st.markdown(html+'</div>', unsafe_allow_html=True)


def conversation_heading():
    st.markdown('<div class="poc-section"><span class="poc-section-title">Conversation</span><span class="poc-section-note">Private endpoint · HTTPS</span></div>', unsafe_allow_html=True)


def empty_state():
    st.markdown('''<section class="poc-empty"><div class="poc-empty-icon">✦</div>
<h3>Start with a good question.</h3>
<p>Your private model is a message away. Write below, or take inspiration from these examples.</p>
<div class="poc-examples">
<div class="poc-example"><strong>Explain a concept</strong><span>Explain cloud computing in two simple sentences.</span></div>
<div class="poc-example"><strong>Draft a response</strong><span>Help me write a clear project update for my team.</span></div>
<div class="poc-example"><strong>Compare ideas</strong><span>Compare two approaches and explain their tradeoffs.</span></div>
</div></section>''', unsafe_allow_html=True)
