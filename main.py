"""エントリポイント。src/ をパスに追加してCLIを起動する。

例:
    python main.py convert
    python main.py assemble
    python main.py eda
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from defect_analysis.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
