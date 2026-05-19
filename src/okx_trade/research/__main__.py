"""Allow ``python -m okx_trade.research`` to dispatch to cli.run()."""
from __future__ import annotations

import sys

from .cli import run


if __name__ == "__main__":
    sys.exit(run())
