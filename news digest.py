"""Investor news digest: run it from a source checkout.

    python "news digest.py" --watchlist companies.xlsx
    python "news digest.py" --doctor

The code lives in the `newsdigest` package (one module per pipeline stage).
Installed with `pip install .`, the same thing is the `newsdigest` command.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from newsdigest.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
