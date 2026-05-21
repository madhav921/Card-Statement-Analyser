"""StmtForge CLI - Credit Card Statement Parser & Analyzer."""

import argparse
import sys


ENV_EXAMPLE_TEMPLATE = """# ============================================================
# StmtForge - Environment Variables
# ============================================================
# Copy this file as .env and fill in your details.
# NEVER commit .env to version control.

# ---- Google OAuth ----
# Path to the OAuth credentials JSON downloaded from Google Cloud Console
GOOGLE_CREDENTIALS_FILE=credentials.json

# ---- PDF passwords ----
# Date of Birth (DDMMYYYY format) - used to unlock bank PDFs
DOB=
# PAN card number (uppercase, e.g. ABCDE1234F)
PAN=
# Additional passwords, comma-separated
CUSTOM_PASSWORDS=

# ---- Per-bank PDF passwords (optional overrides) ----
SBI_PASSWORD=
HDFC_PASSWORD=
ICICI_PASSWORD=
AXIS_PASSWORD=
IDFC_PASSWORD=
FEDERAL_PASSWORD=
CSB_PASSWORD=
YESBANK_PASSWORD=

# ---- Privacy logging ----
# Salt used to pseudonymize PII in event logs (change in production)
STMTFORGE_LOG_SALT=
"""


def main():
    parser = argparse.ArgumentParser(
        prog="stmtforge",
        description="StmtForge - Credit Card Statement Parser & Analyzer",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── run ───────────────────────────────────────────────────
    run_parser = sub.add_parser("run", help="Run the processing pipeline")
    run_parser.add_argument("--full", action="store_true",
                            help="Full historical fetch (default: incremental)")
    run_parser.add_argument("--local", action="store_true",
                            help="Process only local PDFs, skip Gmail fetch")
    run_parser.add_argument("--dashboard", action="store_true",
                            help="Launch dashboard after processing")
    run_parser.add_argument("--folder", type=str, default=None,
                            help="Process all PDFs in a specific folder")
    run_parser.add_argument("--reprocess", action="store_true",
                            help="Re-process all previously completed statements")

    # ── dashboard ─────────────────────────────────────────────
    sub.add_parser("dashboard", help="Launch the Streamlit dashboard")

    # ── init ──────────────────────────────────────────────────
    sub.add_parser("init", help="Initialize project directory with config template")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "run":
        from stmtforge.run_pipeline import run_pipeline
        run_pipeline(
            full=args.full,
            local_only=args.local,
            folder=args.folder,
            reprocess=args.reprocess,
        )
        if args.dashboard:
            _launch_dashboard()

    elif args.command == "dashboard":
        _launch_dashboard()

    elif args.command == "init":
        _init_project()


def _launch_dashboard():
    import subprocess
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print(
            "Error: streamlit is not installed.\n"
            "Run: pip install stmtforge  (or pip install streamlit plotly)\n"
            "then try again.",
            file=sys.stderr,
        )
        sys.exit(1)
    from pathlib import Path
    import stmtforge.dashboard.app as _app_module
    app_path = str(Path(_app_module.__file__))
    subprocess.run([sys.executable, "-m", "streamlit", "run", app_path], check=False)


def _init_project():
    from pathlib import Path
    import shutil

    target = Path.cwd()
    config_dst = target / "config.yaml"
    env_example_dst = target / ".env.example"

    if config_dst.exists():
        print(f"config.yaml already exists in {target}")
    else:
        # Copy bundled template
        template = Path(__file__).parent / "config_template.yaml"
        if template.exists():
            shutil.copy2(template, config_dst)
            print(f"Created config.yaml in {target}")
        else:
            print("No config template found. Create config.yaml manually.")

    # Create data directories
    for d in ["data/raw_pdfs", "data/unlocked_pdfs", "data/processed",
              "data/logs", "data/logs/events"]:
        (target / d).mkdir(parents=True, exist_ok=True)

    # Create .env.example for first-time setup convenience
    if not env_example_dst.exists():
        env_example_dst.write_text(ENV_EXAMPLE_TEMPLATE, encoding="utf-8")
        print(f"Created .env.example in {target}")

    print("Project initialized. Edit config.yaml with your settings.")


if __name__ == "__main__":
    main()
