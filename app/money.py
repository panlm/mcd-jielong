"""Money helpers. Internally everything is integer fen (分)."""
from decimal import ROUND_HALF_UP, Decimal


def yuan_to_fen(v: str | int | float) -> int:
    """Convert a yuan amount (str/int/float) to integer fen, rounding half up."""
    return int((Decimal(str(v)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fen_to_yuan(fen: int) -> str:
    """Render fen as a yuan string without trailing zeros: 2210 -> '22.1', 600 -> '6'."""
    s = f"{Decimal(fen) / 100:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s
