"""`python -m sysvigil`; from a checkout, Textual may live in `.vendor`."""

from pathlib import Path
import sys

vendor = Path(__file__).resolve().parents[1] / ".vendor"
if vendor.is_dir():
    sys.path.insert(0, str(vendor))

from sysvigil.cli import main  # noqa: E402

main()
