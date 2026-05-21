"""SQLite database layer for CCAnalyser."""

import re
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

import pandas as pd

from stmtforge.utils.config import load_config, resolve_path
from stmtforge.utils.logging_config import get_logger

logger = get_logger("database")


class Database:
    """SQLite database for storing transactions and metadata."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            config = load_config()
            db_path = config["database"]["path"]
        self.db_path = resolve_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialize database tables."""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount REAL NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('debit', 'credit')),
                    category TEXT DEFAULT 'Others',
                    bank TEXT NOT NULL,
                    card_name TEXT,
                    card_last4 TEXT,
                    source_file TEXT,
                    file_hash TEXT,
                    balance REAL,
                    reward_points REAL,
                    statement_received_date TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    txn_hash TEXT UNIQUE
                );

                CREATE TABLE IF NOT EXISTS statements_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT UNIQUE NOT NULL,
                    original_path TEXT,
                    unlocked_path TEXT,
                    bank TEXT,
                    card_name TEXT,
                    email_date TEXT,
                    email_subject TEXT,
                    filename TEXT,
                    sender TEXT,
                    message_id TEXT,
                    card_last4 TEXT,
                    statement_period_start TEXT,
                    statement_period_end TEXT,
                    transaction_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    processed_at TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS gmail_messages (
                    message_id TEXT PRIMARY KEY,
                    sender TEXT,
                    email_date TEXT,
                    email_subject TEXT,
                    status TEXT,
                    processed_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS pipeline_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS extraction_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT NOT NULL,
                    filename TEXT,
                    extraction_method TEXT,
                    raw_text TEXT,
                    llm_raw_output TEXT,
                    cleaned_json TEXT,
                    transaction_count INTEGER DEFAULT 0,
                    confidence_score REAL DEFAULT 0.0,
                    llm_model TEXT,
                    error_message TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
                CREATE INDEX IF NOT EXISTS idx_txn_bank ON transactions(bank);
                CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);
                CREATE INDEX IF NOT EXISTS idx_txn_type ON transactions(type);
                CREATE INDEX IF NOT EXISTS idx_txn_file_hash ON transactions(file_hash);
                CREATE INDEX IF NOT EXISTS idx_stmt_file_hash ON statements_metadata(file_hash);
                CREATE INDEX IF NOT EXISTS idx_extlog_file_hash ON extraction_log(file_hash);
            """)

            # Migration: add new columns if DB already exists
            self._migrate_add_column(conn, "transactions", "card_name", "TEXT")
            self._migrate_add_column(conn, "transactions", "reward_points", "REAL")
            self._migrate_add_column(conn, "transactions", "statement_received_date", "TEXT")
            self._migrate_add_column(conn, "statements_metadata", "card_name", "TEXT")
            self._migrate_add_column(conn, "statements_metadata", "email_subject", "TEXT")
            self._migrate_add_column(conn, "statements_metadata", "filename", "TEXT")
            self._migrate_add_column(conn, "statements_metadata", "total_amount_due", "REAL")
            self._migrate_add_column(conn, "statements_metadata", "total_spends", "REAL")
            self._migrate_add_column(conn, "statements_metadata", "total_credits", "REAL")
            self._migrate_add_column(conn, "gmail_messages", "email_subject", "TEXT")

            # Create indexes on new columns (after migration)
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_card_name ON transactions(card_name)")
            except sqlite3.OperationalError:
                pass

        logger.debug("Database initialized")

    _ALLOWED_MIGRATE_TABLES = frozenset({
        "transactions", "statements_metadata", "gmail_messages",
        "pipeline_state", "extraction_log",
    })

    def _migrate_add_column(self, conn, table: str, column: str, col_type: str):
        """Add a column to a table if it doesn't already exist."""
        if table not in self._ALLOWED_MIGRATE_TABLES:
            raise ValueError(f"_migrate_add_column: disallowed table '{table}'")
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            logger.info(f"Migrated: added {column} to {table}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # ── Transaction methods ──────────────────────────────────────

    def insert_transactions(self, df: pd.DataFrame, bank: str, source_file: str,
                            file_hash: str, card_name: str = None,
                            reward_points: float = None,
                            statement_received_date: str = None) -> int:
        """
        Insert transactions from a DataFrame. Uses txn_hash for deduplication.
        Returns number of new rows inserted.
        """
        if df.empty:
            return 0

        inserted = 0
        with self._get_conn() as conn:
            for _, row in df.iterrows():
                # Generate unique hash for this transaction
                txn_hash = self._txn_hash(
                    row["date"], row["description"], row["amount"],
                    row["type"], bank, row.get("card_last4", ""),
                )

                # Per-row card_name overrides file-level card_name
                row_card_name = row.get("card_name") or card_name
                row_reward_pts = row.get("reward_points") or reward_points

                try:
                    conn.execute("""
                        INSERT INTO transactions
                        (date, description, amount, type, category, bank,
                         card_name, card_last4, source_file, file_hash,
                         balance, reward_points, statement_received_date, txn_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(txn_hash) DO UPDATE SET
                            card_name = excluded.card_name,
                            category = excluded.category
                    """, (
                        row["date"],
                        row["description"],
                        row["amount"],
                        row["type"],
                        row.get("category", "Others"),
                        bank,
                        row_card_name,
                        row.get("card_last4"),
                        source_file,
                        file_hash,
                        row.get("balance"),
                        row_reward_pts,
                        statement_received_date,
                        txn_hash,
                    ))
                    if conn.total_changes:
                        inserted += 1
                except sqlite3.IntegrityError:
                    pass  # Duplicate, skip

        logger.info(f"Inserted {inserted}/{len(df)} transactions from {source_file}")
        return inserted

    def _txn_hash(self, date: str, description: str, amount: float,
                  txn_type: str, bank: str, card_last4: str) -> str:
        """Generate a unique hash for a transaction."""
        key = f"{date}|{description}|{amount:.2f}|{txn_type}|{bank}|{card_last4 or ''}"
        return hashlib.sha256(key.encode()).hexdigest()

    def get_transactions(self, filters: dict = None) -> pd.DataFrame:
        """Query transactions with optional filters. Returns DataFrame."""
        query = "SELECT * FROM transactions WHERE 1=1"
        params = []

        if filters:
            if filters.get("date_from"):
                query += " AND date >= ?"
                params.append(filters["date_from"])
            if filters.get("date_to"):
                query += " AND date <= ?"
                params.append(filters["date_to"])
            if filters.get("bank"):
                if isinstance(filters["bank"], list):
                    placeholders = ",".join("?" * len(filters["bank"]))
                    query += f" AND bank IN ({placeholders})"
                    params.extend(filters["bank"])
                else:
                    query += " AND bank = ?"
                    params.append(filters["bank"])
            if filters.get("category"):
                if isinstance(filters["category"], list):
                    placeholders = ",".join("?" * len(filters["category"]))
                    query += f" AND category IN ({placeholders})"
                    params.extend(filters["category"])
                else:
                    query += " AND category = ?"
                    params.append(filters["category"])
            if filters.get("type"):
                query += " AND type = ?"
                params.append(filters["type"])
            if filters.get("amount_min"):
                query += " AND amount >= ?"
                params.append(filters["amount_min"])
            if filters.get("amount_max"):
                query += " AND amount <= ?"
                params.append(filters["amount_max"])
            if filters.get("search"):
                query += " AND description LIKE ?"
                params.append(f"%{filters['search']}%")
            if filters.get("card_last4"):
                query += " AND card_last4 = ?"
                params.append(filters["card_last4"])
            if filters.get("card_name"):
                if isinstance(filters["card_name"], list):
                    placeholders = ",".join("?" * len(filters["card_name"]))
                    query += f" AND card_name IN ({placeholders})"
                    params.extend(filters["card_name"])
                else:
                    query += " AND card_name = ?"
                    params.append(filters["card_name"])

        query += " ORDER BY date DESC"

        with self._get_conn() as conn:
            df = pd.read_sql_query(query, conn, params=params)

        return df

    def get_summary(self) -> dict:
        """Get summary statistics."""
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            total_spend = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='debit'"
            ).fetchone()[0]
            banks = [r[0] for r in conn.execute(
                "SELECT DISTINCT bank FROM transactions"
            ).fetchall()]
            categories = [r[0] for r in conn.execute(
                "SELECT DISTINCT category FROM transactions"
            ).fetchall()]
            date_range = conn.execute(
                "SELECT MIN(date), MAX(date) FROM transactions"
            ).fetchone()

        return {
            "total_transactions": total,
            "total_spend": total_spend,
            "banks": banks,
            "categories": categories,
            "date_range": {
                "start": date_range[0] if date_range else None,
                "end": date_range[1] if date_range else None,
            },
        }

    def get_date_anchor_options(self) -> dict:
        """Get date anchors for dashboard date-range defaults/options."""
        with self._get_conn() as conn:
            period_end_rows = conn.execute(
                """
                SELECT statement_period_end
                FROM statements_metadata
                WHERE statement_period_end IS NOT NULL
                  AND TRIM(statement_period_end) != ''
                """
            ).fetchall()

            latest_statement_end = None
            if period_end_rows:
                parsed_dates = pd.to_datetime(
                    [r[0] for r in period_end_rows],
                    errors="coerce",
                    dayfirst=True,
                )
                parsed_dates = parsed_dates.dropna()
                if not parsed_dates.empty:
                    latest_statement_end = parsed_dates.max().strftime("%Y-%m-%d")

            latest_statement_received = conn.execute(
                """
                SELECT MAX(COALESCE(date(email_date), date(created_at)))
                FROM statements_metadata
                WHERE status = 'completed'
                """
            ).fetchone()[0]

            txn_min = conn.execute("SELECT MIN(date) FROM transactions").fetchone()[0]

        return {
            "latest_statement_end_date": latest_statement_end,
            "latest_statement_received_date": latest_statement_received,
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "transaction_min_date": txn_min,
        }

    def get_monthly_spend(self) -> pd.DataFrame:
        """Get monthly spend aggregation."""
        query = """
            SELECT
                strftime('%Y-%m', date) as month,
                SUM(CASE WHEN type='debit' THEN amount ELSE 0 END) as total_debit,
                SUM(CASE WHEN type='credit' THEN amount ELSE 0 END) as total_credit,
                COUNT(*) as transaction_count
            FROM transactions
            GROUP BY month
            ORDER BY month
        """
        with self._get_conn() as conn:
            return pd.read_sql_query(query, conn)

    def get_category_spend(self, date_from: str = None, date_to: str = None) -> pd.DataFrame:
        """Get spending by category."""
        query = "SELECT category, SUM(amount) as total, COUNT(*) as count FROM transactions WHERE type='debit'"
        params = []
        if date_from:
            query += " AND date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND date <= ?"
            params.append(date_to)
        query += " GROUP BY category ORDER BY total DESC"

        with self._get_conn() as conn:
            return pd.read_sql_query(query, conn, params=params)

    def get_merchant_spend(self, date_from: str = None, date_to: str = None,
                           limit: int = 20) -> pd.DataFrame:
        """Get top merchants by spend."""
        query = "SELECT description as merchant, SUM(amount) as total, COUNT(*) as count FROM transactions WHERE type='debit'"
        params = []
        if date_from:
            query += " AND date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND date <= ?"
            params.append(date_to)
        query += " GROUP BY description ORDER BY total DESC LIMIT ?"
        params.append(limit)

        with self._get_conn() as conn:
            return pd.read_sql_query(query, conn, params=params)

    def get_daily_spend(self, date_from: str = None, date_to: str = None) -> pd.DataFrame:
        """Get daily spend for heatmap."""
        query = "SELECT date, SUM(amount) as total FROM transactions WHERE type='debit'"
        params = []
        if date_from:
            query += " AND date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND date <= ?"
            params.append(date_to)
        query += " GROUP BY date ORDER BY date"

        with self._get_conn() as conn:
            return pd.read_sql_query(query, conn, params=params)

    def get_banks(self) -> list:
        """Get list of distinct banks."""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT DISTINCT bank FROM transactions ORDER BY bank").fetchall()
        return [r[0] for r in rows]

    def get_categories(self) -> list:
        """Get list of distinct categories."""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT DISTINCT category FROM transactions ORDER BY category").fetchall()
        return [r[0] for r in rows]

    def get_cards(self) -> list:
        """Get list of distinct card_last4 values."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT card_last4 FROM transactions WHERE card_last4 IS NOT NULL ORDER BY card_last4"
            ).fetchall()
        return [r[0] for r in rows]

    def get_card_names(self) -> list:
        """Get list of distinct card names."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT card_name FROM transactions WHERE card_name IS NOT NULL ORDER BY card_name"
            ).fetchall()
        return [r[0] for r in rows]

    # ── Statement-level analysis methods ────────────────────────

    # Regex patterns to extract a date from common CC statement filenames.
    # Each pattern must have named groups: year, month (and optionally day).
    _MONTH_ABBR = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    _FILENAME_DATE_PATTERNS = [
        # SBI:  6805786524927007_15042026.pdf → DDMMYYYY → Apr 2026
        re.compile(r"_(\d{2})(\d{2})(\d{4})(?:_\d+)?\.pdf$", re.IGNORECASE),
        # HDFC: 5268XXXXXXXXXX38_19-03-2026_177.pdf → DD-MM-YYYY → Mar 2026
        re.compile(r"_(\d{2})-(\d{2})-(\d{4})(?:_\d+)?\.pdf$", re.IGNORECASE),
        # IDFC: <account>_25102025_112046900.pdf → DDMMYYYY → Oct 2025
        # (same first pattern covers this)
        # Federal: CreditCard_Statement_2026022215007394_21-02-2026.pdf → DD-MM-YYYY
        re.compile(r"_(\d{2})-(\d{2})-(\d{4})\.pdf$", re.IGNORECASE),
        # ICICI: Statement_AUG2024_432681569.pdf → MONYYYY → Aug 2024
        re.compile(r"_([A-Za-z]{3})(\d{4})_", re.IGNORECASE),
        # ICICI: Statement_2024MTH05_681569432.pdf → YYYYMTHmm → May 2024
        re.compile(r"_(\d{4})MTH(\d{2})_", re.IGNORECASE),
        # ICICI: Statement_SEP2025_432681569.pdf (same as MON pattern above)
        # YES Bank / others: _2025_09_ or _202509_
        re.compile(r"_(\d{4})(\d{2})(?:_|\.)pdf", re.IGNORECASE),
    ]

    @classmethod
    def _billing_month_from_filename(cls, filename: str | None) -> str | None:
        """Try to extract YYYY-MM from known filename patterns.

        Returns None when the filename doesn't match any known pattern.
        """
        if not filename or not isinstance(filename, str):
            return None
        for i, pat in enumerate(cls._FILENAME_DATE_PATTERNS):
            m = pat.search(filename)
            if not m:
                continue
            try:
                if i <= 2:
                    # DD MM YYYY (groups 1,2,3)
                    yr = int(m.group(3))
                    mm = int(m.group(2))
                    if 1 <= mm <= 12 and 2000 <= yr <= 2100:
                        return f"{yr:04d}-{mm:02d}"
                elif i == 3:
                    # MON YYYY (groups 1=abbr, 2=year)
                    mon_abbr = m.group(1).lower()
                    yr = int(m.group(2))
                    mm = cls._MONTH_ABBR.get(mon_abbr)
                    if mm and 2000 <= yr <= 2100:
                        return f"{yr:04d}-{mm:02d}"
                elif i == 4:
                    # YYYY MTH mm (groups 1=year, 2=month)
                    yr = int(m.group(1))
                    mm = int(m.group(2))
                    if 1 <= mm <= 12 and 2000 <= yr <= 2100:
                        return f"{yr:04d}-{mm:02d}"
                elif i == 5:
                    # YYYY MM (groups 1=year, 2=month)
                    yr = int(m.group(1))
                    mm = int(m.group(2))
                    if 1 <= mm <= 12 and 2000 <= yr <= 2100:
                        return f"{yr:04d}-{mm:02d}"
            except (ValueError, IndexError):
                continue
        return None

    def get_statement_months(self) -> list:
        """Return distinct YYYY-MM values available in statements_metadata.

        Priority order for determining billing month (must match _row_billing_month):
          1. statement_period_start (if set)
          2. Date extracted from filename
          3. email_date
          4. created_at
        """
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    file_hash, bank, filename,
                    NULLIF(TRIM(statement_period_start), '') AS period_start,
                    NULLIF(TRIM(email_date), '') AS email_date,
                    created_at
                FROM statements_metadata
                WHERE status IN ('completed', 'no_data', 'error', 'skipped_irrelevant')
            """).fetchall()

        import pandas as _pd
        months: set[str] = set()
        for r in rows:
            # r: (file_hash, bank, filename, period_start, email_date, created_at)
            # 1. statement_period_start (highest priority — consistent with _row_billing_month)
            if r[3]:
                try:
                    dt = _pd.to_datetime(r[3][:10], errors="coerce")
                    if not _pd.isna(dt):
                        months.add(dt.strftime("%Y-%m"))
                        continue
                except Exception:
                    pass
            # 2. Filename-derived month
            fn_month = self._billing_month_from_filename(r[2])
            if fn_month:
                months.add(fn_month)
                continue
            # 3. email_date
            if r[4]:
                try:
                    dt = _pd.to_datetime(r[4][:10], errors="coerce")
                    if not _pd.isna(dt):
                        months.add(dt.strftime("%Y-%m"))
                        continue
                except Exception:
                    pass
            # 4. created_at
            if r[5]:
                try:
                    dt = _pd.to_datetime(r[5][:10], errors="coerce")
                    if not _pd.isna(dt):
                        months.add(dt.strftime("%Y-%m"))
                except Exception:
                    pass

        return sorted(months, reverse=True)

    def get_statements_for_month(self, year_month: str) -> pd.DataFrame:
        """Load statements_metadata rows that belong to a given YYYY-MM.

        Billing month resolution (priority order):
          1. statement_period_start
          2. Date extracted from filename
          3. email_date
          4. created_at

        Deduplication: when the same logical file (same bank + filename) was
        processed multiple times (different file_hashes), keep only one row per
        (bank, card_name, filename) group, preferring the most-recent
        ``completed`` entry, then other statuses by recency.
        """
        with self._get_conn() as conn:
            all_rows = pd.read_sql_query("""
                SELECT
                    sm.file_hash, sm.bank, sm.card_name, sm.card_last4,
                    sm.filename, sm.email_date, sm.statement_period_start,
                    sm.statement_period_end, sm.transaction_count, sm.status,
                    sm.total_amount_due, sm.total_spends, sm.total_credits,
                    sm.error_message, sm.created_at
                FROM statements_metadata sm
                WHERE sm.status IN ('completed', 'no_data', 'error', 'skipped_irrelevant')
                ORDER BY sm.bank, sm.card_name, sm.email_date, sm.created_at DESC
            """, conn)

        if all_rows.empty:
            return all_rows

        # ── Determine billing month for each row ─────────────────
        def _row_billing_month(row) -> str | None:
            def _safe_str(v):
                """Return stripped string or None for NaN/None/empty."""
                if v is None:
                    return None
                s = str(v).strip()
                return s if s and s.lower() not in ("nan", "none", "nat") else None

            # 1. statement_period_start
            sp = _safe_str(row.get("statement_period_start"))
            if sp:
                try:
                    dt = pd.to_datetime(sp[:10], errors="coerce")
                    if not pd.isna(dt):
                        return dt.strftime("%Y-%m")
                except Exception:
                    pass
            # 2. filename-derived
            fn_month = self._billing_month_from_filename(_safe_str(row.get("filename")))
            if fn_month:
                return fn_month
            # 3. email_date
            ed = _safe_str(row.get("email_date"))
            if ed:
                try:
                    dt = pd.to_datetime(ed[:10], errors="coerce")
                    if not pd.isna(dt):
                        return dt.strftime("%Y-%m")
                except Exception:
                    pass
            # 4. created_at
            ca = _safe_str(row.get("created_at"))
            if ca:
                try:
                    dt = pd.to_datetime(ca[:10], errors="coerce")
                    if not pd.isna(dt):
                        return dt.strftime("%Y-%m")
                except Exception:
                    pass
            return None

        all_rows["_billing_month"] = all_rows.apply(_row_billing_month, axis=1)

        # ── Filter to the requested month ─────────────────────────
        month_rows = all_rows[all_rows["_billing_month"] == year_month].copy()
        month_rows = month_rows.drop(columns=["_billing_month"])

        if month_rows.empty:
            return month_rows

        # ── Deduplicate: keep one row per (bank, card_name, filename) ─
        # Priority: completed > others; within same status, keep most recent.
        STATUS_RANK = {"completed": 0, "no_data": 1, "error": 2, "skipped_irrelevant": 3}
        month_rows["_status_rank"] = month_rows["status"].map(
            lambda s: STATUS_RANK.get(s, 9)
        )
        month_rows["_created_ts"] = pd.to_datetime(
            month_rows["created_at"], errors="coerce"
        )
        month_rows = month_rows.sort_values(
            ["_status_rank", "_created_ts"],
            ascending=[True, False],
        )
        dedup_key = month_rows.apply(
            lambda r: (
                str(r.get("bank") or ""),
                str(r.get("card_name") or ""),
                str(r.get("filename") or ""),
            ),
            axis=1,
        )
        month_rows = month_rows[~dedup_key.duplicated(keep="first")].copy()
        month_rows = month_rows.drop(
            columns=["_status_rank", "_created_ts"], errors="ignore"
        )

        return month_rows.reset_index(drop=True)

    def get_transactions_for_file_hashes(self, file_hashes: list) -> pd.DataFrame:
        """Load all transactions for a list of file_hashes."""
        if not file_hashes:
            return pd.DataFrame()
        placeholders = ",".join("?" * len(file_hashes))
        with self._get_conn() as conn:
            df = pd.read_sql_query(
                f"SELECT * FROM transactions WHERE file_hash IN ({placeholders}) ORDER BY date",
                conn, params=file_hashes,
            )
        return df

    def backfill_statement_metadata(self) -> dict:
        """Backfill card_name, period dates, and financial totals for completed statements
        that have NULL values in those fields, deriving them from existing transactions.
        Returns counts of updated rows."""
        updated_metadata = 0
        updated_summary = 0

        with self._get_conn() as conn:
            # Find completed statements missing at least one derived field
            rows = conn.execute("""
                SELECT sm.file_hash
                FROM statements_metadata sm
                WHERE sm.status = 'completed'
                  AND (
                      sm.card_name IS NULL OR sm.card_name = ''
                      OR sm.statement_period_start IS NULL OR sm.statement_period_start = ''
                      OR sm.statement_period_end IS NULL OR sm.statement_period_end = ''
                      OR sm.total_spends IS NULL
                  )
            """).fetchall()

            for row in rows:
                fhash = row[0]

                # Fetch aggregated data from transactions in one query
                agg = conn.execute("""
                    SELECT
                        MIN(date) as min_date,
                        MAX(date) as max_date,
                        MAX(card_name) as card_name,
                        SUM(CASE WHEN type = 'debit' THEN amount ELSE 0 END) as total_spends,
                        SUM(CASE WHEN type = 'credit' THEN amount ELSE 0 END) as total_credits
                    FROM transactions
                    WHERE file_hash = ?
                """, (fhash,)).fetchone()

                if not agg or agg[0] is None:
                    continue  # No transactions for this statement

                min_date, max_date, card_name, total_spends, total_credits = agg

                # Normalise dates to YYYY-MM-DD
                period_start = None
                period_end = None
                try:
                    period_start = pd.to_datetime(min_date, errors="coerce")
                    period_start = period_start.strftime("%Y-%m-%d") if not pd.isnull(period_start) else None
                except Exception:
                    pass
                try:
                    period_end = pd.to_datetime(max_date, errors="coerce")
                    period_end = period_end.strftime("%Y-%m-%d") if not pd.isnull(period_end) else None
                except Exception:
                    pass

                conn.execute("""
                    UPDATE statements_metadata
                    SET
                        card_name = CASE
                            WHEN (card_name IS NULL OR card_name = '') AND ? IS NOT NULL THEN ?
                            ELSE card_name END,
                        statement_period_start = CASE
                            WHEN (statement_period_start IS NULL OR statement_period_start = '') AND ? IS NOT NULL THEN ?
                            ELSE statement_period_start END,
                        statement_period_end = CASE
                            WHEN (statement_period_end IS NULL OR statement_period_end = '') AND ? IS NOT NULL THEN ?
                            ELSE statement_period_end END
                    WHERE file_hash = ?
                """, (card_name, card_name,
                      period_start, period_start,
                      period_end, period_end,
                      fhash))
                if conn.total_changes:
                    updated_metadata += 1

                conn.execute("""
                    UPDATE statements_metadata
                    SET
                        total_spends = CASE WHEN total_spends IS NULL AND ? > 0 THEN ? ELSE total_spends END,
                        total_credits = CASE WHEN total_credits IS NULL AND ? > 0 THEN ? ELSE total_credits END
                    WHERE file_hash = ?
                """, (total_spends, total_spends,
                      total_credits, total_credits,
                      fhash))
                if conn.total_changes:
                    updated_summary += 1

        return {"metadata_updated": updated_metadata, "summary_updated": updated_summary}

    def export_attachment_metadata_csv(self, output_path: str) -> str:
        """Export all statement metadata to CSV."""
        with self._get_conn() as conn:
            df = pd.read_sql_query(
                "SELECT file_hash, original_path, bank, card_name, email_date, "
                "email_subject, filename, sender, message_id, card_last4, "
                "statement_period_start, statement_period_end, transaction_count, "
                "status, processed_at, created_at "
                "FROM statements_metadata ORDER BY email_date DESC",
                conn,
            )
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output, index=False)
        logger.info(f"Exported {len(df)} statement records to {output}")
        return str(output)

    # ── Statement metadata methods ───────────────────────────────

    def record_statement(self, file_hash: str, original_path: str, bank: str,
                         email_date: str = None, sender: str = None,
                         message_id: str = None, card_name: str = None,
                         email_subject: str = None, filename: str = None,
                         **kwargs) -> bool:
        """Record a statement file in metadata. Returns True if new."""
        with self._get_conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO statements_metadata
                    (file_hash, original_path, bank, card_name, email_date,
                     email_subject, filename, sender, message_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_hash) DO UPDATE SET
                        card_name = excluded.card_name,
                        bank = CASE WHEN excluded.bank != 'unknown'
                                    THEN excluded.bank
                                    ELSE statements_metadata.bank END
                """, (file_hash, original_path, bank, card_name, email_date,
                       email_subject, filename, sender, message_id))
                return conn.total_changes > 0
            except sqlite3.IntegrityError:
                return False

    def update_statement_status(self, file_hash: str, status: str,
                                transaction_count: int = 0,
                                error_message: str = None, **kwargs):
        """Update processing status of a statement."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE statements_metadata
                SET status = ?, transaction_count = ?, error_message = ?,
                    processed_at = datetime('now')
                WHERE file_hash = ?
            """, (status, transaction_count, error_message, file_hash))

    def update_statement_summary(self, file_hash: str,
                                 total_amount_due: float = None,
                                 total_spends: float = None,
                                 total_credits: float = None):
        """Store statement financial summary (total due, spends, credits) in metadata."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE statements_metadata
                SET total_amount_due = ?, total_spends = ?, total_credits = ?
                WHERE file_hash = ?
            """, (total_amount_due, total_spends, total_credits, file_hash))

    def update_statement_metadata(self, file_hash: str,
                                   card_name: str = None,
                                   statement_period_start: str = None,
                                   statement_period_end: str = None):
        """Fill in derived metadata fields (card_name, period dates) without overwriting existing values."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE statements_metadata
                SET
                    card_name = CASE
                        WHEN (card_name IS NULL OR card_name = '') AND ? IS NOT NULL THEN ?
                        ELSE card_name END,
                    statement_period_start = CASE
                        WHEN (statement_period_start IS NULL OR statement_period_start = '') AND ? IS NOT NULL THEN ?
                        ELSE statement_period_start END,
                    statement_period_end = CASE
                        WHEN (statement_period_end IS NULL OR statement_period_end = '') AND ? IS NOT NULL THEN ?
                        ELSE statement_period_end END
                WHERE file_hash = ?
            """, (card_name, card_name,
                  statement_period_start, statement_period_start,
                  statement_period_end, statement_period_end,
                  file_hash))

    def is_file_processed(self, file_hash: str) -> bool:
        """Check if a file has already been successfully processed, skipped, or unlock-failed."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM statements_metadata WHERE file_hash = ?",
                (file_hash,)
            ).fetchone()
        return row is not None and row[0] in ("completed", "skipped_irrelevant", "unlock_failed")

    # ── Gmail message tracking ───────────────────────────────────

    def record_message(self, message_id: str, sender: str, email_date: str,
                       status: str, email_subject: str = None):
        """Record a Gmail message as processed."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO gmail_messages
                (message_id, sender, email_date, email_subject, status, processed_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (message_id, sender, email_date, email_subject, status))

    def get_processed_message_ids(self) -> set:
        """Get set of already-processed Gmail message IDs."""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT message_id FROM gmail_messages").fetchall()
        return {r[0] for r in rows}

    # ── Pipeline state ───────────────────────────────────────────

    def get_last_fetch_date(self) -> str | None:
        """Get the date of the last Gmail fetch."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM pipeline_state WHERE key = 'last_fetch_date'"
            ).fetchone()
        return row[0] if row else None

    def update_last_fetch_date(self, date: str):
        """Update the last fetch date."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pipeline_state (key, value, updated_at)
                VALUES ('last_fetch_date', ?, datetime('now'))
            """, (date,))

    def get_pipeline_state(self, key: str) -> str | None:
        """Get a pipeline state value."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM pipeline_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_pipeline_state(self, key: str, value: str):
        """Set a pipeline state value."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pipeline_state (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
            """, (key, value))

    # ── Extraction log methods ───────────────────────────────────

    def store_extraction_log(self, file_hash: str, filename: str,
                             extraction_method: str, raw_text: str,
                             llm_raw_output: str = None,
                             cleaned_json: str = None,
                             transaction_count: int = 0,
                             confidence_score: float = 0.0,
                             llm_model: str = None,
                             error_message: str = None):
        """Store extraction log entry for a processed PDF."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO extraction_log
                (file_hash, filename, extraction_method, raw_text,
                 llm_raw_output, cleaned_json, transaction_count,
                 confidence_score, llm_model, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (file_hash, filename, extraction_method, raw_text,
                  llm_raw_output, cleaned_json, transaction_count,
                  confidence_score, llm_model, error_message))

    def get_extraction_log(self, file_hash: str = None) -> pd.DataFrame:
        """Get extraction log entries, optionally filtered by file_hash."""
        query = "SELECT * FROM extraction_log"
        params = []
        if file_hash:
            query += " WHERE file_hash = ?"
            params.append(file_hash)
        query += " ORDER BY created_at DESC"
        with self._get_conn() as conn:
            return pd.read_sql_query(query, conn, params=params)
