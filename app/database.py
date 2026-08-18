import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "replenishment.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS styles (
    style_code TEXT PRIMARY KEY,
    lifecycle TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS style_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    style_code TEXT NOT NULL,
    platform TEXT NOT NULL,             -- amazon / walmart / other / temu
    account_name TEXT NOT NULL,         -- e.g. HW / PS / ZO
    account_prefix TEXT NOT NULL,       -- first 2 letters, e.g. HW
    stocking_days INTEGER NOT NULL,
    quota REAL,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(style_code, platform, account_name)
);

CREATE TABLE IF NOT EXISTS sales_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                 -- yyyymmdd
    sku TEXT NOT NULL,
    platform TEXT NOT NULL,             -- amazon / walmart / other / temu
    account TEXT,                       -- amazon account prefix or NULL
    quantity REAL NOT NULL,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_sku_date ON sales_raw(sku, date);
CREATE INDEX IF NOT EXISTS idx_sales_platform ON sales_raw(platform, account);

CREATE TABLE IF NOT EXISTS inventory_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    platform TEXT NOT NULL,             -- amazon / walmart / other / temu
    account TEXT,                       -- amazon account prefix or NULL
    warehouse TEXT,
    quantity REAL NOT NULL,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_sku ON inventory_raw(sku);
CREATE INDEX IF NOT EXISTS idx_inv_platform ON inventory_raw(platform, account);

CREATE TABLE IF NOT EXISTS unproduced_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    style_code TEXT NOT NULL,
    sku TEXT NOT NULL,
    batch_name TEXT NOT NULL,
    quantity REAL NOT NULL,
    uploaded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_unprod_style_sku ON unproduced_batches(style_code, sku);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    baseline_date TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS report_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    style_code TEXT,
    color TEXT,
    lifecycle TEXT,
    stocking_days INTEGER,
    denoised_total_sales REAL,
    final_forecast_total REAL,
    fba_total REAL,
    china_total REAL,
    batch_quantities TEXT,              -- JSON {batch_name: qty}
    sellable_days TEXT,                 -- JSON [day0, day1, ...]
    hw_need REAL,
    ps_need REAL,
    zo_need REAL,
    other_need REAL,
    temu_need REAL,
    walmart_need REAL,
    total_need REAL,
    available_inventory REAL,
    total_planned_order REAL,
    current_order_quantity REAL,
    manual_final_forecast REAL,
    UNIQUE(report_id, sku)
);

CREATE TABLE IF NOT EXISTS report_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    style_code TEXT,
    color TEXT,
    platform TEXT NOT NULL,             -- amazon / walmart / other / temu
    account TEXT,                       -- amazon prefix or NULL
    stocking_days INTEGER,
    d3 REAL,
    d7 REAL,
    d14 REAL,
    d30 REAL,
    d60 REAL,
    denoised_sales REAL,
    historical_sales REAL,
    historical_days INTEGER,
    historical_daily REAL,
    forecast_sales REAL,
    final_forecast REAL,
    forecast_qty REAL,
    inventory_qty REAL,
    need_qty REAL,
    sellable_days REAL,
    manual_final_forecast REAL,
    UNIQUE(report_id, sku, platform, account)
);
"""

# 增量迁移：老库缺列时自动补齐
MIGRATIONS = {
    "report_detail": [
        ("color", "TEXT"),
        ("historical_days", "INTEGER"),
        ("historical_daily", "REAL"),
    ],
    "report_summary": [
        ("historical_daily_total", "REAL"),
    ],
}


def _migrate(conn):
    cur = conn.cursor()
    for table, cols in MIGRATIONS.items():
        cur.execute(f"PRAGMA table_info({table})")
        existing = {r[1] for r in cur.fetchall()}
        for name, coltype in cols:
            if name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
    conn.commit()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    conn.close()


def get_conn():
    return sqlite3.connect(DB_PATH, timeout=30.0)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
