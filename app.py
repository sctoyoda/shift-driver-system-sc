"""
app.py - 軽貨物ドライバー シフト確認 Web アプリ (Streamlit)

主な機能:
  - PDFアップロード & 解析（pdfplumber による直接テキスト抽出）
  - 日付指定 / 明日の稼働ワンクリック表示
  - ドライバーごとの与野タイプ設定（通常/スポット/早番）
  - CSV / PDFダウンロード
"""
import io
import os
import re
import uuid
from datetime import date, datetime, timedelta
import pypdfium2 as pdfium

from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st
from fpdf import FPDF
from PIL import Image as PILImage

import database as db
import pdf_parser as parser

# ────────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────────

MAIN_JOBS_ORDER = ['与野', '川口', '巣鴨', 'イイダ', '高島平', '東天紅', 'ハナマサ']
EXCLUDED_JOBS   = {'ダイオ', '草加'}

LOGO_DIR = os.path.join(os.path.dirname(__file__), 'logos')
LOGO_MAP = {
    '与野':   '与野.png',
    '川口':   '川口.png',
    '巣鴨':   '巣鴨.png',
    '高島平': '高島平.png',
    'イイダ': 'イイダ.png',
    '東天紅': '東天紅.png',
    'ハナマサ':'ハナマサ.png',
}

# ロゴ前処理キャッシュ（白背景除去 + bbox自動クロップ）
_logo_buf_cache: dict = {}

def _prepare_logo_buf(logo_path: str):
    """背景色（コーナーから自動検出）を透明化してロゴ実体部分だけをクロップしたPNG BytesIOを返す。"""
    if logo_path in _logo_buf_cache:
        return _logo_buf_cache[logo_path]
    try:
        with PILImage.open(logo_path) as im:
            arr = np.array(im.convert('RGBA'))
        h, w = arr.shape[:2]
        # コーナー4点の色をサンプリして背景色を決定
        corners = [arr[0,0,:3], arr[0,w-1,:3], arr[h-1,0,:3], arr[h-1,w-1,:3]]
        bg = np.array(corners).mean(axis=0).astype(int)
        # 背景色に近いピクセル（各チャンネル±30以内）を透明化
        tol = 30
        mask = (
            (np.abs(arr[:,:,0].astype(int) - int(bg[0])) < tol) &
            (np.abs(arr[:,:,1].astype(int) - int(bg[1])) < tol) &
            (np.abs(arr[:,:,2].astype(int) - int(bg[2])) < tol)
        )
        arr[mask, 3] = 0
        im2 = PILImage.fromarray(arr)
        bbox = im2.getbbox()
        if bbox:
            im2 = im2.crop(bbox)
        buf = io.BytesIO()
        im2.save(buf, 'PNG')
        buf.seek(0)
        _logo_buf_cache[logo_path] = (buf, im2.size)
        return buf, im2.size
    except Exception:
        return None, (1, 1)

JOB_COLORS = {
    '与野':     {'bg': '#f0fdf4', 'border': '#43a047', 'header_bg': '#2e7d32', 'header_txt': '#ffffff'},
    '川口':     {'bg': '#fffdf0', 'border': '#f9a825', 'header_bg': '#f57f17', 'header_txt': '#ffffff'},
    '巣鴨':     {'bg': '#fff8f0', 'border': '#ef6c00', 'header_bg': '#e65100', 'header_txt': '#ffffff'},
    'イイダ':   {'bg': '#fafafa', 'border': '#757575', 'header_bg': '#37474f', 'header_txt': '#ffffff'},
    '高島平':   {'bg': '#f0f7ff', 'border': '#1e88e5', 'header_bg': '#0d47a1', 'header_txt': '#ffffff'},
    '東天紅':   {'bg': '#fdf4ff', 'border': '#8e24aa', 'header_bg': '#6a1b9a', 'header_txt': '#ffffff'},
    'ハナマサ': {'bg': '#fff0f6', 'border': '#e91e63', 'header_bg': '#880e4f', 'header_txt': '#ffffff'},
}

EARLY_JOB_COLORS = {
    'リネン':       {'bg': '#eff6ff', 'border': '#1565c0', 'header_bg': '#0d47a1', 'header_txt': '#ffffff'},
    'リネン対面':   {'bg': '#dbeafe', 'border': '#1565c0', 'header_bg': '#0a3060', 'header_txt': '#ffffff'},
    'リネン2回線':  {'bg': '#bfdbfe', 'border': '#0d47a1', 'header_bg': '#082040', 'header_txt': '#ffffff'},
    '洗濯':         {'bg': '#f7fee7', 'border': '#689f38', 'header_bg': '#33691e', 'header_txt': '#ffffff'},
    'カゴ回収':     {'bg': '#eef2ff', 'border': '#3949ab', 'header_bg': '#283593', 'header_txt': '#ffffff'},
}

WEEKDAY_JA = ['月', '火', '水', '木', '金', '土', '日']

YONO_TYPE_LABELS = {
    'normal':      '通常',
    'spot':        'スポット',
    'early_shift': '早番',
}
YONO_TYPE_OPTIONS = list(YONO_TYPE_LABELS.keys())

YONO_EXPECTED = {
    'weekday': {'normal': 3, 'early_shift': 1, 'spot': 1},
    'weekend': {'normal': 4, 'early_shift': 0, 'spot': 1},
}

# ドライバーアバター用カラーパレット（名前ハッシュで固定色）
_AVATAR_PALETTE = [
    '#3b82f6', '#10b981', '#8b5cf6', '#f59e0b',
    '#ef4444', '#06b6d4', '#ec4899', '#14b8a6',
    '#f97316', '#6366f1', '#84cc16', '#a855f7',
]


def _avatar_color(name: str) -> str:
    return _AVATAR_PALETTE[hash(name) % len(_AVATAR_PALETTE)]


# ────────────────────────────────────────────────
# ページ設定 & CSS
# ────────────────────────────────────────────────

def setup_page():
    st.set_page_config(
        page_title='シフト確認',
        page_icon='🚛',
        layout='wide',
        initial_sidebar_state='collapsed',
    )
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

    * { box-sizing: border-box; }

    .main .block-container,
    .stMainBlockContainer,
    section[data-testid="stMain"] .block-container,
    div[data-testid="stAppViewBlockContainer"] {
        padding: 0.5rem 1rem 5rem 1rem !important;
        max-width: 100% !important;
        font-family: 'Noto Sans JP', 'Hiragino Kaku Gothic ProN', 'Meiryo', sans-serif;
    }
    /* Streamlit のデフォルト左右余白を除去 */
    section[data-testid="stMain"] > div {
        padding-left: 0 !important;
        padding-right: 0 !important;
        max-width: 100% !important;
    }
    #MainMenu, footer, header { visibility: hidden; }

    /* ── APP HEADER ── */
    .app-header {
        background: #1a1a1a;
        padding: 1.5rem 1.2rem 1.3rem;
        text-align: center;
        border-bottom: 3px solid #1a1a1a;
    }
    .app-header .brand {
        font-size: 0.65rem;
        font-weight: 700;
        letter-spacing: 0.3em;
        text-transform: uppercase;
        color: #555;
        margin-bottom: 0.4rem;
    }
    .app-header h1 {
        font-size: 1.6rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        color: #f5f5f5;
        margin: 0 0 0.55rem;
    }
    .app-header .today-line {
        font-family: 'IBM Plex Mono', 'Courier New', monospace;
        font-size: 0.7rem;
        color: #444;
        letter-spacing: 0.1em;
    }

    /* ── TABS ── */
    .stTabs [role="tablist"] {
        background: #ffffff !important;
        border-bottom: 2px solid #e8e6df !important;
        padding: 0 0.8rem;
        gap: 0;
    }
    .stTabs [role="tab"] {
        font-size: 0.78rem !important;
        font-weight: 700 !important;
        letter-spacing: 0.12em !important;
        text-transform: uppercase !important;
        padding: 0.8rem 1rem !important;
        color: #aaa !important;
        border-bottom: 2px solid transparent !important;
        margin-bottom: -2px !important;
    }
    .stTabs [role="tab"][aria-selected="true"] {
        color: #1a1a1a !important;
        border-bottom-color: #1a1a1a !important;
    }
    .stTabs [role="tabpanel"] {
        padding: 1rem 0.9rem 0 !important;
    }

    /* ── DATE DISPLAY ── */
    .date-block {
        display: flex;
        align-items: center;
        gap: 1.2rem;
        padding: 1.2rem 0 1rem;
        border-bottom: 1px solid #e0ddd6;
        margin-bottom: 1.3rem;
    }
    .date-block .d-num {
        font-family: 'IBM Plex Mono', 'Courier New', monospace;
        font-size: 3rem;
        font-weight: 500;
        color: #1a1a1a;
        letter-spacing: -0.04em;
        line-height: 1;
        white-space: nowrap;
    }
    .date-block .d-slash {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 1.8rem;
        color: #ccc;
        line-height: 1;
    }
    .date-block .d-meta {
        display: flex;
        flex-direction: column;
        gap: 0.15rem;
    }
    .date-block .d-year {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 0.65rem;
        color: #aaa;
        letter-spacing: 0.1em;
    }
    .date-block .d-wd {
        font-size: 1rem;
        font-weight: 700;
        color: #1a1a1a;
        letter-spacing: 0.02em;
    }
    .date-block .d-badge {
        margin-left: auto;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 0.6rem;
        font-weight: 500;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        padding: 0.3rem 0.75rem;
        border-radius: 2px;
    }
    .badge-weekday { background: #e8f0fe; color: #1a56db; border: 1px solid #c3d6fd; }
    .badge-weekend { background: #fef3f2; color: #c0392b; border: 1px solid #fbd3cf; }
    .badge-holiday { background: #fef3f2; color: #c0392b; border: 1px solid #fbd3cf; }

    /* ── SECTION LABEL ── */
    .section-label {
        font-size: 0.65rem;
        font-weight: 700;
        letter-spacing: 0.22em;
        text-transform: uppercase;
        color: #aaa;
        margin: 1.5rem 0 0.7rem;
    }

    /* ── JOB CARD ── */
    .job-card {
        background: #ffffff;
        border-radius: 4px;
        overflow: hidden;
        margin-bottom: 0.55rem;
        border: 1px solid #e8e6df;
        border-left-width: 4px;
    }
    .job-card-header {
        padding: 0.65rem 1rem;
        display: flex;
        align-items: center;
        justify-content: space-between;
        border-bottom: 1px solid #f0ede6;
        background: #faf9f6;
    }
    .job-card-title {
        font-size: 1rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        color: #333;
    }
    .job-card-count {
        font-family: 'IBM Plex Mono', 'Courier New', monospace;
        font-size: 0.75rem;
        color: #aaa;
        letter-spacing: 0.06em;
    }
    .job-card-body { background: #fff; }

    /* ── DRIVER ROW ── */
    .driver-row {
        display: flex;
        align-items: center;
        padding: 0.72rem 1rem;
        border-bottom: 1px solid #f5f3ef;
        gap: 0.6rem;
    }
    .driver-row:last-child { border-bottom: none; }
    .d-name {
        font-size: 1rem;
        font-weight: 500;
        color: #1a1a1a;
        flex: 1;
        letter-spacing: 0.01em;
    }
    .d-badges {
        display: flex;
        gap: 0.3rem;
        align-items: center;
    }

    /* ── BADGES ── */
    .badge {
        font-size: 0.72rem;
        font-weight: 700;
        padding: 0.25rem 0.65rem;
        border-radius: 3px;
        white-space: nowrap;
        letter-spacing: 0.04em;
    }
    .badge-special     { background: #b91c1c; color: #fff; }
    .badge-early       { background: #1e40af; color: #fff; }
    .badge-spot        { background: #075985; color: #fff; }
    .badge-early-shift { background: #5b21b6; color: #fff; }
    .badge-yokonori    { background: #b45309; color: #fff; }

    /* ── WARNING ── */
    .warning-card {
        background: #fffbeb;
        border: 1px solid #fde68a;
        border-left: 4px solid #d97706;
        border-radius: 3px;
        padding: 0.8rem 1rem;
        margin-bottom: 1rem;
        font-size: 0.82rem;
        line-height: 1.75;
        color: #92400e;
        white-space: pre-line;
    }

    /* ── TOTAL BAR ── */
    .total-bar {
        display: flex;
        align-items: baseline;
        justify-content: flex-end;
        gap: 0.4rem;
        padding: 0.8rem 0.2rem 0;
        border-top: 1px solid #e8e6df;
        margin-top: 0.5rem;
    }
    .total-bar .t-label {
        font-size: 0.72rem;
        color: #aaa;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }
    .total-bar .t-num {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 1.15rem;
        font-weight: 500;
        color: #1a1a1a;
    }
    .total-bar .t-unit {
        font-size: 0.75rem;
        color: #888;
    }

    /* ── BUTTONS ── */
    div.stButton > button {
        width: 100%;
        background: #1a1a1a !important;
        color: #f5f5f5 !important;
        border: none !important;
        border-radius: 3px !important;
        font-weight: 700 !important;
        font-size: 0.82rem !important;
        letter-spacing: 0.1em !important;
        text-transform: uppercase !important;
        height: 2.8rem !important;
        transition: background 0.15s !important;
    }
    div.stButton > button:hover {
        background: #333 !important;
    }

    /* ── DIVIDER ── */
    .thin-divider {
        height: 1px;
        background: #e8e6df;
        margin: 1.2rem 0;
        border: none;
    }

    /* ── UPLOAD HINT ── */
    .upload-hint {
        border: 1.5px dashed #d0cdc6;
        border-radius: 4px;
        padding: 1.4rem 1rem;
        text-align: center;
        color: #888;
        font-size: 0.85rem;
        margin-bottom: 0.8rem;
        line-height: 1.7;
        background: #faf9f6;
    }
    .upload-hint strong { color: #444; font-weight: 700; }

    /* ── EMPTY STATE ── */
    .empty-state {
        text-align: center;
        padding: 3rem 1rem;
        color: #ccc;
    }
    .empty-state .empty-icon { font-size: 2.2rem; margin-bottom: 0.6rem; }
    .empty-state p {
        font-size: 0.82rem;
        margin: 0;
        line-height: 1.9;
        color: #aaa;
    }

    @media (max-width: 480px) {
        .main .block-container { padding: 0 0 5rem; }
        .date-block .d-num { font-size: 2.4rem; }
        .d-name { font-size: 0.95rem; }
        .stTabs [role="tab"] { padding: 0.7rem 0.7rem !important; font-size: 0.72rem !important; }
    }
    </style>
    """, unsafe_allow_html=True)


# ────────────────────────────────────────────────
# 与野人数チェック
# ────────────────────────────────────────────────

def _effective_yono_type(row, driver_configs: dict) -> str:
    """
    与野タイプを決定する優先順位:
      1. PDF セル色から検出した yono_type（spot / early_shift）
      2. driver_config の固定設定
      3. デフォルト 'normal'
    """
    from_pdf = row.get('yono_type', 'normal')
    if from_pdf and from_pdf != 'normal':
        return from_pdf
    return driver_configs.get(row['driver'], 'normal')


def _is_weekend(d: date) -> bool:
    try:
        import jpholiday
        return d.weekday() >= 5 or jpholiday.is_holiday(d)
    except ImportError:
        return d.weekday() >= 5


def _is_holiday(d: date) -> bool:
    try:
        import jpholiday
        return jpholiday.is_holiday(d)
    except ImportError:
        return False


def check_yono_warning(yono_df: pd.DataFrame, target_date: date) -> Optional[str]:
    counts = {'normal': 0, 'spot': 0, 'early_shift': 0}
    for _, row in yono_df.iterrows():
        t = row.get('effective_yono_type', 'normal')
        counts[t] = counts.get(t, 0) + 1

    is_weekend = _is_weekend(target_date)
    expected   = YONO_EXPECTED['weekend'] if is_weekend else YONO_EXPECTED['weekday']
    day_type   = '土日祝' if is_weekend else '平日'

    total_actual   = sum(counts.values())
    total_expected = sum(expected.values())

    if total_actual == total_expected:
        return None

    lines = [f'⚠️  与野人数が基準と異なります（{day_type}）']
    lines.append(f'通常: {counts["normal"]}名  (基準 {expected["normal"]}名)')
    if not is_weekend:
        lines.append(f'早番: {counts["early_shift"]}名  (基準 {expected["early_shift"]}名)')
    lines.append(f'スポット: {counts["spot"]}名  (基準 {expected["spot"]}名)')
    lines.append(f'合計: {total_actual}名  (基準 {total_expected}名)')
    return '\n'.join(lines)


# ────────────────────────────────────────────────
# HTML コンポーネント生成
# ────────────────────────────────────────────────

def _date_chip_html(target_date: date) -> str:
    wd = WEEKDAY_JA[target_date.weekday()]
    if _is_holiday(target_date):
        badge_cls, badge_label = 'badge-holiday', '祝日'
    elif target_date.weekday() >= 5:
        badge_cls, badge_label = 'badge-weekend', '休日'
    else:
        badge_cls, badge_label = 'badge-weekday', '平日'
    m  = f'{target_date.month:02d}'
    d  = f'{target_date.day:02d}'
    yr = str(target_date.year)
    return f"""
<div class="date-block">
  <span class="d-num">{m}</span>
  <span class="d-slash">/</span>
  <span class="d-num">{d}</span>
  <div class="d-meta">
    <span class="d-year">{yr}</span>
    <span class="d-wd">{wd}曜日</span>
  </div>
  <span class="d-badge {badge_cls}">{badge_label}</span>
</div>"""


def _card_html(title: str, color: dict, drivers_html: str, count: int) -> str:
    return f"""
<div class="job-card" style="border-left-color:{color['border']};">
  <div class="job-card-header">
    <span class="job-card-title">{title}</span>
    <span class="job-card-count">{count} 名</span>
  </div>
  <div class="job-card-body">
    {drivers_html}
  </div>
</div>"""


def _driver_row_html(driver: str, job_early: Optional[str],
                     special: bool, yono_type: Optional[str] = None,
                     yokonori: bool = False) -> str:
    badges = ''
    if job_early:
        badges += f'<span class="badge badge-early">{job_early}</span>'
    if yono_type == 'spot':
        badges += '<span class="badge badge-spot">スポット</span>'
    elif yono_type == 'early_shift':
        badges += '<span class="badge badge-early-shift">早番</span>'
    if yokonori:
        badges += '<span class="badge badge-yokonori">横乗り</span>'
    if special:
        badges += '<span class="badge badge-special">特殊</span>'

    badges_html = f'<div class="d-badges">{badges}</div>' if badges else ''
    return f"""
<div class="driver-row">
  <span class="d-name">{driver}</span>
  {badges_html}
</div>"""


# ────────────────────────────────────────────────
# シフト表示
# ────────────────────────────────────────────────

def render_shift_view(target_date_str: str):
    df = db.get_shifts_by_date(target_date_str)

    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except ValueError:
        st.error('日付の形式が正しくありません。')
        return

    st.markdown(_date_chip_html(target_date), unsafe_allow_html=True)

    if df.empty:
        st.markdown("""
        <div class="empty-state">
          <div class="empty-icon">📭</div>
          <p>この日のシフトデータはありません。<br>PDFをアップロードしてください。</p>
        </div>""", unsafe_allow_html=True)
        return

    display_df = df[~df['job_main'].fillna('').isin(EXCLUDED_JOBS)].copy()
    driver_configs = db.get_all_driver_configs()

    yono_df = display_df[display_df['job_main'] == '与野'].copy()
    yono_df['effective_yono_type'] = yono_df.apply(
        lambda r: _effective_yono_type(r, driver_configs), axis=1
    )
    warning = check_yono_warning(yono_df, target_date)
    if warning:
        st.markdown(f'<div class="warning-card">{warning}</div>', unsafe_allow_html=True)

    st.markdown('<div class="section-label">案件別稼働</div>', unsafe_allow_html=True)

    has_any = False
    for job in MAIN_JOBS_ORDER:
        job_df = display_df[display_df['job_main'] == job]
        if job_df.empty:
            continue
        has_any = True
        color = JOB_COLORS.get(job, {
            'bg': '#fafafa', 'border': '#9e9e9e',
            'header_bg': '#424242', 'header_txt': '#fff',
        })
        rows_html = ''
        for _, r in job_df.iterrows():
            yono_type = _effective_yono_type(r, driver_configs) if job == '与野' else None
            # 与野は早朝案件バッジを表示しない（早朝案件セクションに別途表示）
            early = None if job == '与野' else r.get('job_early')
            rows_html += _driver_row_html(
                r['driver'], early,
                bool(r.get('special_flag')), yono_type,
                yokonori=bool(r.get('yokonori_flag')),
            )
        st.markdown(_card_html(job, color, rows_html, len(job_df)), unsafe_allow_html=True)

    if not has_any:
        st.markdown("""
        <div class="empty-state">
          <div class="empty-icon">🔍</div>
          <p>案件データがありません。</p>
        </div>""", unsafe_allow_html=True)

    early_df = display_df[display_df['job_early'].notna() & (display_df['job_early'] != '')]
    if not early_df.empty:
        st.markdown('<div class="section-label">🌅 早朝案件</div>', unsafe_allow_html=True)
        for early_job, grp in early_df.groupby('job_early'):
            color = EARLY_JOB_COLORS.get(early_job, {
                'bg': '#eef2ff', 'border': '#3949ab',
                'header_bg': '#283593', 'header_txt': '#fff',
            })
            rows_html = ''.join(
                f'<div class="driver-row">'
                f'<span class="d-name">{r["driver"]}</span>'
                f'<span style="font-family:\'DM Mono\',monospace;font-size:0.65rem;'
                f'color:#333;letter-spacing:0.08em;text-transform:uppercase;">'
                f'{r["job_main"] or ""}</span></div>'
                for _, r in grp.iterrows()
            )
            st.markdown(_card_html(early_job, color, rows_html, len(grp)), unsafe_allow_html=True)

    st.markdown(
        f'<div class="total-bar">'
        f'<span class="t-label">Total</span>'
        f'<span class="t-num">{len(display_df)}</span>'
        f'<span class="t-unit">名</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ────────────────────────────────────────────────
# CSV / PDF ダウンロード
# ────────────────────────────────────────────────

def generate_csv(year_month: str) -> bytes:
    df = db.get_all_shifts_for_month(year_month)
    if df.empty:
        return b''
    driver_configs = db.get_all_driver_configs()
    df['yono_type'] = df.apply(
        lambda r: YONO_TYPE_LABELS.get(driver_configs.get(r['driver'], 'normal'), '通常')
        if r['job_main'] == '与野' else '', axis=1
    )
    df['special_flag'] = df['special_flag'].map({0: '', 1: '特殊'})
    df = df[['driver', 'date', 'job_main', 'job_early', 'yono_type', 'special_flag']]
    df.columns = ['ドライバー', '日付', 'メイン案件', '早朝案件', '与野タイプ', '特殊フラグ']
    return df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')


def generate_day_csv(target_date_str: str) -> bytes:
    df = db.get_shifts_by_date(target_date_str)
    if df.empty:
        return b''
    driver_configs = db.get_all_driver_configs()
    out = df[['driver', 'date', 'job_main', 'job_early', 'special_flag']].copy()
    out['yono_type'] = out.apply(
        lambda r: YONO_TYPE_LABELS.get(driver_configs.get(r['driver'], 'normal'), '通常')
        if r['job_main'] == '与野' else '', axis=1
    )
    out['special_flag'] = out['special_flag'].map({0: '', 1: '特殊'})
    out = out[['driver', 'date', 'job_main', 'job_early', 'yono_type', 'special_flag']]
    out.columns = ['ドライバー', '日付', 'メイン案件', '早朝案件', '与野タイプ', '特殊フラグ']
    return out.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')


def _build_shift_html(target_date_str: str) -> str:
    """Web UIと同じデザインのスタンドアロンHTMLを生成する"""
    df = db.get_shifts_by_date(target_date_str)
    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except ValueError:
        return ''
    if df.empty:
        return ''

    driver_configs = db.get_all_driver_configs()
    display_df = df[~df['job_main'].fillna('').isin(EXCLUDED_JOBS)].copy()

    date_html = _date_chip_html(target_date)

    cards_html = '<div class="section-label">案件別稼働</div>'
    for job in MAIN_JOBS_ORDER:
        job_df = display_df[display_df['job_main'] == job]
        if job_df.empty:
            continue
        color = JOB_COLORS.get(job, {'bg': '#fafafa', 'border': '#9e9e9e', 'header_bg': '#424242', 'header_txt': '#fff'})
        rows_html = ''
        for _, r in job_df.iterrows():
            yono_type = _effective_yono_type(r, driver_configs) if job == '与野' else None
            early = None if job == '与野' else r.get('job_early')
            rows_html += _driver_row_html(r['driver'], early, bool(r.get('special_flag')), yono_type, yokonori=bool(r.get('yokonori_flag')))
        cards_html += _card_html(job, color, rows_html, len(job_df))

    early_section_html = ''
    early_df = display_df[display_df['job_early'].notna() & (display_df['job_early'] != '')]
    if not early_df.empty:
        early_section_html = '<div class="section-label">早朝案件</div>'
        for early_job, grp in early_df.groupby('job_early'):
            color = EARLY_JOB_COLORS.get(early_job, {'bg': '#eef2ff', 'border': '#3949ab', 'header_bg': '#283593', 'header_txt': '#fff'})
            rows_html = ''.join(
                f'<div class="driver-row"><span class="d-name">{r["driver"]}</span></div>'
                for _, r in grp.iterrows()
            )
            early_section_html += _card_html(early_job, color, rows_html, len(grp))

    total_html = (
        f'<div class="total-bar">'
        f'<span class="t-label">Total</span>'
        f'<span class="t-num">{len(display_df)}</span>'
        f'<span class="t-unit">名</span>'
        f'</div>'
    )

    css = '''
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #f2f1ed;
    padding: 0.8rem 0.6rem 1.5rem;
    font-family: "Noto Sans CJK JP", "Noto Sans JP", "Hiragino Kaku Gothic ProN", "Meiryo", sans-serif;
    width: 420px;
}
.date-block {
    display: flex;
    align-items: center;
    gap: 1.2rem;
    padding: 1.2rem 0 1rem;
    border-bottom: 1px solid #e0ddd6;
    margin-bottom: 1.3rem;
}
.date-block .d-num {
    font-family: "Courier New", monospace;
    font-size: 3rem;
    font-weight: 500;
    color: #1a1a1a;
    line-height: 1;
    white-space: nowrap;
}
.date-block .d-slash {
    font-family: "Courier New", monospace;
    font-size: 1.8rem;
    color: #ccc;
    line-height: 1;
}
.date-block .d-meta { display: flex; flex-direction: column; gap: 0.15rem; }
.date-block .d-year {
    font-family: "Courier New", monospace;
    font-size: 0.65rem;
    color: #aaa;
}
.date-block .d-wd { font-size: 1rem; font-weight: 700; color: #1a1a1a; }
.date-block .d-badge {
    margin-left: auto;
    font-family: "Courier New", monospace;
    font-size: 0.6rem;
    font-weight: 500;
    padding: 0.3rem 0.75rem;
    border-radius: 2px;
}
.badge-weekday { background: #e8f0fe; color: #1a56db; border: 1px solid #c3d6fd; }
.badge-weekend { background: #fef3f2; color: #c0392b; border: 1px solid #fbd3cf; }
.badge-holiday { background: #fef3f2; color: #c0392b; border: 1px solid #fbd3cf; }
.section-label {
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: #aaa;
    margin: 1.5rem 0 0.7rem;
}
.job-card {
    background: #ffffff;
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 0.55rem;
    border: 1px solid #e8e6df;
    border-left-width: 4px;
}
.job-card-header {
    padding: 0.65rem 1rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid #f0ede6;
    background: #faf9f6;
}
.job-card-title { font-size: 1rem; font-weight: 700; color: #333; }
.job-card-count {
    font-family: "Courier New", monospace;
    font-size: 0.75rem;
    color: #aaa;
}
.job-card-body { background: #fff; }
.driver-row {
    display: flex;
    align-items: center;
    padding: 0.72rem 1rem;
    border-bottom: 1px solid #f5f3ef;
    gap: 0.6rem;
}
.driver-row:last-child { border-bottom: none; }
.d-name { font-size: 1rem; font-weight: 500; color: #1a1a1a; flex: 1; }
.d-badges { display: flex; gap: 0.3rem; align-items: center; }
.badge {
    font-size: 0.72rem;
    font-weight: 700;
    padding: 0.25rem 0.65rem;
    border-radius: 3px;
    white-space: nowrap;
}
.badge-special     { background: #b91c1c; color: #fff; }
.badge-early       { background: #1e40af; color: #fff; }
.badge-spot        { background: #075985; color: #fff; }
.badge-early-shift { background: #5b21b6; color: #fff; }
.badge-yokonori    { background: #b45309; color: #fff; }
.total-bar {
    display: flex;
    align-items: baseline;
    justify-content: flex-end;
    gap: 0.4rem;
    padding: 0.8rem 0.2rem 0;
    border-top: 1px solid #e8e6df;
    margin-top: 0.5rem;
}
.t-label { font-size: 0.72rem; color: #aaa; }
.t-num { font-family: "Courier New", monospace; font-size: 1.15rem; font-weight: 500; color: #1a1a1a; }
.t-unit { font-size: 0.75rem; color: #888; }
'''

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>
{date_html}
{cards_html}
{early_section_html}
{total_html}
</body>
</html>"""


def generate_day_image(target_date_str: str, dpi: int = 200) -> bytes:
    """シフト表をPNG画像として返す（Web UIと同じHTMLデザイン）。"""
    html_content = _build_shift_html(target_date_str)
    if not html_content:
        return b''

    try:
        import shutil as _shutil
        import tempfile
        from html2image import Html2Image

        browser = (
            _shutil.which('chromium-browser')
            or _shutil.which('chromium')
            or '/usr/bin/chromium-browser'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            hti = Html2Image(
                browser_executable=browser,
                output_path=tmpdir,
                custom_flags=[
                    '--no-sandbox',
                    '--disable-gpu',
                    '--disable-dev-shm-usage',
                    '--disable-software-rasterizer',
                ],
            )
            hti.screenshot(html_str=html_content, save_as='shift.png', size=(420, 3000))
            png_path = os.path.join(tmpdir, 'shift.png')
            if not os.path.exists(png_path):
                raise FileNotFoundError('screenshot not created')

            # 下部の余白を自動クロップ（背景色 #f2f1ed を除去）
            img = PILImage.open(png_path).convert('RGB')
            arr = np.array(img)
            bg = np.array([242, 241, 237])
            row_is_bg = np.all(np.abs(arr[:, :, :3].astype(int) - bg) < 10, axis=(1, 2))
            rows_with_content = np.where(~row_is_bg)[0]
            if len(rows_with_content) > 0:
                crop_h = int(rows_with_content[-1]) + 24
                img = img.crop((0, 0, img.width, crop_h))

            buf = io.BytesIO()
            img.save(buf, 'PNG', optimize=True)
            return buf.getvalue()

    except Exception:
        # フォールバック: 従来の PDF→PNG 方式
        pdf_bytes = generate_day_pdf(target_date_str)
        if not pdf_bytes:
            return b''
        doc = pdfium.PdfDocument(pdf_bytes)
        page = doc[0]
        bitmap = page.render(scale=dpi / 72)
        img = bitmap.to_pil()
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=True)
        return buf.getvalue()


def _load_font(pdf: FPDF):
    import glob
    import subprocess
    base = os.path.dirname(os.path.abspath(__file__))

    # 最優先: リポジトリ同梱の IPAexGothic（Streamlit Cloud で確実に動作）
    candidates = [
        os.path.join(base, 'fonts', 'ipaexg.ttf'),
        os.path.join(base, 'fonts', 'NotoSansCJK.ttc'),
        '/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc',
        '/System/Library/Fonts/Hiragino Sans GB W3.ttc',
    ]

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            pdf.add_font('CJK', '', path, uni=True)
            return 'CJK'
        except Exception:
            pass

    return None


def generate_day_pdf(target_date_str: str) -> bytes:
    df = db.get_shifts_by_date(target_date_str)
    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except ValueError:
        return b''

    driver_configs = db.get_all_driver_configs()
    wd = WEEKDAY_JA[target_date.weekday()]

    PAGE_W  = 210
    PAGE_H  = 297
    MARGIN  = 8
    CW      = PAGE_W - MARGIN * 2

    C_BG        = (242, 241, 237)
    C_CARD_BG   = (255, 255, 255)
    C_CARD_HEAD = (250, 249, 246)
    C_BORDER    = (224, 221, 214)
    C_TEXT      = (26,  26,  26)
    C_MUTED     = (170, 170, 170)
    C_LABEL     = (150, 150, 150)

    COLOR_MAP = {
        '与野':    (67, 160,  71), '川口':    (245, 127,  23),
        '巣鴨':    (230,  81,   0),'イイダ':  ( 55,  71,  79),
        '高島平':  ( 13,  71, 161),'東天紅':  (106,  27, 154),
        'ハナマサ':(136,  14,  79),
    }
    EARLY_COLORS = {
        'リネン': (13, 71, 161), 'リネン対面': (10, 48, 96),
        '洗濯':   (51, 105, 30), 'カゴ回収':   (40, 53, 147),
    }
    YONO_BADGE = {
        'early_shift': ((91, 33, 182), '早番'),
        'spot':        (( 7, 89, 133), 'スポット'),
    }

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    pdf.set_auto_page_break(auto=False)
    font = _load_font(pdf)

    def sf(size): pdf.set_font(font if font else 'Helvetica', size=size)
    def t(s): return s if font else s.encode('ascii', 'replace').decode()
    def fill(r, g, b): pdf.set_fill_color(r, g, b)
    def ink(r, g, b):  pdf.set_text_color(r, g, b)
    def draw(r, g, b): pdf.set_draw_color(r, g, b)

    # ページ背景
    fill(*C_BG); pdf.rect(0, 0, PAGE_W, PAGE_H, 'F')

    display_df_pre = df[~df['job_main'].fillna('').isin(EXCLUDED_JOBS)]
    early_df_pre   = display_df_pre[
        display_df_pre['job_early'].notna() & (display_df_pre['job_early'] != '')]

    # ── 高さ推算してスケール計算 ──
    BASE_ROW_H   = 10.0
    BASE_HDR_H   = 10.0
    BASE_SPACING = 1.5
    BASE_SEC_LBL = 6.0
    DATE_H       = 28.0
    FOOTER_H     = 7.0
    AVAIL_H      = PAGE_H - DATE_H - 4 - FOOTER_H

    def _est_h(nrows): return BASE_HDR_H + nrows * BASE_ROW_H + BASE_SPACING

    need = BASE_SEC_LBL
    for job in MAIN_JOBS_ORDER:
        if job == '与野':
            n = len(display_df_pre[display_df_pre['job_main'] == '与野'])
            if n > 0:
                need += _est_h(n) + (BASE_HDR_H + BASE_SPACING) * 2
        else:
            n = len(display_df_pre[display_df_pre['job_main'] == job])
            if n > 0: need += _est_h(n)
    grp_early = {}
    for _, r in early_df_pre.iterrows():
        grp_early[r['job_early']] = grp_early.get(r['job_early'], 0) + 1
    if grp_early:
        need += BASE_SEC_LBL
        for n in grp_early.values(): need += _est_h(n)

    scale    = (AVAIL_H / need) if need > 0 else 1.0
    ROW_H    = BASE_ROW_H   * scale
    CARD_HDR = BASE_HDR_H   * scale
    SPACING  = BASE_SPACING * scale
    SEC_LBL  = BASE_SEC_LBL * scale
    LEFT_BAR = 4.0

    # ── 日付ヘッダー（WebUI スタイル）──
    is_we    = _is_weekend(target_date)
    day_type = '休日' if is_we else '平日'

    sf(22); ink(*C_TEXT)
    date_str = f"{target_date.month:02d}  /  {target_date.day:02d}"
    pdf.set_xy(MARGIN, 4)
    pdf.cell(CW * 0.62, 14, t(date_str), align='L')

    sf(7); ink(*C_MUTED)
    pdf.set_xy(MARGIN + CW * 0.62, 5)
    pdf.cell(CW * 0.38, 5, str(target_date.year), align='L')

    sf(11); ink(*C_TEXT)
    pdf.set_xy(MARGIN + CW * 0.62, 11)
    pdf.cell(CW * 0.38, 7, t(f'{wd}曜日'), align='L')

    # 右端: 平日/休日バッジ
    is_we = _is_weekend(target_date)
    bx, bw_badge, bh_badge = PAGE_W - MARGIN - 18, 18, 6
    if is_we:
        fill(254, 242, 242); draw(251, 147, 147)
        ink(192, 57, 43)
    else:
        fill(232, 240, 254); draw(195, 214, 253)
        ink(26, 86, 219)
    pdf.set_draw_color(*((251,147,147) if is_we else (195,214,253)))
    pdf.rect(bx, 6, bw_badge, bh_badge, 'FD')
    sf(6)
    pdf.set_xy(bx, 7)
    pdf.cell(bw_badge, bh_badge - 2, t(day_type), align='C')

    # 区切り線
    draw(*C_BORDER)
    pdf.line(MARGIN, DATE_H, MARGIN + CW, DATE_H)
    pdf.set_y(DATE_H + 4)

    if df.empty:
        sf(11); ink(*C_MUTED); pdf.set_x(MARGIN)
        pdf.cell(CW, 10, t('データがありません。'), align='C')
        return bytes(pdf.output())

    display_df = display_df_pre

    # ── セクションラベル ──
    def draw_section_label(label_txt):
        y = pdf.get_y()
        sf(6); ink(*C_LABEL)
        pdf.set_xy(MARGIN, y)
        pdf.cell(CW, SEC_LBL * 0.7, t(label_txt), align='L')
        draw(*C_BORDER)
        pdf.line(MARGIN, y + SEC_LBL * 0.75, MARGIN + CW, y + SEC_LBL * 0.75)
        pdf.set_y(y + SEC_LBL)

    # ── カード描画 ──
    def draw_card(label, rows, ar, ag, ab,
                  hide_early=False, header_badge=None):
        if not rows: return
        card_x = MARGIN
        head_y = pdf.get_y()

        # カードヘッダー（WebUI: 薄背景 + 左ボーダー + 案件名大）
        fill(*C_CARD_HEAD); draw(*C_BORDER)
        pdf.rect(card_x, head_y, CW, CARD_HDR, 'FD')
        fill(ar, ag, ab)
        pdf.rect(card_x, head_y, LEFT_BAR, CARD_HDR, 'F')

        # 案件名
        name_fs = max(8, int(CARD_HDR * 0.75))
        sf(name_fs); ink(*C_TEXT)
        pdf.set_xy(card_x + LEFT_BAR + 3, head_y + (CARD_HDR - name_fs * 0.35) / 2)
        pdf.cell(CW * 0.55, name_fs * 0.35 + 1, t(label), align='L')

        # ヘッダーバッジ
        if header_badge:
            hb_text, hb_r, hb_g, hb_b = header_badge
            bh = CARD_HDR * 0.6; bw = 16.0
            lw = pdf.get_string_width(label) + 3
            bx = card_x + LEFT_BAR + 3 + lw
            fill(hb_r, hb_g, hb_b); ink(255, 255, 255); sf(6)
            pdf.set_xy(bx, head_y + (CARD_HDR - bh) / 2)
            pdf.cell(bw, bh, t(hb_text), fill=True, align='C')

        # 右: 人数
        sf(7); ink(*C_MUTED)
        pdf.set_xy(card_x, head_y + (CARD_HDR - 4) / 2)
        pdf.cell(CW - 3, 4, t(f'{len(rows)}'), align='R')

        pdf.set_y(head_y + CARD_HDR)

        # ドライバー行
        tag_h  = ROW_H * 0.5
        drv_fs = max(8, int(ROW_H * 0.72))

        for i, row in enumerate(rows):
            row_y = pdf.get_y()
            fill(255, 255, 255); draw(*C_BORDER)
            pdf.rect(card_x, row_y, CW, ROW_H, 'FD')
            fill(ar, ag, ab)
            pdf.rect(card_x, row_y, LEFT_BAR, ROW_H, 'F')

            x_cur = card_x + LEFT_BAR + 3

            # 早朝タグ
            if row.get('job_early') and not hide_early:
                fill(30, 64, 175); ink(255, 255, 255); sf(6)
                tw = pdf.get_string_width(row['job_early']) + 4
                pdf.set_xy(x_cur, row_y + (ROW_H - tag_h) / 2)
                pdf.cell(tw, tag_h, t(row['job_early']), fill=True, align='C')
                x_cur += tw + 2

            # ドライバー名
            sf(drv_fs); ink(*C_TEXT)
            pdf.set_xy(x_cur, row_y + (ROW_H - drv_fs * 0.35) / 2)
            pdf.cell(110, drv_fs * 0.35 + 1, t(row['driver']), align='L')

            # 右端バッジ（右から順に配置）
            rx = card_x + CW - 2
            def put_badge(label_b, br, bg_c, bb):
                nonlocal rx
                sf(6); bw = pdf.get_string_width(label_b) + 5
                rx -= bw + 1
                fill(br, bg_c, bb); ink(255, 255, 255)
                pdf.set_xy(rx, row_y + (ROW_H - tag_h) / 2)
                pdf.cell(bw, tag_h, t(label_b), fill=True, align='C')

            if row.get('special_flag'):     put_badge('特殊',  185, 28, 28)
            if row.get('yokonori_flag'):    put_badge('横乗り',180, 83,  9)

            # 与野タイプバッジ
            yt = row.get('yono_type', 'normal')
            if not yt or yt == 'normal':
                yt = driver_configs.get(row['driver'], 'normal')
            if yt in YONO_BADGE:
                (br, bg_c, bb), lbl = YONO_BADGE[yt]
                put_badge(lbl, br, bg_c, bb)

            pdf.set_y(row_y + ROW_H)

        pdf.set_y(head_y + CARD_HDR + len(rows) * ROW_H + SPACING)

    # ── 与野タイプ判定 ──
    def _get_yt(row):
        v = row.get('yono_type', 'normal')
        return v if v and v != 'normal' else driver_configs.get(row['driver'], 'normal')

    # ── 案件別稼働 ──
    draw_section_label('案件別稼働')
    for job in MAIN_JOBS_ORDER:
        job_rows = display_df[display_df['job_main'] == job].to_dict('records')
        accent   = COLOR_MAP.get(job, (80, 80, 80))
        if not job_rows: continue

        if job == '与野':
            spot_rows   = [r for r in job_rows if _get_yt(r) == 'spot']
            early_rows  = [r for r in job_rows if _get_yt(r) == 'early_shift']
            normal_rows = [r for r in job_rows if _get_yt(r) == 'normal']
            if spot_rows:
                draw_card('与野', spot_rows, *accent, hide_early=True,
                          header_badge=('スポット', 7, 89, 133))
            if early_rows:
                draw_card('与野', early_rows, *accent, hide_early=True,
                          header_badge=('早番', 91, 33, 182))
            if normal_rows:
                draw_card('与野', normal_rows, *accent, hide_early=True)
        else:
            draw_card(job, job_rows, *accent)

    # ── 早朝案件 ──
    early_df = display_df[
        display_df['job_early'].notna() & (display_df['job_early'] != '')]
    if not early_df.empty:
        draw_section_label('早朝案件')
        for ej, grp in early_df.groupby('job_early'):
            ec = EARLY_COLORS.get(ej, (13, 71, 161))
            draw_card(ej, grp.to_dict('records'), *ec, hide_early=True)

    # ── フッター ──
    fy = PAGE_H - FOOTER_H
    draw(*C_BORDER)
    pdf.line(MARGIN, fy, MARGIN + CW, fy)
    sf(6); ink(*C_MUTED)
    pdf.set_xy(MARGIN, fy + 2)
    pdf.cell(CW, 4, t(f'出力  {datetime.now().strftime("%Y.%m.%d  %H:%M")}'), align='R')

    return bytes(pdf.output())


# ────────────────────────────────────────────────
# タブ: 稼働確認
# ────────────────────────────────────────────────

def tab_view():
    available = db.get_available_dates()

    col1, col2 = st.columns(2)
    with col1:
        if st.button('⚡ 明日の稼働', use_container_width=True):
            st.session_state['view_date'] = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    with col2:
        if st.button('📅 今日の稼働', use_container_width=True):
            st.session_state['view_date'] = date.today().strftime('%Y-%m-%d')

    if available:
        min_d = datetime.strptime(min(available), '%Y-%m-%d').date()
        max_d = datetime.strptime(max(available), '%Y-%m-%d').date()
    else:
        min_d = date.today()
        max_d = date.today() + timedelta(days=31)

    default_val = date.today() + timedelta(days=1)
    if 'view_date' in st.session_state:
        try:
            default_val = datetime.strptime(st.session_state['view_date'], '%Y-%m-%d').date()
        except Exception:
            pass
    # min/max の範囲内にクランプ
    default_val = max(min_d, min(max_d, default_val))

    col_date, col_btn = st.columns([3, 1])
    with col_date:
        selected = st.date_input('', value=default_val,
                                 min_value=min_d, max_value=max_d,
                                 format='YYYY/MM/DD',
                                 label_visibility='collapsed')
    with col_btn:
        if st.button('表示', use_container_width=True):
            st.session_state['view_date'] = selected.strftime('%Y-%m-%d')

    view_date_str = st.session_state.get(
        'view_date', (date.today() + timedelta(days=1)).strftime('%Y-%m-%d'))

    render_shift_view(view_date_str)

    st.markdown('<hr class="thin-divider">', unsafe_allow_html=True)
    st.markdown('<div class="section-label">ダウンロード</div>', unsafe_allow_html=True)
    img_data = generate_day_image(view_date_str)
    st.download_button('🖼️ 画像として保存', data=img_data,
                       file_name=f'shift_{view_date_str}.png', mime='image/png',
                       disabled=not img_data, use_container_width=True,
                       key='dl_view')


# ────────────────────────────────────────────────
# タブ: ドライバー設定
# ────────────────────────────────────────────────

def tab_settings():
    st.caption('ドライバーごとに「与野」案件のタイプを設定します。PDFの色検出が優先されます。')

    drivers = db.get_all_known_drivers()
    if not drivers:
        st.markdown("""
        <div class="empty-state">
          <div class="empty-icon">⚙️</div>
          <p>シフトデータがありません。<br>先にPDFをアップロードしてください。</p>
        </div>""", unsafe_allow_html=True)
        return

    current_configs = db.get_all_driver_configs()

    with st.form('driver_config_form'):
        st.markdown('<div class="section-label">与野タイプ 固定設定</div>', unsafe_allow_html=True)
        new_configs = {}
        for driver in drivers:
            current_type = current_configs.get(driver, 'normal')
            col_name, col_sel = st.columns([2, 2])
            with col_name:
                st.markdown(
                    f'<div style="display:flex;align-items:center;padding:0.45rem 0;">'
                    f'<span style="font-weight:600;font-size:0.95rem;color:#1e293b;">{driver}</span></div>',
                    unsafe_allow_html=True,
                )
            with col_sel:
                selected_type = st.selectbox(
                    label=driver,
                    options=YONO_TYPE_OPTIONS,
                    index=YONO_TYPE_OPTIONS.index(current_type),
                    format_func=lambda x: YONO_TYPE_LABELS[x],
                    key=f'cfg_{driver}',
                    label_visibility='collapsed',
                )
            new_configs[driver] = selected_type

        st.markdown('<div style="height:0.5rem;"></div>', unsafe_allow_html=True)
        submitted = st.form_submit_button('💾 設定を保存', use_container_width=True)

    if submitted:
        db.save_driver_configs_bulk(new_configs)
        st.success('✅ 設定を保存しました。')
        st.rerun()


# ────────────────────────────────────────────────
# タブ: アップロード
# ────────────────────────────────────────────────

def tab_upload():
    st.markdown("""
    <div class="upload-hint">
      <strong>月初にシフトPDFをアップロード</strong><br>
      同月のデータが既にある場合は上書きされます
    </div>""", unsafe_allow_html=True)

    today = date.today()
    col1, col2 = st.columns(2)
    with col1:
        sel_year  = st.selectbox('年', [today.year, today.year + 1], index=0)
    with col2:
        sel_month = st.selectbox('月', list(range(1, 13)), index=today.month - 1)

    year_month = f'{sel_year}-{sel_month:02d}'
    uploaded   = st.file_uploader('PDFファイルを選択', type=['pdf'], label_visibility='collapsed')

    if uploaded is not None:
        st.info(f'📎 {uploaded.name}  ({uploaded.size:,} bytes)')

        col_dbg, col_parse = st.columns([1, 1])
        with col_dbg:
            debug_mode = st.checkbox('生テーブル確認')
        with col_parse:
            do_parse = st.button('🔍 解析開始', use_container_width=True)

        if debug_mode:
            pdf_bytes_dbg = uploaded.read()
            with st.spinner('読み取り中...'):
                raw = parser.debug_raw_table(pdf_bytes_dbg)
            if raw:
                st.caption('生テーブル（先頭5行）')
                st.dataframe(pd.DataFrame(raw[:5]), use_container_width=True)
            else:
                st.warning('テーブルを検出できませんでした。')
            uploaded.seek(0)

        if do_parse:
            with st.spinner('PDFを解析中...'):
                try:
                    pdf_bytes = uploaded.read()
                    shifts    = parser.parse_pdf(pdf_bytes, year_month)

                    if not shifts:
                        st.error('シフトデータを抽出できませんでした。\n'
                                 '「生テーブル確認」でテーブルを確認してください。')
                        return

                    upload_id = str(uuid.uuid4())
                    db.save_shifts(shifts, upload_id, year_month)
                    db.save_upload_record(upload_id, uploaded.name, year_month, len(shifts))
                    st.success(f'✅ 解析完了  {len(shifts):,} 件を保存しました')

                    sdf = pd.DataFrame(shifts)
                    if not sdf.empty:
                        st.dataframe(
                            sdf.groupby('date').size().reset_index(name='件数'),
                            use_container_width=True, height=280,
                        )
                except Exception as e:
                    st.error(f'解析中にエラーが発生しました:\n{e}')

    st.markdown('<hr class="thin-divider">', unsafe_allow_html=True)
    st.markdown('<div class="section-label">アップロード履歴</div>', unsafe_allow_html=True)
    history = db.get_upload_history()
    if history.empty:
        st.caption('アップロード履歴はありません。')
    else:
        st.dataframe(
            history[['filename', 'year_month', 'record_count', 'uploaded_at']].rename(columns={
                'filename': 'ファイル名', 'year_month': '対象年月',
                'record_count': '件数', 'uploaded_at': 'アップロード日時',
            }),
            use_container_width=True, hide_index=True,
        )


# ────────────────────────────────────────────────
# タブ: ダウンロード
# ────────────────────────────────────────────────

def tab_download():
    available = db.get_available_dates()
    if not available:
        st.markdown("""
        <div class="empty-state">
          <div class="empty-icon">📂</div>
          <p>ダウンロードできるデータがありません。</p>
        </div>""", unsafe_allow_html=True)
        return

    months_available = sorted(set(d[:7] for d in available), reverse=True)
    sel_month = st.selectbox('対象年月', months_available)

    dates_in_month = sorted([d for d in available if d.startswith(sel_month)])
    if dates_in_month:
        st.markdown('<div class="section-label">日付を選んで画像ダウンロード</div>', unsafe_allow_html=True)
        sel_date = st.selectbox('日付', dates_in_month, key='dl_date')
        img_data = generate_day_image(sel_date)
        st.download_button('🖼️ 画像として保存', data=img_data,
                           file_name=f'shift_{sel_date}.png', mime='image/png',
                           disabled=not img_data, use_container_width=True,
                           key='dl_download')

    st.markdown('<hr class="thin-divider">', unsafe_allow_html=True)
    st.markdown(f'<div class="section-label">{sel_month} データ一覧</div>', unsafe_allow_html=True)
    df = db.get_all_shifts_for_month(sel_month)
    if df.empty:
        st.caption('データがありません。')
    else:
        df['special_flag'] = df['special_flag'].map({0: '', 1: '特殊'})
        df.columns = ['ドライバー', '日付', 'メイン案件', '早朝案件', '特殊フラグ']
        st.dataframe(df, use_container_width=True, hide_index=True)


# ────────────────────────────────────────────────
# メインエントリポイント
# ────────────────────────────────────────────────

def _check_password() -> bool:
    """パスワードゲート。認証済みなら True を返す。"""
    if st.session_state.get('_authenticated'):
        return True

    st.markdown("""
    <style>
    .login-wrap {
        max-width: 360px;
        margin: 5rem auto 0;
        padding: 2.5rem 2rem;
        background: #fff;
        border: 1px solid #e8e6df;
        border-top: 4px solid #1a1a1a;
        border-radius: 4px;
    }
    .login-wrap h2 {
        font-size: 1.1rem;
        font-weight: 700;
        letter-spacing: 0.1em;
        color: #1a1a1a;
        margin: 0 0 0.3rem;
    }
    .login-wrap p {
        font-size: 0.78rem;
        color: #aaa;
        margin: 0 0 1.5rem;
        letter-spacing: 0.05em;
    }
    </style>
    <div class="login-wrap">
      <h2>SHIFT  SYSTEM</h2>
      <p>Logistics · Operations</p>
    </div>
    """, unsafe_allow_html=True)

    pwd = st.text_input('パスワード', type='password', placeholder='パスワードを入力')
    if st.button('ログイン', use_container_width=True):
        try:
            correct = st.secrets['APP_PASSWORD']
        except Exception:
            correct = os.environ.get('APP_PASSWORD', 'shift2026')
        if pwd == correct:
            st.session_state['_authenticated'] = True
            st.rerun()
        else:
            st.error('パスワードが違います')
    return False


def main():
    setup_page()

    if not _check_password():
        return

    db.init_db()

    today = date.today()
    wd_today = WEEKDAY_JA[today.weekday()]
    st.markdown(f"""
    <div class="app-header">
      <div class="brand">Logistics · Operations</div>
      <h1>シフト確認</h1>
      <div class="today-line">TODAY &nbsp; {today.year}.{today.month:02d}.{today.day:02d} &nbsp; {wd_today}曜日</div>
    </div>""", unsafe_allow_html=True)

    tabs = st.tabs(['稼働確認', '設定', 'アップロード', 'ダウンロード'])
    with tabs[0]: tab_view()
    with tabs[1]: tab_settings()
    with tabs[2]: tab_upload()
    with tabs[3]: tab_download()


if __name__ == '__main__':
    main()
