"""
debug_cells.py - セル座標と着色矩形の詳細マッピング
"""
import sys
import io
import pdfplumber
import pdf_parser

def run_debug(pdf_path: str, year_month: str):
    with open(pdf_path, 'rb') as f:
        pdf_bytes = f.read()

    table_settings = {
        'vertical_strategy':   'lines',
        'horizontal_strategy': 'lines',
        'snap_tolerance':       5,
        'join_tolerance':       3,
        'edge_min_length':     10,
    }

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        colored_rects = pdf_parser._get_colored_rects(page)

        # 着色矩形をday列ごとに整理（x座標から日付推定）
        # rect grid: col0=68.4, 各列=23.4px
        def rect_col(rx0):
            return round((rx0 - 68.4) / 23.35)

        print("=== 着色矩形のY行ごとのearly_shift/spot (X→日付) ===")
        rect_rows = {}
        for r in colored_rects:
            if r['color_type'] in ('yono_early_shift', 'yono_spot'):
                ry = round(r['top'], 1)
                if ry not in rect_rows:
                    rect_rows[ry] = []
                col = rect_col(r['x0'])
                rect_rows[ry].append((col, r['color_type'], r['x0'], r['x1'], r['top'], r['bottom']))

        for ry in sorted(rect_rows.keys()):
            items = rect_rows[ry]
            print(f"  y≈{ry:.1f}: ", end="")
            for col, ct, x0, x1, t, b in sorted(items):
                short = 'E' if ct == 'yono_early_shift' else 'S'
                print(f"col{col}({short}) ", end="")
            print()

        # テーブルセルのY座標を取得
        print("\n=== テーブルのセル座標 (row_i, col_j→top,bottom,left,right) ===")
        tf = page.debug_tablefinder(table_settings)
        if tf and tf.cells:
            unique_tops  = sorted(set(c[0] for c in tf.cells))
            unique_lefts = sorted(set(c[1] for c in tf.cells))
            print(f"  行数: {len(unique_tops)}, 列数: {len(unique_lefts)}")
            print("  行のY座標:")
            for ri, ty in enumerate(unique_tops[:20]):
                # find bottom for this row
                bots = [c[2] for c in tf.cells if c[0] == ty]
                print(f"    row{ri}: y=[{ty:.1f}, {max(bots):.1f}]")

            print("\n  列のX座標 (最初の10列):")
            for ci, lx in enumerate(unique_lefts[:15]):
                rights = [c[3] for c in tf.cells if c[1] == lx]
                print(f"    col{ci}: x=[{lx:.1f}, {max(rights):.1f}]")

        # 表テキスト抽出でドライバー名と行インデックスの対応
        print("\n=== ドライバー名→表の行インデックス ===")
        table = page.extract_table(table_settings)
        if table:
            for i, row in enumerate(table):
                if row and row[0]:
                    name = str(row[0]).strip().replace('\n','').replace(' ','')
                    if len(name) >= 2 and not name.isdigit():
                        # find Y range for this row
                        if tf and tf.cells:
                            row_tops = sorted(set(c[0] for c in tf.cells))
                            if i < len(row_tops):
                                ty = row_tops[i]
                                bots = [c[2] for c in tf.cells if c[0] == ty]
                                print(f"  row{i} {name}: y=[{ty:.1f},{max(bots):.1f}]")
                        else:
                            print(f"  row{i} {name}")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("使い方: python3 debug_cells.py <pdf_path> <year_month>")
        sys.exit(1)
    run_debug(sys.argv[1], sys.argv[2])
