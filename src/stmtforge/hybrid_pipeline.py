"""
Hybrid PDF Transaction Extraction Pipeline.

Combines deterministic bank parsers with multi-stage text extraction + LLM
structuring + validation for maximum extraction coverage.

Flow:
  1. Try deterministic bank-specific parser first (if enabled)
  2. If parser fails or returns too few results:
     a. Extract raw text (table → layout → OCR fallback)
     b. Send to LLM for JSON structuring (Primary → Hard Mode fallback)
     c. Validate and clean LLM output
  3. Categorize transactions
  4. Store in database with extraction log
"""

import json
from pathlib import Path

import pandas as pd

from stmtforge.database.db import Database
from stmtforge.extractor.core import PDFTextExtractor
from stmtforge.llm.client import OllamaClient
from stmtforge.llm.prompts import PRIMARY_PROMPT, HARD_MODE_PROMPT, VALIDATION_PROMPT
from stmtforge.parsers.categorizer import Categorizer
from stmtforge.parsers.registry import get_parser
from stmtforge.pdf_processing.extractor import PDFExtractor
from stmtforge.utils.config import load_config
from stmtforge.utils.hashing import file_hash
from stmtforge.utils.logging_config import get_logger
from stmtforge.validator.transaction_validator import TransactionValidator

logger = get_logger("hybrid_pipeline")


class HybridPipeline:
    """Orchestrates deterministic + LLM-based transaction extraction."""

    def __init__(self, db: Database = None, config: dict = None):
        self.config = config or load_config()
        self.db = db or Database()
        self.categorizer = Categorizer()

        # Sub-components
        llm_config = self.config.get("llm", {})
        extraction_config = self.config.get("extraction", {})
        ocr_config = self.config.get("ocr", {})

        self.text_extractor = PDFTextExtractor({
            "table_min_rows": extraction_config.get("table_min_rows", 10),
            "ocr_enabled": ocr_config.get("enabled", False),
        })
        self.llm_client = OllamaClient(llm_config)
        self.validator = TransactionValidator()
        self.pdf_extractor = PDFExtractor()  # For card detection

        self.use_deterministic = extraction_config.get("use_deterministic_first", True)
        self.store_raw = extraction_config.get("store_raw_text", True)
        self.store_llm = extraction_config.get("store_llm_output", True)
        self.llm_enabled = llm_config.get("enabled", True)

        card_identifiers = self.config.get("parsers", {}).get("card_identifiers", {})
        self.card_identifiers = card_identifiers

    def process_pdf(self, pdf_path: str, bank: str = "unknown",
                    fhash: str = None, card_name: str = None,
                    email_date: str = None, **meta) -> dict:
        """
        Process a single PDF through the hybrid pipeline.

        Returns dict with:
          - transactions: list[dict]
          - df: pd.DataFrame (ready for DB)
          - method: str (how extraction succeeded)
          - confidence: float
          - transaction_count: int
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            logger.error(f"PDF not found: {pdf_path}")
            return self._empty_result("file_not_found")

        fhash = fhash or file_hash(str(pdf_path))
        filename = pdf_path.name

        logger.info(f"{'='*60}")
        logger.info(f"Processing: {filename} (bank={bank})")
        logger.info(f"{'='*60}")

        # ── Step 1: Try deterministic parser ─────────────────────
        det_df = pd.DataFrame()
        det_summary = None
        if self.use_deterministic:
            det_df, det_summary = self._try_deterministic(str(pdf_path), bank)

        # Trust the deterministic parser for any known bank when it returns at
        # least 1 transaction.  The old threshold of >= 3 caused statements
        # with genuinely few transactions (e.g. a 2-transaction month) to fall
        # through to the LLM, which then hallucinated example-prompt data.
        if not det_df.empty and bank != "unknown":
            logger.info(
                f"Deterministic parser succeeded: {len(det_df)} transactions"
            )
            return self._finalize(
                det_df, bank, fhash, filename, card_name, email_date,
                method="deterministic",
                confidence=0.95,
                raw_text="",
                llm_output="",
                statement_summary=det_summary,
                **meta,
            )

        # For unknown bank or truly zero-result parsers, fall through to LLM
        # but still keep det_df around as a fallback.
        if not det_df.empty and len(det_df) >= 3:
            logger.info(
                f"Deterministic parser succeeded: {len(det_df)} transactions"
            )
            return self._finalize(
                det_df, bank, fhash, filename, card_name, email_date,
                method="deterministic",
                confidence=0.95,
                raw_text="",
                llm_output="",
                statement_summary=det_summary,
                **meta,
            )

        # ── Step 2: Multi-stage text extraction ──────────────────
        extraction = self.text_extractor.extract(pdf_path)
        raw_text = extraction.text

        if not raw_text.strip():
            logger.warning(f"No text extracted from {filename}")
            self._log_extraction(
                fhash, filename, "none", "", "", "",
                0, 0.0, error="All extraction stages returned empty text"
            )
            return self._empty_result("no_text")

        logger.info(
            f"Text extracted via '{extraction.method}': "
            f"{len(raw_text)} chars, confidence={extraction.confidence:.2f}"
        )

        # ── Step 3: LLM structuring ─────────────────────────────
        if not self.llm_enabled or not self.llm_client.is_available():
            if not self.llm_enabled:
                logger.info("LLM disabled in config, skipping")
            else:
                logger.warning("Ollama not available, skipping LLM extraction")

            # Fall back: try deterministic even if it got few results
            if not det_df.empty:
                return self._finalize(
                    det_df, bank, fhash, filename, card_name, email_date,
                    method=f"deterministic_fallback({extraction.method})",
                    confidence=0.5,
                    raw_text=raw_text,
                    llm_output="",
                    **meta,
                )
            self._log_extraction(
                fhash, filename, extraction.method, raw_text, "", "",
                0, 0.0, error="LLM unavailable and deterministic failed"
            )
            return self._empty_result("llm_unavailable")

        # Primary prompt
        transactions = self.llm_client.extract_transactions(
            raw_text, PRIMARY_PROMPT
        )
        llm_output = json.dumps(transactions, indent=2) if transactions else ""

        # If primary fails or returns few results, try hard mode
        if len(transactions) < 3:
            logger.info("Primary prompt returned few results, trying hard mode")
            hard_txns = self.llm_client.extract_transactions(
                raw_text, HARD_MODE_PROMPT
            )
            if len(hard_txns) > len(transactions):
                transactions = hard_txns
                llm_output = json.dumps(hard_txns, indent=2)

        if not transactions:
            logger.warning(f"LLM returned no transactions for {filename}")
            # Last resort: use deterministic partial results
            if not det_df.empty:
                return self._finalize(
                    det_df, bank, fhash, filename, card_name, email_date,
                    method=f"deterministic_fallback({extraction.method})",
                    confidence=0.4,
                    raw_text=raw_text,
                    llm_output=llm_output,
                    **meta,
                )
            self._log_extraction(
                fhash, filename, extraction.method, raw_text,
                llm_output, "", 0, 0.0, error="LLM returned no transactions"
            )
            return self._empty_result("llm_empty")

        # ── Step 4: Validation ───────────────────────────────────
        validated = self.validator.validate(transactions)

        # Optionally run LLM validation pass
        if validated and len(validated) > 0:
            try:
                validated = self.llm_client.validate_transactions(
                    validated, VALIDATION_PROMPT
                )
                validated = self.validator.validate(validated)  # Re-validate
            except Exception as e:
                logger.warning(f"LLM validation pass failed: {e}")

        if not validated:
            logger.warning(f"All transactions failed validation for {filename}")
            self._log_extraction(
                fhash, filename, extraction.method, raw_text,
                llm_output, "", 0, 0.0, error="All transactions failed validation"
            )
            return self._empty_result("validation_failed")

        # ── Step 5: Convert to DataFrame ─────────────────────────
        df = self._transactions_to_df(validated, bank)
        confidence = extraction.confidence * (len(validated) / max(len(transactions), 1))

        return self._finalize(
            df, bank, fhash, filename, card_name, email_date,
            method=f"llm({extraction.method})",
            confidence=confidence,
            raw_text=raw_text,
            llm_output=llm_output,
            **meta,
        )

    def _try_deterministic(self, pdf_path: str, bank: str) -> tuple[pd.DataFrame, dict | None]:
        """Try the bank-specific deterministic parser.

        Returns (df, statement_summary) where statement_summary may be None.
        """
        try:
            parser = get_parser(bank)
            df = parser.parse(pdf_path)
            summary = None
            if hasattr(parser, "get_statement_summary"):
                try:
                    summary = parser.get_statement_summary(pdf_path)
                except Exception as e:
                    logger.debug(f"Statement summary extraction failed for {bank}: {e}")
            return (df if df is not None else pd.DataFrame()), summary
        except Exception as e:
            logger.debug(f"Deterministic parser failed for {bank}: {e}")
            return pd.DataFrame(), None

    def _transactions_to_df(self, transactions: list[dict],
                            bank: str) -> pd.DataFrame:
        """Convert validated transaction dicts to a DataFrame."""
        records = []
        for txn in transactions:
            records.append({
                "date": txn.get("date", ""),
                "description": txn.get("description", ""),
                "amount": txn.get("amount", 0.0),
                "type": txn.get("type", "debit"),
                "card_name": txn.get("card_name"),
                "card_last4": txn.get("card_last4"),
                "balance": txn.get("balance"),
                "reward_points": txn.get("reward_points"),
            })

        df = pd.DataFrame(records)
        if df.empty:
            return df

        # Ensure required columns exist
        for col in ["card_name", "card_last4", "balance", "reward_points"]:
            if col not in df.columns:
                df[col] = None

        return df

    def _finalize(self, df: pd.DataFrame, bank: str, fhash: str,
                  filename: str, card_name: str, email_date: str,
                  method: str, confidence: float,
                  raw_text: str = "", llm_output: str = "",
                  statement_summary: dict = None,
                  **meta) -> dict:
        """
        Categorize, detect card info, store to DB, log extraction.
        """
        # Detect card name from PDF text if not known
        if not card_name and self.card_identifiers:
            try:
                text = self.pdf_extractor.extract_text(
                    meta.get("unlocked_path") or meta.get("path", "")
                )
                if text:
                    card_name = self.pdf_extractor.detect_card_name(
                        text, self.card_identifiers, bank=bank
                    )
            except Exception:
                pass

        # Detect reward points
        reward_points = None
        try:
            text = self.pdf_extractor.extract_text(
                meta.get("unlocked_path") or meta.get("path", "")
            )
            if text:
                reward_points = self.pdf_extractor.detect_reward_points(text)
        except Exception:
            pass

        # Categorize
        if "category" not in df.columns:
            df["category"] = self.categorizer.categorize_batch(
                df["description"].tolist()
            )

        # Store transactions
        inserted = self.db.insert_transactions(
            df, bank, filename, fhash,
            card_name=card_name,
            reward_points=reward_points,
            statement_received_date=email_date,
        )

        # Update statement status
        self.db.update_statement_status(fhash, "completed", len(df))

        # Derive statement period from min/max transaction dates
        period_start = None
        period_end = None
        if not df.empty and "date" in df.columns:
            dates = pd.to_datetime(df["date"], errors="coerce").dropna()
            if not dates.empty:
                period_start = dates.min().strftime("%Y-%m-%d")
                period_end = dates.max().strftime("%Y-%m-%d")

        # Backfill card_name and period dates into statements_metadata
        self.db.update_statement_metadata(
            fhash,
            card_name=card_name,
            statement_period_start=period_start,
            statement_period_end=period_end,
        )

        # Store financial summary if available (e.g. from CSB parser)
        if statement_summary and statement_summary.get("total_amount_due") is not None:
            stmt_credits = (
                (statement_summary.get("repayments") or 0)
                + (statement_summary.get("refunds_and_reversals") or 0)
                + (statement_summary.get("waivers") or 0)
            )
            self.db.update_statement_summary(
                fhash,
                total_amount_due=statement_summary.get("total_amount_due"),
                total_spends=statement_summary.get("spends"),
                total_credits=stmt_credits or None,
            )
        elif not df.empty:
            # Derive spends and credits directly from transactions
            txn_spends = df[df["type"] == "debit"]["amount"].sum()
            txn_credits = df[df["type"] == "credit"]["amount"].sum()
            self.db.update_statement_summary(
                fhash,
                total_spends=float(txn_spends) if txn_spends > 0 else None,
                total_credits=float(txn_credits) if txn_credits > 0 else None,
            )

        # Log extraction
        self._log_extraction(
            fhash, filename, method, raw_text, llm_output,
            json.dumps(df.to_dict(orient="records")),
            len(df), confidence,
        )

        logger.info(
            f"✓ {filename}: {len(df)} txns, {inserted} new, "
            f"method={method}, confidence={confidence:.2f}"
            f"{f', card={card_name}' if card_name else ''}"
        )

        return {
            "transactions": df.to_dict(orient="records"),
            "df": df,
            "method": method,
            "confidence": confidence,
            "transaction_count": len(df),
            "inserted": inserted,
            "card_name": card_name,
        }

    def _log_extraction(self, fhash: str, filename: str, method: str,
                        raw_text: str, llm_output: str, cleaned_json: str,
                        txn_count: int, confidence: float,
                        error: str = None):
        """Store extraction log in database."""
        try:
            self.db.store_extraction_log(
                file_hash=fhash,
                filename=filename,
                extraction_method=method,
                raw_text=raw_text if self.store_raw else "",
                llm_raw_output=llm_output if self.store_llm else "",
                cleaned_json=cleaned_json,
                transaction_count=txn_count,
                confidence_score=confidence,
                llm_model=self.llm_client.model if self.llm_enabled else None,
                error_message=error,
            )
        except Exception as e:
            logger.error(f"Failed to store extraction log: {e}")

    def _empty_result(self, reason: str) -> dict:
        """Return an empty result dict."""
        return {
            "transactions": [],
            "df": pd.DataFrame(),
            "method": reason,
            "confidence": 0.0,
            "transaction_count": 0,
            "inserted": 0,
            "card_name": None,
        }

    @staticmethod
    def _detect_bank_from_content(pdf_path: str) -> str | None:
        """Detect bank by scanning first page text for known bank keywords."""
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                if not pdf.pages:
                    return None
                text = (pdf.pages[0].extract_text() or "").lower()
                if "csb bank" in text or "edge csb" in text:
                    return "csb"
                if "federal bank" in text or "federalbank" in text:
                    return "federal"
                if "idfc first" in text or "idfcfirstbank" in text:
                    return "idfc_first"
                if "axis bank" in text or "axisbank" in text:
                    return "axis"
                if "hdfc bank" in text:
                    return "hdfc"
                if "sbi card" in text or "sbicard" in text:
                    return "sbi"
                if "icici" in text:
                    return "icici"
                if "yes bank" in text or "yesbank" in text:
                    return "yes"
                if "kotak" in text:
                    return "kotak"
        except Exception:
            pass
        return None

    def process_folder(self, folder_path: str, bank: str = "unknown") -> list[dict]:
        """Process all PDFs in a folder."""
        folder = Path(folder_path)
        if not folder.exists():
            logger.error(f"Folder not found: {folder}")
            return []

        results = []
        pdfs = sorted(folder.rglob("*.pdf"))
        logger.info(f"Found {len(pdfs)} PDFs in {folder}")

        for pdf_path in pdfs:
            fhash = file_hash(str(pdf_path))

            # Detect bank from subfolder structure within the given folder.
            # Only apply subfolder-based detection when the caller did NOT
            # pass an explicit bank (i.e. bank == "unknown").
            try:
                rel = pdf_path.relative_to(folder)
                parts = rel.parts
                detected_bank = (parts[0] if len(parts) > 1 else bank) if bank == "unknown" else bank
            except ValueError:
                detected_bank = bank

            # If still unknown, try detecting from PDF content
            if detected_bank == "unknown":
                content_bank = self._detect_bank_from_content(str(pdf_path))
                if content_bank:
                    detected_bank = content_bank
                    logger.info(f"Detected bank '{detected_bank}' from PDF content: {pdf_path.name}")

            if self.db.is_file_processed(fhash):
                logger.debug(f"Already processed: {pdf_path.name}")
                continue

            # Record statement metadata
            self.db.record_statement(
                file_hash=fhash,
                original_path=str(pdf_path),
                bank=detected_bank,
                filename=pdf_path.name,
            )

            result = self.process_pdf(
                str(pdf_path), bank=detected_bank, fhash=fhash,
                path=str(pdf_path),
            )
            results.append(result)

        total_txns = sum(r["transaction_count"] for r in results)
        total_inserted = sum(r["inserted"] for r in results)
        logger.info(
            f"Folder processing complete: {len(pdfs)} PDFs, "
            f"{total_txns} transactions, {total_inserted} new"
        )
        return results
