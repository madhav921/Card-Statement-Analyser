"""
StmtForge - Main Pipeline Runner

Orchestrates the full pipeline:
1. Fetch emails from Gmail
2. Unlock PDF passwords
3. Extract & parse transactions
4. Categorize transactions
5. Store in database

Usage:
    stmtforge run                # Incremental update (from last fetch)
    stmtforge run --full         # Full historical fetch
    stmtforge run --local        # Process only local PDFs (skip Gmail)
    stmtforge run --dashboard    # Launch dashboard after processing
"""

import re
import shutil
from datetime import datetime
from pathlib import Path

from stmtforge.utils.config import load_config, resolve_path
from stmtforge.utils.logging_config import setup_logging, get_logger
from stmtforge.utils.hashing import file_hash
from stmtforge.utils.scope_filter import (
    is_irrelevant_filename,
    is_irrelevant_statement_text,
    is_irrelevant_pdf_path,
)
from stmtforge.database.db import Database
from stmtforge.pdf_processing.unlocker import PDFUnlocker
from stmtforge.parsers.registry import get_parser
from stmtforge.parsers.categorizer import Categorizer
from stmtforge.hybrid_pipeline import HybridPipeline
from stmtforge.utils.run_logger import RunLogger
from stmtforge.utils.privacy_logging import PrivacyEventLogger


# Filename→bank mapping for PDFs in the "unknown" folder.
# Patterns here are generic; account-number-specific hints belong in config.yaml
# (parsers.filename_to_bank) which is gitignored and user-local.
_FILENAME_BANK_HINTS = [
    (re.compile(r"^CreditCard_Statement_"), "federal"),
    (re.compile(r"^Credit Card Statement\.pdf$", re.IGNORECASE), "axis"),
    (re.compile(r"^Scapia_"), "federal"),
]


def _is_irrelevant_pdf(filename: str, bank: str) -> bool:
    return is_irrelevant_filename(filename, bank)


def _detect_bank_from_filename(filename: str) -> str | None:
    for pattern, bank in _FILENAME_BANK_HINTS:
        if pattern.search(filename):
            return bank
    return None


def _detect_bank_from_content(pdf_path: str) -> str | None:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                return None
            text = (pdf.pages[0].extract_text() or "").lower()

            if is_irrelevant_statement_text(text):
                return None

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
    except Exception:
        pass
    return None


def _resolve_bank_for_unknown_pdf(pdf_path: Path, peer_path: Path | None = None) -> str | None:
    bank = _detect_bank_from_filename(pdf_path.name)
    if bank:
        return bank
    bank = _detect_bank_from_content(str(pdf_path))
    if bank:
        return bank
    if peer_path and peer_path.exists():
        bank = _detect_bank_from_filename(peer_path.name)
        if bank:
            return bank
        bank = _detect_bank_from_content(str(peer_path))
        if bank:
            return bank
    return None


def _move_file_preserving_month(src_file: Path, root_dir: Path, bank: str) -> Path:
    try:
        rel = src_file.relative_to(root_dir / "unknown")
        rel_parts = rel.parts
        month_dir = rel_parts[0] if rel_parts else datetime.now().strftime("%Y_%m")
    except ValueError:
        month_dir = datetime.now().strftime("%Y_%m")

    target_dir = root_dir / bank / month_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    target_path = target_dir / src_file.name
    if target_path.exists():
        stem = target_path.stem
        suffix = target_path.suffix
        counter = 1
        while target_path.exists():
            target_path = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    shutil.move(str(src_file), str(target_path))
    return target_path


def _infer_bank_from_unlocked_sibling(raw_pdf: Path, raw_root: Path, unlocked_root: Path) -> str | None:
    try:
        rel = raw_pdf.relative_to(raw_root / "unknown")
    except ValueError:
        return None

    candidates: list[str] = []
    if len(rel.parts) >= 2:
        month_dir = rel.parts[0]
        filename = rel.parts[-1]
        for bank_dir in unlocked_root.iterdir():
            if not bank_dir.is_dir() or bank_dir.name == "unknown":
                continue
            if (bank_dir / month_dir / filename).exists():
                candidates.append(bank_dir.name)

    if len(candidates) == 1:
        return candidates[0]
    return None


def organize_unknown_pdfs() -> dict:
    logger = get_logger("pipeline.organize")
    config = load_config()

    raw_root = resolve_path(config["data"]["raw_pdfs"])
    unlocked_root = resolve_path(config["data"]["unlocked_pdfs"])

    moved_raw = 0
    moved_unlocked = 0

    unknown_unlocked_dir = unlocked_root / "unknown"
    unknown_raw_dir = raw_root / "unknown"

    unlocked_bank_by_rel: dict[Path, str] = {}
    if unknown_unlocked_dir.exists():
        for unlocked_pdf in unknown_unlocked_dir.rglob("*.pdf"):
            bank = _resolve_bank_for_unknown_pdf(unlocked_pdf)
            if not bank:
                continue
            try:
                rel = unlocked_pdf.relative_to(unknown_unlocked_dir)
                unlocked_bank_by_rel[rel] = bank
            except ValueError:
                continue

    if unknown_raw_dir.exists():
        for raw_pdf in unknown_raw_dir.rglob("*.pdf"):
            peer_unlocked = None
            precomputed_bank = None
            try:
                rel_to_unknown = raw_pdf.relative_to(unknown_raw_dir)
                candidate = unknown_unlocked_dir / rel_to_unknown
                peer_unlocked = candidate if candidate.exists() else None
                precomputed_bank = unlocked_bank_by_rel.get(rel_to_unknown)
            except ValueError:
                peer_unlocked = None

            bank = precomputed_bank or _resolve_bank_for_unknown_pdf(raw_pdf, peer_unlocked)
            if not bank:
                bank = _infer_bank_from_unlocked_sibling(raw_pdf, raw_root, unlocked_root)
            if not bank:
                continue
            dst = _move_file_preserving_month(raw_pdf, raw_root, bank)
            moved_raw += 1
            logger.info(f"Moved raw PDF: {raw_pdf.name} -> {bank} ({dst})")

    if unknown_unlocked_dir.exists():
        for unlocked_pdf in unknown_unlocked_dir.rglob("*.pdf"):
            bank = _resolve_bank_for_unknown_pdf(unlocked_pdf)
            if not bank:
                continue
            dst = _move_file_preserving_month(unlocked_pdf, unlocked_root, bank)
            moved_unlocked += 1
            logger.info(f"Moved unlocked PDF: {unlocked_pdf.name} -> {bank} ({dst})")

    return {"moved_raw": moved_raw, "moved_unlocked": moved_unlocked}


def backfill_unknown_bank_rows(db: Database) -> dict:
    logger = get_logger("pipeline.backfill")
    rules = [
        ("Statement.pdf", "csb", "Edge"),
        ("Credit Card Statement.pdf", "axis", "Neo Rupay"),
        ("CreditCard_Statement_%", "federal", "Signet"),
    ]

    tx_updated = 0
    st_updated = 0

    with db._get_conn() as conn:
        for pattern, bank, card in rules:
            cur = conn.execute(
                "UPDATE transactions SET bank = ?, card_name = COALESCE(card_name, ?) "
                "WHERE bank = 'unknown' AND source_file LIKE ?",
                (bank, card, pattern),
            )
            tx_updated += cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

            cur = conn.execute(
                "UPDATE statements_metadata SET bank = ?, card_name = COALESCE(card_name, ?) "
                "WHERE bank = 'unknown' AND filename LIKE ?",
                (bank, card, pattern),
            )
            st_updated += cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

    if tx_updated or st_updated:
        logger.info(
            f"Backfilled unknown rows: transactions={tx_updated}, statements={st_updated}"
        )

    return {"transactions": tx_updated, "statements": st_updated}


def cleanup_irrelevant_records(db: Database) -> dict:
    logger = get_logger("pipeline.cleanup")

    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT file_hash, filename, bank, original_path FROM statements_metadata"
        ).fetchall()

        def _effective_filename(row) -> str | None:
            """Return filename, falling back to basename of original_path."""
            if row["filename"]:
                return row["filename"]
            if row["original_path"]:
                from pathlib import Path as _Path
                return _Path(row["original_path"]).name
            return None

        irrelevant_hashes = [
            row["file_hash"]
            for row in rows
            if (fn := _effective_filename(row)) and is_irrelevant_filename(fn, row["bank"] or "unknown")
        ]

        if not irrelevant_hashes:
            return {"statements_marked": 0, "transactions_deleted": 0, "logs_deleted": 0}

        placeholders = ",".join("?" for _ in irrelevant_hashes)

        cur = conn.execute(
            f"DELETE FROM transactions WHERE file_hash IN ({placeholders})",
            irrelevant_hashes,
        )
        tx_deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

        cur = conn.execute(
            f"DELETE FROM extraction_log WHERE file_hash IN ({placeholders})",
            irrelevant_hashes,
        )
        logs_deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

        cur = conn.execute(
            f"UPDATE statements_metadata SET status = 'skipped_irrelevant', "
            f"transaction_count = 0, "
            f"error_message = 'Out-of-scope document (non-credit-card statement)', "
            f"processed_at = datetime('now') "
            f"WHERE file_hash IN ({placeholders})",
            irrelevant_hashes,
        )
        st_marked = cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

    logger.info(
        f"Cleanup out-of-scope records: "
        f"statements_marked={st_marked}, transactions_deleted={tx_deleted}, "
        f"logs_deleted={logs_deleted}"
    )
    return {
        "statements_marked": st_marked,
        "transactions_deleted": tx_deleted,
        "logs_deleted": logs_deleted,
    }


def cleanup_csb_edge_artifacts(db: Database) -> dict:
    logger = get_logger("pipeline.cleanup.csb")

    credit_patterns = [
        "%repayment%", "%payment received%", "%thank you%",
        "%refund%", "%reversal%", "%cashback%", "%reward%",
    ]

    promoted_to_credit = 0
    deleted_noise = 0

    with db._get_conn() as conn:
        for pattern in credit_patterns:
            cur = conn.execute(
                "UPDATE transactions SET type = 'credit' "
                "WHERE bank = 'csb' AND type = 'debit' "
                "AND source_file = 'Statement.pdf' AND LOWER(description) LIKE ?",
                (pattern,),
            )
            promoted_to_credit += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        cur = conn.execute(
            "DELETE FROM transactions "
            "WHERE bank = 'csb' AND source_file = 'Statement.pdf' "
            "AND LOWER(TRIM(description)) IN ('rs', 'rs.', 'inr', 'inr.')"
        )
        deleted_noise += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    if promoted_to_credit or deleted_noise:
        logger.info(
            f"CSB cleanup: promoted_to_credit={promoted_to_credit}, "
            f"deleted_noise={deleted_noise}"
        )

    return {"promoted_to_credit": promoted_to_credit, "deleted_noise": deleted_noise}


def _backfill_statement_metadata(db: Database, logger=None) -> None:
    """Backfill card_name, period dates, and financial totals from existing transactions."""
    if logger is None:
        logger = get_logger("pipeline.backfill_meta")
    try:
        result = db.backfill_statement_metadata()
        if result["metadata_updated"] or result["summary_updated"]:
            logger.info(
                f"Backfilled statement metadata: "
                f"metadata_rows={result['metadata_updated']}, "
                f"summary_rows={result['summary_updated']}"
            )
    except Exception as e:
        logger.warning(f"Statement metadata backfill failed: {e}")


def run_gmail_fetch(db: Database, full: bool = False,
                    run_log: RunLogger = None,
                    event_logger: PrivacyEventLogger = None) -> list:
    logger = get_logger("pipeline.gmail")
    logger.info("=" * 60)
    logger.info("STEP 1: Gmail Fetch")
    logger.info("=" * 60)

    try:
        from stmtforge.gmail.fetcher import GmailFetcher
        fetcher = GmailFetcher(db=db)

        if full:
            config = load_config()
            start = config["gmail"]["search"].get("initial_start_date", "2024-06-01")
            query = fetcher.build_query(start_date=start.replace("-", "/"))
            messages = fetcher.fetch_messages(query)
        else:
            messages = fetcher.fetch_messages()

        downloaded = fetcher.download_attachments(messages)
        if downloaded:
            db.update_last_fetch_date(datetime.now().strftime("%Y-%m-%d"))

        emails_scanned = len(messages) if messages else 0
        logger.info(f"Gmail fetch complete: {len(downloaded)} new PDFs downloaded")
        if run_log:
            run_log.log_gmail_fetch(skipped=False, emails_scanned=emails_scanned, downloaded=downloaded)
        if event_logger:
            event_logger.log_event(
                "gmail_fetch_completed",
                {"full_mode": full, "emails_scanned": emails_scanned, "new_pdfs_downloaded": len(downloaded)},
                source="pipeline.gmail",
            )
        return downloaded

    except ImportError:
        logger.error(
            "Gmail dependencies are not installed. "
            "Run: pip install stmtforge[gmail]  — then re-run without --local."
        )
        logger.info("Continuing with local PDF processing...")
        if run_log:
            run_log.log_gmail_fetch(skipped=True)
        if event_logger:
            event_logger.log_event("gmail_fetch_skipped", {"reason": "deps_not_installed"},
                                   source="pipeline.gmail", severity="warning")
        return []
    except FileNotFoundError as e:
        logger.error(f"Gmail setup incomplete: {e}")
        logger.info("Continuing with local PDF processing...")
        if run_log:
            run_log.log_gmail_fetch(skipped=True)
        if event_logger:
            event_logger.log_event("gmail_fetch_skipped", {"reason": "setup_incomplete"},
                                   source="pipeline.gmail", severity="warning")
        return []
    except Exception as e:
        logger.error(f"Gmail fetch failed: {e}")
        logger.info("Continuing with local PDF processing...")
        if run_log:
            run_log.log_gmail_fetch(skipped=True)
        if event_logger:
            event_logger.log_event("gmail_fetch_failed", {"error": str(e)},
                                   source="pipeline.gmail", severity="error")
        return []


def discover_local_pdfs(db: Database) -> list:
    logger = get_logger("pipeline.discover")
    config = load_config()
    raw_dir = resolve_path(config["data"]["raw_pdfs"])

    if not raw_dir.exists():
        return []

    pdfs = []
    skipped = 0
    for pdf_path in raw_dir.rglob("*.pdf"):
        fhash = file_hash(pdf_path)
        if not db.is_file_processed(fhash):
            rel = pdf_path.relative_to(raw_dir)
            parts = rel.parts
            bank = parts[0] if len(parts) > 1 else "unknown"
            filename = pdf_path.name

            if _is_irrelevant_pdf(filename, bank) or is_irrelevant_pdf_path(str(pdf_path), bank):
                logger.debug(f"Skipping irrelevant PDF: {filename}")
                skipped += 1
                continue

            if bank == "unknown":
                detected = _detect_bank_from_filename(filename)
                if detected:
                    bank = detected
                    logger.info(f"Detected bank '{bank}' from filename: {filename}")

            pdfs.append({
                "path": str(pdf_path),
                "bank": bank,
                "file_hash": fhash,
                "filename": filename,
            })

    if skipped:
        logger.info(f"Skipped {skipped} irrelevant PDFs (non-CC-statement docs)")
    logger.info(f"Found {len(pdfs)} unprocessed PDFs in {raw_dir}")
    return pdfs


def run_unlock(pdf_infos: list, db: Database = None) -> list:
    logger = get_logger("pipeline.unlock")
    logger.info("=" * 60)
    logger.info("STEP 2: PDF Unlock")
    logger.info("=" * 60)

    if not pdf_infos:
        logger.info("No PDFs to unlock")
        return []

    unlocker = PDFUnlocker()
    results = []

    for info in pdf_infos:
        pdf_path = info["path"]
        unlocked_path = unlocker.unlock(pdf_path)

        if unlocked_path:
            info["unlocked_path"] = str(unlocked_path)
            results.append(info)
        else:
            logger.warning(f"Could not unlock: {info['filename']}")
            info["unlocked_path"] = None
            info["error"] = "Failed to unlock PDF"
            # Record in DB so this PDF is skipped in future runs (until --reprocess)
            if db and info.get("file_hash"):
                db.record_statement(
                    file_hash=info["file_hash"],
                    original_path=info["path"],
                    bank=info.get("bank", "unknown"),
                    filename=info.get("filename"),
                )
                db.update_statement_status(
                    info["file_hash"], "unlock_failed", 0,
                    "PDF password not available — add password to .env CUSTOM_PASSWORDS and run --reprocess"
                )

    logger.info(f"Unlock complete: {len(results)}/{len(pdf_infos)} successful")
    return results


def run_parse_and_store(pdf_infos: list, db: Database,
                        run_log: RunLogger = None,
                        event_logger: PrivacyEventLogger = None) -> int:
    logger = get_logger("pipeline.parse")
    logger.info("=" * 60)
    logger.info("STEP 3: Hybrid Parse, Categorize & Store")
    logger.info("=" * 60)

    if not pdf_infos:
        logger.info("No PDFs to process")
        return 0

    if run_log:
        run_log.log_discovered(len(pdf_infos))
    if event_logger:
        event_logger.log_event("parse_started", {"pdfs_discovered": len(pdf_infos)}, source="pipeline.parse")

    pipeline = HybridPipeline(db=db)
    total_inserted = 0

    for info in pdf_infos:
        pdf_path = info.get("unlocked_path") or info.get("path")
        if not pdf_path or not Path(pdf_path).exists():
            logger.warning(f"PDF not found: {pdf_path}")
            continue

        bank = info.get("bank", "unknown")
        fhash = info.get("file_hash", file_hash(pdf_path))
        card_name = info.get("card_name")
        email_date = info.get("email_date")

        if bank == "unknown":
            detected = _detect_bank_from_content(pdf_path)
            if detected:
                bank = detected
                info["bank"] = bank
                logger.info(f"Detected bank '{bank}' from PDF content: {info.get('filename', '')}")

        filename = info.get("filename") or Path(pdf_path).name
        if _is_irrelevant_pdf(filename, bank) or is_irrelevant_pdf_path(pdf_path, bank):
            logger.info(f"Skipping out-of-scope PDF during parse step: {filename}")
            db.record_statement(
                file_hash=fhash, original_path=info.get("path", pdf_path),
                bank=bank, card_name=card_name, email_date=email_date,
                email_subject=info.get("email_subject"), filename=filename,
                sender=info.get("sender"), message_id=info.get("message_id"),
            )
            db.update_statement_status(fhash, "skipped_irrelevant", 0,
                                       "Out-of-scope document (non-credit-card statement)")
            if run_log:
                run_log.log_statement(filename=filename, bank=bank, card_name=card_name,
                                      status="skipped", method="scope_filter")
            continue

        db.record_statement(
            file_hash=fhash, original_path=info.get("path", pdf_path),
            bank=bank, card_name=card_name, email_date=email_date,
            email_subject=info.get("email_subject"), filename=filename,
            sender=info.get("sender"), message_id=info.get("message_id"),
        )

        if db.is_file_processed(fhash):
            logger.debug(f"Already processed: {info.get('filename', pdf_path)}")
            if run_log:
                run_log.log_statement(filename=filename, bank=bank, card_name=card_name,
                                      status="already_processed")
            continue

        result = pipeline.process_pdf(
            pdf_path, bank=bank, fhash=fhash, card_name=card_name,
            email_date=email_date,
            path=info.get("path", pdf_path),
            unlocked_path=info.get("unlocked_path"),
        )

        inserted = result.get("inserted", 0)
        total_inserted += inserted
        txn_count = result["transaction_count"]

        if txn_count == 0:
            db.update_statement_status(fhash, "no_data", 0)

        if run_log:
            status = "parsed" if txn_count > 0 else "no_data"
            run_log.log_statement(
                filename=filename, bank=bank,
                card_name=result.get("card_name") or card_name,
                status=status, method=result.get("method"),
                confidence=result.get("confidence"),
                txn_count=txn_count, new_inserted=inserted,
                df=result.get("df"),
            )

    logger.info(f"Processing complete: {total_inserted} new transactions stored")
    return total_inserted


def run_pipeline(full: bool = False, local_only: bool = False,
                 folder: str = None, reprocess: bool = False):
    """Run the complete pipeline."""
    logger = setup_logging()
    logger = get_logger("pipeline")

    logger.info("=" * 60)
    logger.info("StmtForge Pipeline Started")
    logger.info(f"Mode: {'FULL' if full else 'INCREMENTAL'}, Local only: {local_only}")
    logger.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    run_log = RunLogger(mode={"full": full, "local": local_only, "reprocess": reprocess})
    event_logger = PrivacyEventLogger(channel="server")
    event_logger.log_event(
        "pipeline_started",
        {"full": full, "local_only": local_only, "reprocess": reprocess, "folder_mode": bool(folder)},
        source="pipeline",
    )

    db = Database()

    if folder:
        # Detect bank from folder path (e.g. data/unlocked_pdfs/csb/2026_04 → csb)
        from stmtforge.parsers.registry import list_available_parsers
        known_banks = set(list_available_parsers())
        folder_parts = Path(folder).resolve().parts
        detected_bank = "unknown"
        for part in reversed(folder_parts):
            if part.lower() in known_banks:
                detected_bank = part.lower()
                break

        if reprocess:
            logger.info("Reprocess mode: resetting statement statuses for folder PDFs")
            from stmtforge.utils.hashing import file_hash as fh
            for pdf in Path(folder).rglob("*.pdf"):
                h = fh(str(pdf))
                with db._get_conn() as conn:
                    conn.execute(
                        "UPDATE statements_metadata SET status = 'pending', "
                        "processed_at = NULL WHERE file_hash = ? AND status = 'completed'",
                        (h,),
                    )
                    conn.execute("DELETE FROM transactions WHERE file_hash = ?", (h,))

        logger.info(f"Processing folder: {folder} (bank={detected_bank})")
        pipeline = HybridPipeline(db=db)
        results = pipeline.process_folder(folder, bank=detected_bank)
        total_txns = sum(r["transaction_count"] for r in results)
        logger.info(f"Folder processing complete: {total_txns} transactions")
        _backfill_statement_metadata(db, logger)
        run_log.log_gmail_fetch(skipped=True)
        run_log.log_organize({"moved_raw": 0, "moved_unlocked": 0})
        run_log.log_backfill({"transactions": 0, "statements": 0})
        run_log.log_cleanup({}, {})
        summary = db.get_summary()
        run_log.log_summary(summary, new_transactions=total_txns)
        run_log.finish()
        return

    if reprocess:
        logger.info("Reprocess mode: resetting all statement statuses and clearing transactions")
        with db._get_conn() as conn:
            # Fetch file hashes being reset so we can delete their transactions
            rows = conn.execute(
                "SELECT file_hash FROM statements_metadata WHERE status = 'completed'"
            ).fetchall()
            hashes = [r[0] for r in rows if r[0]]
            if hashes:
                placeholders = ",".join("?" * len(hashes))
                conn.execute(
                    f"DELETE FROM transactions WHERE file_hash IN ({placeholders})",
                    hashes,
                )
            conn.execute(
                "UPDATE statements_metadata SET status = 'pending', "
                "processed_at = NULL WHERE status IN ('completed', 'unlock_failed')"
            )

    downloaded = []

    if not local_only:
        downloaded = run_gmail_fetch(db, full=full, run_log=run_log, event_logger=event_logger)
    else:
        run_log.log_gmail_fetch(skipped=True)

    moved = organize_unknown_pdfs()
    run_log.log_organize(moved)

    backfill = backfill_unknown_bank_rows(db)
    run_log.log_backfill(backfill)

    cleaned = cleanup_irrelevant_records(db)
    run_log.log_cleanup(cleaned, {})

    all_pdfs = discover_local_pdfs(db)

    seen_hashes = {p["file_hash"] for p in all_pdfs}
    for dl in downloaded:
        if dl.get("file_hash") and dl["file_hash"] not in seen_hashes:
            all_pdfs.append(dl)

    if not all_pdfs:
        logger.info("No new PDFs to process")
        # Still run backfill in case existing records are missing derived fields
        _backfill_statement_metadata(db, logger)
        summary = db.get_summary()
        run_log.log_summary(summary, new_transactions=0)
        run_log.finish()
        return

    unlocked = run_unlock(all_pdfs, db=db)
    total_inserted = run_parse_and_store(unlocked, db, run_log=run_log, event_logger=event_logger)

    _backfill_statement_metadata(db, logger)

    try:
        config = load_config()
        csv_path = resolve_path(config["data"]["processed"]) / "attachment_metadata.csv"
        db.export_attachment_metadata_csv(str(csv_path))
    except Exception as e:
        logger.error(f"CSV export failed: {e}")

    summary = db.get_summary()
    run_log.log_summary(summary, new_transactions=total_inserted)
    run_log.finish()

    logger.info("=" * 60)
    logger.info("Pipeline Complete!")
    logger.info(f"Total transactions in DB: {summary['total_transactions']}")
    logger.info(f"Total spend tracked: INR {summary['total_spend']:,.2f}")
    logger.info(f"Banks: {', '.join(summary['banks'])}")
    logger.info(f"Date range: {summary['date_range']['start']} to {summary['date_range']['end']}")
    logger.info("=" * 60)
