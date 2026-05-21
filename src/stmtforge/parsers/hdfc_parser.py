"""HDFC Bank credit card statement parser."""

import re
from pathlib import Path

import pandas as pd

from stmtforge.parsers.base_parser import BaseParser, parse_date, parse_amount, detect_debit_credit
from stmtforge.utils.logging_config import get_logger

logger = get_logger("parsers.hdfc")


class HDFCParser(BaseParser):
    BANK_NAME = "hdfc"

    # HDFC statement transaction line pattern:
    # DD/MM/YYYY  Description  Amount [Cr]
    TRANSACTION_RE = re.compile(
        r"(\d{2}/\d{2}/\d{4})\s+"   # Date DD/MM/YYYY
        r"(.+?)\s+"                   # Description
        r"(\d[\d,]*\.\d{2})\s*"      # Amount
        r"(Cr)?\s*$",                 # Optional Cr indicator
        re.IGNORECASE,
    )

    # Alternative pattern for HDFC (some formats)
    TRANSACTION_RE2 = re.compile(
        r"(\d{2}\s+[A-Za-z]{3}\s+\d{2,4})\s+"  # Date: DD Mon YY/YYYY
        r"(.+?)\s+"                               # Description
        r"(\d[\d,]*\.\d{2})\s*"                  # Amount
        r"(Cr)?\s*$",                             # Optional Cr
        re.IGNORECASE,
    )

    # Newer HDFC format (Aug 2025+): "DD/MM/YYYY| HH:MM DESCRIPTION [+ ]C AMOUNT [l]"
    # Credits have a leading + before the C currency symbol; debits have just C.
    # Groups: (1) date, (2) description, (3) '+' or None (credit marker), (4) amount
    TRANSACTION_RE3 = re.compile(
        r"(\d{2}/\d{2}/\d{4})\|\s*\d{2}:\d{2}\s+"  # Date: DD/MM/YYYY| HH:MM
        r"(.+?)\s+"                                   # Description (lazy)
        r"(\+)?\s*[Cc]\s+"                            # Optional + (credit), then C (currency symbol)
        r"(\d[\d,]*\.\d{2})\s*[A-Za-z]?\s*$",       # Amount, optional trailing char (PI indicator)
        re.IGNORECASE,
    )

    def parse(self, pdf_path: str | Path) -> pd.DataFrame:
        pdf_path = Path(pdf_path)
        logger.info(f"HDFC parsing: {pdf_path.name}")

        full_text = self.extractor.extract_text(pdf_path)
        card_last4 = self.extractor.detect_card_last4(full_text)
        ref_year = self._detect_year(full_text)

        # Try table-based extraction first
        records = self._parse_tables(pdf_path, card_last4, ref_year)

        # Fall back to text-based extraction
        if not records:
            records = self._parse_text(full_text, card_last4, ref_year)

        df = self._get_standard_df(records)
        logger.info(f"HDFC extracted {len(df)} transactions from {pdf_path.name}")
        return df

    def _parse_tables(self, pdf_path: Path, card_last4: str, ref_year: int) -> list:
        tables = self.extractor.extract_tables(pdf_path)
        records = []

        for table_info in tables:
            table = table_info["data"]
            if not table or len(table) < 2:
                continue

            for row in table[1:]:
                if not row:
                    continue

                # Newer HDFC format: single-cell rows containing full transaction text
                if len(row) == 1 and row[0]:
                    cell_text = str(row[0]).strip()
                    m = self.TRANSACTION_RE3.search(cell_text)
                    if m:
                        date = parse_date(m.group(1), ref_year)
                        description = m.group(2).strip()
                        amount = parse_amount(m.group(4))  # group(4) = amount
                        txn_type = "credit" if m.group(3) else "debit"  # group(3) = '+' or None
                        if date and amount and amount > 0 and len(description) > 2:
                            records.append({
                                "date": date,
                                "description": description,
                                "amount": amount,
                                "type": txn_type,
                                "card_last4": card_last4,
                                "balance": None,
                            })
                    continue

                if len(row) < 3:
                    continue

                # Older HDFC tables: Date, Description, Amount columns
                date = parse_date(str(row[0] or ""), ref_year)
                if not date:
                    continue

                description = str(row[1] or "").strip() if len(row) > 1 else ""
                if not description or len(description) < 3:
                    continue

                # Amount might be in column 2 or 3
                amount = None
                txn_type = "debit"
                for col_idx in range(2, min(len(row), 5)):
                    cell = str(row[col_idx] or "")
                    amt = parse_amount(cell)
                    if amt and amt > 0:
                        amount = amt
                        txn_type = detect_debit_credit(cell, description)
                        break

                if amount:
                    records.append({
                        "date": date,
                        "description": description,
                        "amount": amount,
                        "type": txn_type,
                        "card_last4": card_last4,
                        "balance": None,
                    })

        return records

    def _parse_text(self, text: str, card_last4: str, ref_year: int) -> list:
        records = []
        lines = text.split("\n")

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Try newer format first (date with pipe+time)
            m = self.TRANSACTION_RE3.search(line)
            if m:
                date = parse_date(m.group(1), ref_year)
                description = m.group(2).strip()
                amount = parse_amount(m.group(4))  # group(4) = amount
                txn_type = "credit" if m.group(3) else "debit"  # group(3) = '+' or None
                if date and amount and amount > 0 and len(description) > 2:
                    records.append({
                        "date": date,
                        "description": description,
                        "amount": amount,
                        "type": txn_type,
                        "card_last4": card_last4,
                        "balance": None,
                    })
                continue

            # Try older formats (date without time)
            for pattern in [self.TRANSACTION_RE, self.TRANSACTION_RE2]:
                match = pattern.search(line)
                if match:
                    date_str, description, amount_str, cr_flag = match.groups()
                    date = parse_date(date_str, ref_year)
                    amount = parse_amount(amount_str)

                    if date and amount and amount > 0 and len(description.strip()) > 2:
                        txn_type = "credit" if cr_flag else "debit"
                        records.append({
                            "date": date,
                            "description": description.strip(),
                            "amount": amount,
                            "type": txn_type,
                            "card_last4": card_last4,
                            "balance": None,
                        })
                    break

        return records

    def _detect_year(self, text: str) -> int:
        years = re.findall(r"20[12]\d", text)
        if years:
            from collections import Counter
            return int(Counter(years).most_common(1)[0][0])
        return 2024
