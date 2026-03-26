"""
pdf_parser.py - シフトPDF解析エンジン

解析方式:
  - pdfplumber でテキストを直接抽出（デジタルPDFのためOCR不要・高精度）
  - pdfplumber の矩形情報からセル背景色を取得
  - OCR はフォールバック用として残す

実際のPDFフォーマット (2026.2.pdf 確認済み):
  行 = ドライバー / 列 = 日付（1〜28/31）
  両端列に氏名が重複する構造
  セル例: 与野, 与野2, リ与野, 洗a2与, リハナ, 北戸田, 草加 など
"""
import io
import re
import colorsys

import pdfplumber


# ────────────────────────────────────────────────
# 案件定義
# ────────────────────────────────────────────────

# 早朝案件プレフィックス（長い文字列を先に照合）
EARLY_PREFIXES = [
    ('リネン対面',  'リネン対面'),
    ('リネン2回線', 'リネン2回線'),
    ('リネン',      'リネン'),
    ('リネ対',      'リネン対面'),
    ('リネ2',       'リネン2回線'),
    ('リネ',        'リネン'),
    ('リ',          'リネン'),
    # 洗濯系（表記揺れを全て網羅）
    ('洗濯b2',      '洗濯'),
    ('洗濯b',       '洗濯'),
    ('洗濯a',       '洗濯'),
    ('洗濯',        '洗濯'),
    ('洗a2',        '洗濯'),
    ('洗b2',        '洗濯'),
    ('洗c2',        '洗濯'),
    ('洗a',         '洗濯'),
    ('洗b',         '洗濯'),
    ('洗c',         '洗濯'),
    ('カゴ',        'カゴ回収'),
]

# メイン案件照合パターン（長い文字列を先に照合）
# value は正規化後のジョブ名
MAIN_JOB_PATTERNS = [
    ('高島平横', '高島平'),
    ('高島横',  '高島平'),
    ('高島平',  '高島平'),
    ('東天紅横', '東天紅'),
    ('東天横',  '東天紅'),
    ('東天紅',  '東天紅'),
    ('ハナマサ横','ハナマサ'),
    ('ハナ横',  'ハナマサ'),
    ('ハナマサ','ハナマサ'),
    ('イイダ横', 'イイダ'),
    ('イイダ',  'イイダ'),
    # 与野の各バリアント（数字 / 横 は全て与野に正規化）
    ('与野横',  '与野'),
    ('与野1',   '与野'),
    ('与野2',   '与野'),
    ('与野3',   '与野'),
    ('与野4',   '与野'),
    ('与野',    '与野'),
    # 北戸田は「与野エリアの時間バリアント」として与野に正規化
    ('北戸田',  '与野'),
    ('川口横',  '川口'),
    ('川口',    '川口'),
    ('巣鴨横',  '巣鴨'),
    ('巣鴨',    '巣鴨'),
    ('草加',    '草加'),
    ('ダイオ',  'ダイオ'),
    # 短縮形（早朝プレフィックス除去後の残り文字列に対して照合）
    ('高島',    '高島平'),
    ('東天',    '東天紅'),
    ('ハナ',    'ハナマサ'),
    ('イイ',    'イイダ'),
    # 「与」単独は与野の略称
    ('与',      '与野'),
]

# スキップすべきドライバー行のパターン
_SKIP_ROW_RE = re.compile(
    r'^(氏\s*名|稼働合計|合計|\d+は\d|北戸田は|1は|2は|3は|4は|\s*$)',
)


# ────────────────────────────────────────────────
# テキスト解析
# ────────────────────────────────────────────────

def parse_cell_text(raw: str) -> dict:
    """
    セルテキストを早朝案件 / メイン案件に分解する。

    実際のPDFで確認されたパターン:
      与野, 与野2, リ与野, 洗a与野, 洗a2与, リハナ, カゴ与野,
      洗c東天, 洗aミー, 北戸田, 草加, ダイオ, 洗濯b, リネン等

    Returns:
        {'job_early': str|None, 'job_main': str|None}
    """
    text = re.sub(r'[\s\n\r　]+', '', raw).strip()
    if not text:
        return {'job_early': None, 'job_main': None}

    job_early = None
    job_main  = None
    remaining = text

    # ── 早朝案件プレフィックスを照合 ──
    for prefix, normalized in EARLY_PREFIXES:
        if remaining.startswith(prefix):
            job_early = normalized
            remaining = remaining[len(prefix):]
            break

    # 早朝プレフィックス除去後に先頭の数字があれば除去
    # 例: リ2与野 → リ除去後 "2与野" → "与野"
    remaining = re.sub(r'^\d', '', remaining)

    # ── メイン案件を照合 ──
    for pattern, normalized in MAIN_JOB_PATTERNS:
        if remaining.startswith(pattern):
            job_main = normalized
            break

    # プレフィックスなしで全体照合（フォールバック）
    if not job_main and not job_early:
        for pattern, normalized in MAIN_JOB_PATTERNS:
            if text.startswith(pattern):
                job_main = normalized
                break

    is_yokonori = '横' in text
    return {'job_early': job_early, 'job_main': job_main, 'is_yokonori': is_yokonori}


# ────────────────────────────────────────────────
# セル背景色判定
# ────────────────────────────────────────────────

def _classify_pdf_color(color) -> str:
    """
    PDFの non_stroking_color から案件種別を返す。

    pdfplumber の color 形式:
      - None / 0 / 1  (グレースケール)
      - (R, G, B)     0〜1 スケール
      - (C, M, Y, K)  0〜1 スケール
    """
    if color is None:
        return 'normal'

    # グレースケール
    if isinstance(color, (int, float)):
        return 'normal'  # 白か黒

    if not hasattr(color, '__len__') or len(color) == 0:
        return 'normal'

    if len(color) == 3:
        r, g, b = color
    elif len(color) == 4:
        # CMYK → RGB (近似)
        c, m, y, k = color
        r = (1 - c) * (1 - k)
        g = (1 - m) * (1 - k)
        b = (1 - y) * (1 - k)
    else:
        return 'normal'

    # ほぼ白
    if r > 0.92 and g > 0.92 and b > 0.92:
        return 'normal'

    # HSV に変換
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h_deg = h * 360   # 0-360
    s_pct = s * 100   # 0-100

    if s_pct < 15:
        return 'normal'

    # 緑 / 黄緑 (H=60〜150) → 与野通常
    if 60 <= h_deg <= 150:
        return 'yono_normal'

    # 青 (H=195〜255, 高彩度) → 与野スポット
    # ※ 低彩度(S<50)は土日の列背景色のため除外
    if 195 <= h_deg <= 255 and s_pct > 50:
        return 'yono_spot'

    # 紫 (H=255〜310) → 与野早番
    if 255 < h_deg <= 310:
        return 'yono_early_shift'

    # 濃ピンク (H=310〜359, 高彩度) → 特殊案件
    # ※ 純赤 (H=0) はドライバー行の視覚的な色分けのため除外
    if 310 <= h_deg <= 359 and s_pct > 50:
        return 'special'

    return 'normal'


def _get_colored_rects(page) -> list:
    """ページから着色矩形のリストを取得"""
    result = []
    for rect in page.rects:
        fill = rect.get('non_stroking_color')
        color_type = _classify_pdf_color(fill)
        if color_type == 'normal':
            continue
        result.append({
            'x0':        rect['x0'],
            'top':       rect['top'],
            'x1':        rect['x1'],
            'bottom':    rect['bottom'],
            'color_type': color_type,
        })
    return result


def _color_type_at(x0, top, x1, bottom, colored_rects: list) -> str:
    """
    指定セル座標に対応する着色矩形の種別を返す。
    セル幅の2倍以上ある矩形（行背景・週グループ背景）は除外する。

    着色矩形のグリッドとテーブルセルのグリッドはX座標がずれているため、
    X軸はオーバーラップ判定（矩形中心がセル範囲外でも検出可能）を使用する。
    Y軸はセル上部20%のマージンを設け、上のヘッダー行の矩形が
    データ行にはみ出すケース（フォールスポジティブ）を除外する。

    複数の矩形がマッチした場合は優先度の高い種別を返す。
    優先度: special > yono_early_shift > yono_spot > yono_normal
    """
    # 優先度テーブル
    PRIORITY = {'special': 4, 'yono_early_shift': 3, 'yono_spot': 2, 'yono_normal': 1, 'normal': 0}

    cell_w = max(x1 - x0, 1)
    cell_h = max(bottom - top, 1)
    # ヘッダー行の矩形が上からはみ出すケースを除外するため、Y上部に20%のマージン
    y_min = top + 0.2 * cell_h

    best = 'normal'
    best_pri = 0
    for r in colored_rects:
        # X: オーバーラップ判定（矩形グリッドとセルグリッドのずれを吸収）
        if r['x1'] <= x0 or r['x0'] >= x1:
            continue
        # Y: 矩形中心がセル内（上部20%マージンを考慮）
        rcy = (r['top'] + r['bottom']) / 2
        if not (y_min <= rcy <= bottom):
            continue
        # セル幅の2倍以上広い矩形は行/週の背景色として除外
        if (r['x1'] - r['x0']) > cell_w * 2:
            continue
        pri = PRIORITY.get(r['color_type'], 0)
        if pri > best_pri:
            best_pri = pri
            best = r['color_type']
    return best


# ────────────────────────────────────────────────
# テーブル解析
# ────────────────────────────────────────────────

def _find_date_header(table: list) -> tuple[int, dict]:
    """
    テーブルから日付ヘッダー行を探し (行インデックス, {列→日}) を返す。
    """
    for i, row in enumerate(table[:6]):
        if not row:
            continue
        temp = {}
        for j, cell in enumerate(row):
            if not cell:
                continue
            s = re.sub(r'[\s　]+', '', str(cell))
            m = re.fullmatch(r'(\d{1,2})', s)
            if m:
                day = int(m.group(1))
                if 1 <= day <= 31:
                    temp[j] = day
        if len(temp) >= 15:
            return i, temp
    return -1, {}


def _is_driver_name(text: str) -> bool:
    """テキストがドライバー名として有効か判定"""
    if not text or len(text) < 2:
        return False
    if _SKIP_ROW_RE.match(text):
        return False
    # 数字のみ / 記号のみはスキップ
    if re.match(r'^[\d０-９\s\u3000\-\+\*\/]+$', text):
        return False
    # 注釈行スキップ（「〜は〜分着車」など）
    if re.search(r'着車|分着|走\)', text):
        return False
    return True


def _parse_table_page(page, year_month: str) -> list:
    """1ページを解析してシフトリストを返す"""
    table_settings = {
        'vertical_strategy':   'lines',
        'horizontal_strategy': 'lines',
        'snap_tolerance':       5,
        'join_tolerance':       3,
        'edge_min_length':     10,
    }

    # テキストテーブル抽出
    table = page.extract_table(table_settings)
    if not table:
        return []

    # 日付ヘッダー行を特定
    header_idx, col_to_day = _find_date_header(table)
    if not col_to_day:
        return []

    # 着色矩形を収集
    colored_rects = _get_colored_rects(page)

    # ────────────────────────────────────────────────
    # 着色矩形グリッドを使った (driver, day) → color_type マッピング
    #
    # PDFの着色矩形グリッドはテーブルセルのX座標グリッドとは別物:
    #   着色矩形グリッド: x0_start=68.4, 列幅=23.35px (col0=ドライバー名欄, col1=1日, col2=2日...)
    #   テーブルセルグリッド: x0_start=46.6, 列幅=13.5px (完全に異なるX座標)
    # → X座標でのオーバーラップ判定は機能しない
    # → 代わりに着色矩形のX座標から日付を計算し、Y座標からドライバー行を特定する
    # ────────────────────────────────────────────────
    RECT_X0_START  = 68.4
    RECT_COL_WIDTH = 23.35
    PRIORITY = {'special': 4, 'yono_early_shift': 3, 'yono_spot': 2, 'yono_normal': 1, 'normal': 0}

    driver_day_color = {}  # (driver, day) -> color_type

    try:
        # 着色矩形をY帯でグループ化（同じ行の矩形をまとめる）
        # colN の実際の日付は N+1（col0=day1, col1=day2, ...）
        band_map = {}  # rounded_top -> {'top':, 'bottom':, 'rects':[]}
        for r in colored_rects:
            if r['color_type'] not in ('yono_early_shift', 'yono_spot', 'special'):
                continue
            band_key = round(r['top'])
            if band_key not in band_map:
                band_map[band_key] = {'top': r['top'], 'bottom': r['bottom'], 'rects': []}
            band_map[band_key]['rects'].append(r)
            band_map[band_key]['bottom'] = max(band_map[band_key]['bottom'], r['bottom'])

        # テーブルのドライバー名リスト（マッチング用）
        driver_list = []
        for i, row in enumerate(table):
            if i <= header_idx:
                continue
            if not row or not row[0]:
                continue
            d = re.sub(r'[\s\n\r　]+', '', str(row[0]))
            if _is_driver_name(d):
                driver_list.append(d)

        for band in sorted(band_map.values(), key=lambda b: b['top']):
            # このY帯のドライバー名をwith_bboxで抽出
            try:
                txt = page.within_bbox(
                    (0, band['top'] - 0.5, 150, band['bottom'] + 0.5)
                ).extract_text() or ''
            except Exception:
                txt = ''
            txt_clean = re.sub(r'[\s\n\r　]+', '', txt)

            # ドライバー名をマッチング（完全一致 → 先頭1文字欠け → 先頭2文字欠け）
            matched_driver = None
            for driver in driver_list:
                if driver in txt_clean:
                    matched_driver = driver
                    break
                if len(driver) >= 2 and driver[1:] in txt_clean:
                    matched_driver = driver
                    break
                if len(driver) >= 3 and driver[2:] in txt_clean:
                    matched_driver = driver
                    break
            if matched_driver is None:
                continue

            for r in band['rects']:
                # 着色矩形のX座標から列インデックスを計算
                # col0=day1, col1=day2, ..., colN=day(N+1)
                rect_col = round((r['x0'] - RECT_X0_START) / RECT_COL_WIDTH)
                if rect_col < 0 or rect_col >= 31:
                    continue
                day = rect_col + 1  # col0→day1, col1→day2 ...

                key = (matched_driver, day)
                existing = driver_day_color.get(key)
                if existing is None or PRIORITY.get(r['color_type'], 0) > PRIORITY.get(existing, 0):
                    driver_day_color[key] = r['color_type']
    except Exception:
        pass

    # データ行の解析
    shifts = []
    for i, row in enumerate(table):
        if i <= header_idx:
            continue
        if not row:
            continue

        # ドライバー名（0列目）
        raw_driver = str(row[0]).strip() if row[0] else ''
        driver = re.sub(r'[\s\n\r　]+', '', raw_driver)
        if not _is_driver_name(driver):
            continue

        for col_j, day in col_to_day.items():
            if col_j >= len(row) or not row[col_j]:
                continue

            cell_text = re.sub(r'[\n\r]+', '', str(row[col_j])).strip()
            if not cell_text:
                continue

            # 案件解析
            job_info = parse_cell_text(cell_text)
            if not job_info['job_main'] and not job_info['job_early']:
                continue

            # セル色を判定（driver_day_color マッピングを使用）
            special_flag = 0
            yono_type    = 'normal'
            ct = driver_day_color.get((driver, day), 'normal')
            if ct == 'special':
                special_flag = 1
            elif ct == 'yono_spot' and job_info.get('job_main') == '与野':
                yono_type = 'spot'
            elif ct == 'yono_early_shift' and job_info.get('job_main') == '与野':
                yono_type = 'early_shift'

            try:
                full_date = f"{year_month}-{day:02d}"
            except Exception:
                continue

            shifts.append({
                'driver':        driver,
                'date':          full_date,
                'job_main':      job_info['job_main'],
                'job_early':     job_info['job_early'],
                'special_flag':  special_flag,
                'yono_type':     yono_type,
                'yokonori_flag': 1 if job_info.get('is_yokonori') else 0,
            })

    return shifts


# ────────────────────────────────────────────────
# メインエントリポイント
# ────────────────────────────────────────────────

def parse_pdf(pdf_bytes: bytes, year_month: str) -> list:
    """
    PDFバイト列を解析してシフトデータリストを返す。

    Args:
        pdf_bytes  : PDFファイルのバイト列
        year_month : 対象年月 (例: '2026-02')

    Returns:
        [{'driver', 'date', 'job_main', 'job_early', 'special_flag'}, ...]
    """
    all_shifts = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_shifts = _parse_table_page(page, year_month)
            all_shifts.extend(page_shifts)

    # 重複除去（同一ドライバー×日付の最初のレコードを採用）
    seen = set()
    unique = []
    for s in all_shifts:
        key = (s['driver'], s['date'])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return unique


# ────────────────────────────────────────────────
# デバッグ用ユーティリティ
# ────────────────────────────────────────────────

def debug_raw_table(pdf_bytes: bytes) -> list:
    """
    pdfplumber が抽出した生テーブルを返す（解析精度確認用）。
    Streamlit の st.dataframe() に渡して確認できる。
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        table = page.extract_table({
            'vertical_strategy':   'lines',
            'horizontal_strategy': 'lines',
        })
    return table or []
