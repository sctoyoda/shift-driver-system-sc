"""
debug_colors.py - 色検出デバッグスクリプト

使い方:
  python3 debug_colors.py 2026.3.pdf 2026-03
"""
import sys
import io
import colorsys
import pdfplumber
import pdf_parser

def run_debug(pdf_path: str, year_month: str):
    with open(pdf_path, 'rb') as f:
        pdf_bytes = f.read()

    # 全シフトを解析
    shifts = pdf_parser.parse_pdf(pdf_bytes, year_month)
    shift_map = {(s['driver'], s['date']): s for s in shifts}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            print(f"\n{'='*60}")
            print(f"PAGE {page_num+1}")
            print(f"{'='*60}")

            colored_rects = pdf_parser._get_colored_rects(page)
            print(f"\n[着色矩形一覧] count={len(colored_rects)}")
            for r in colored_rects:
                print(f"  {r['color_type']:20s} x=[{r['x0']:.1f},{r['x1']:.1f}] y=[{r['top']:.1f},{r['bottom']:.1f}]")

            print(f"\n[ターゲット日付の結果]")
            target_dates = [
                f"{year_month}-02", f"{year_month}-03", f"{year_month}-04",
                f"{year_month}-05", f"{year_month}-06",
                f"{year_month}-15", f"{year_month}-16",
            ]
            for s in shifts:
                if s['date'] in target_dates and s['job_main'] == '与野':
                    marker = ''
                    if s['yono_type'] in ('early_shift', 'spot'):
                        marker = ' ★'
                    print(f"  {s['date']} {s['driver']:10s} yono_type={s['yono_type']}{marker}")

    print("\n[全与野ドライバーのyono_type (早番/スポットのみ)]")
    for s in shifts:
        if s['job_main'] == '与野' and s['yono_type'] in ('early_shift', 'spot'):
            print(f"  {s['date']} {s['driver']:10s} {s['yono_type']}")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("使い方: python3 debug_colors.py <pdf_path> <year_month>")
        print("例:    python3 debug_colors.py 2026.3.pdf 2026-03")
        sys.exit(1)
    run_debug(sys.argv[1], sys.argv[2])
