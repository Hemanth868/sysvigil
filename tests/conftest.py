"""Use the checkout's local Textual install when running tests without pip install."""

from pathlib import Path
import sys


vendor = Path(__file__).resolve().parents[1] / ".vendor"
if vendor.is_dir():
    sys.path.insert(0, str(vendor))
