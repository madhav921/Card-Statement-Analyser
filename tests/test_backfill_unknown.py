import sqlite3
import sys
import types

import pandas as pd

from stmtforge.database.db import Database


# run_pipeline imports PDFUnlocker -> pikepdf at module import time.
# Stub pikepdf for this DB-only unit test.
if "pikepdf" not in sys.modules:
    sys.modules["pikepdf"] = types.SimpleNamespace(PasswordError=Exception, open=None)

from stmtforge.run_pipeline import backfill_unknown_bank_rows


def _insert_unknown_transaction(db: Database, source_file: str, idx: int):
    df = pd.DataFrame([
        {
            "date": f"2026-02-{19 + idx:02d}",
            "description": f"TEST MERCHANT {idx}",
            "amount": 399.0 + idx,
            "type": "debit",
            "card_name": None,
            "card_last4": None,
            "balance": None,
            "reward_points": None,
        }
    ])
    db.insert_transactions(
        df,
        bank="unknown",
        source_file=source_file,
        file_hash=f"hash_{source_file}",
    )


def _insert_unknown_statement(db: Database, filename: str):
    db.record_statement(
        file_hash=f"stmt_{filename}",
        original_path=f"data/raw_pdfs/unknown/2026_02/{filename}",
        bank="unknown",
        filename=filename,
    )


def test_backfill_unknown_bank_rows_updates_bank_and_card(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(db_path=str(db_path))

    _insert_unknown_transaction(db, "Statement.pdf", 0)
    _insert_unknown_transaction(db, "Credit Card Statement.pdf", 1)
    _insert_unknown_transaction(db, "CreditCard_Statement_2026022215007394_21-02-2026.pdf", 2)

    _insert_unknown_statement(db, "Statement.pdf")
    _insert_unknown_statement(db, "Credit Card Statement.pdf")
    _insert_unknown_statement(db, "CreditCard_Statement_2026022215007394_21-02-2026.pdf")

    result = backfill_unknown_bank_rows(db)

    assert result["transactions"] >= 3
    assert result["statements"] >= 3

    with sqlite3.connect(str(db_path)) as conn:
        tx_rows = conn.execute(
            "SELECT source_file, bank, card_name FROM transactions ORDER BY source_file"
        ).fetchall()
        st_rows = conn.execute(
            "SELECT filename, bank, card_name FROM statements_metadata ORDER BY filename"
        ).fetchall()

    tx_map = {row[0]: (row[1], row[2]) for row in tx_rows}
    st_map = {row[0]: (row[1], row[2]) for row in st_rows}

    assert tx_map["Statement.pdf"] == ("csb", "Edge")
    assert tx_map["Credit Card Statement.pdf"] == ("axis", "Neo Rupay")
    assert tx_map["CreditCard_Statement_2026022215007394_21-02-2026.pdf"] == ("federal", "Signet")

    assert st_map["Statement.pdf"] == ("csb", "Edge")
    assert st_map["Credit Card Statement.pdf"] == ("axis", "Neo Rupay")
    assert st_map["CreditCard_Statement_2026022215007394_21-02-2026.pdf"] == ("federal", "Signet")
