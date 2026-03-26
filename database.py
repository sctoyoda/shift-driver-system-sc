"""
database.py - SQLite データベース操作
"""
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime

# Fly.io の永続ボリューム(/data)があればそちらを使う
_data_dir = Path("/data") if Path("/data").exists() else Path(__file__).parent
DB_PATH = _data_dir / "shift_data.db"


def init_db():
    """データベースとテーブルを初期化する"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            driver      TEXT    NOT NULL,
            date        TEXT    NOT NULL,
            job_main    TEXT,
            job_early   TEXT,
            special_flag INTEGER DEFAULT 0,
            upload_id   TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS uploads (
            id           TEXT PRIMARY KEY,
            filename     TEXT,
            year_month   TEXT,
            record_count INTEGER,
            uploaded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ドライバーごとの与野タイプ設定（固定スポット用）
    # yono_type: 'normal'=通常 / 'spot'=スポット / 'early_shift'=早番
    c.execute('''
        CREATE TABLE IF NOT EXISTS driver_config (
            driver      TEXT PRIMARY KEY,
            yono_type   TEXT NOT NULL DEFAULT 'normal',
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # shifts に yono_type 列を追加（既存テーブルへの移行対応）
    try:
        c.execute("ALTER TABLE shifts ADD COLUMN yono_type TEXT DEFAULT 'normal'")
    except Exception:
        pass
    # shifts に yokonori_flag 列を追加（既存テーブルへの移行対応）
    try:
        c.execute("ALTER TABLE shifts ADD COLUMN yokonori_flag INTEGER DEFAULT 0")
    except Exception:
        pass

    conn.commit()
    conn.close()


def save_shifts(shifts_data: list, upload_id: str, year_month: str):
    """
    シフトデータを保存する。
    同月のデータが既に存在する場合は上書きする。
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 同月の既存データを削除（再アップロード対応）
    c.execute("DELETE FROM shifts WHERE date LIKE ?", (f"{year_month}%",))

    for shift in shifts_data:
        c.execute('''
            INSERT INTO shifts (driver, date, job_main, job_early, special_flag, yono_type, yokonori_flag, upload_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            shift['driver'],
            shift['date'],
            shift.get('job_main'),
            shift.get('job_early'),
            int(shift.get('special_flag', 0)),
            shift.get('yono_type', 'normal'),
            int(shift.get('yokonori_flag', 0)),
            upload_id,
        ))

    conn.commit()
    conn.close()


def save_upload_record(upload_id: str, filename: str, year_month: str, record_count: int):
    """アップロード履歴を保存する"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO uploads (id, filename, year_month, record_count)
        VALUES (?, ?, ?, ?)
    ''', (upload_id, filename, year_month, record_count))
    conn.commit()
    conn.close()


def get_shifts_by_date(target_date: str) -> pd.DataFrame:
    """指定日のシフトを全件取得する"""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM shifts WHERE date = ? ORDER BY driver",
        conn,
        params=(target_date,),
    )
    conn.close()
    return df


def get_available_dates() -> list:
    """データが存在する日付の一覧を返す"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT date FROM shifts ORDER BY date")
    dates = [row[0] for row in c.fetchall()]
    conn.close()
    return dates


def get_upload_history() -> pd.DataFrame:
    """アップロード履歴を返す"""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM uploads ORDER BY uploaded_at DESC",
        conn,
    )
    conn.close()
    return df


def get_all_shifts_for_month(year_month: str) -> pd.DataFrame:
    """指定月の全シフトを返す（CSVダウンロード用）"""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT driver, date, job_main, job_early, special_flag "
        "FROM shifts WHERE date LIKE ? ORDER BY date, driver",
        conn,
        params=(f"{year_month}%",),
    )
    conn.close()
    return df


def delete_month_data(year_month: str):
    """指定月のデータを全削除する"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM shifts WHERE date LIKE ?", (f"{year_month}%",))
    c.execute("DELETE FROM uploads WHERE year_month = ?", (year_month,))
    conn.commit()
    conn.close()


# ────────────────────────────────────────────────
# ドライバー設定
# ────────────────────────────────────────────────

def get_all_driver_configs() -> dict:
    """全ドライバーの設定を返す。{driver: yono_type}"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT driver, yono_type FROM driver_config")
    result = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    return result


def save_driver_config(driver: str, yono_type: str):
    """ドライバーの与野タイプを保存する"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO driver_config (driver, yono_type, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(driver) DO UPDATE SET
            yono_type  = excluded.yono_type,
            updated_at = CURRENT_TIMESTAMP
    ''', (driver, yono_type))
    conn.commit()
    conn.close()


def save_driver_configs_bulk(configs: dict):
    """ドライバー設定を一括保存する。{driver: yono_type}"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for driver, yono_type in configs.items():
        c.execute('''
            INSERT INTO driver_config (driver, yono_type, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(driver) DO UPDATE SET
                yono_type  = excluded.yono_type,
                updated_at = CURRENT_TIMESTAMP
        ''', (driver, yono_type))
    conn.commit()
    conn.close()


def get_all_known_drivers() -> list:
    """シフトデータに存在する全ドライバー名を返す"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT driver FROM shifts ORDER BY driver")
    drivers = [row[0] for row in c.fetchall()]
    conn.close()
    return drivers
