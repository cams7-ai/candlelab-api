import re
from decimal import Context, Decimal, ROUND_HALF_EVEN

from candlelab.domain.errors import invalid

# Indicators/ratios use a fixed context; accounting has enough precision for bounded inputs.
INDICATOR_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
MONEY_CONTEXT = Context(prec=120, rounding=ROUND_HALF_EVEN)
NUMERIC_POLICY = "DECIMAL50_HALF_EVEN_MONEY120_V1"
DECIMAL_PATTERN = r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"


def decimal_input(value, field="value"):
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(DECIMAL_PATTERN, value):
        raise invalid(
            field, "Use string decimal com ponto, sem expoente (até 24 dígitos/12 casas)."
        )
    d = Decimal(value)
    if (
        len(d.as_tuple().digits) > 24
        or d.as_tuple().exponent < -12
        or d.copy_abs() > Decimal("1e12")
    ):
        raise invalid(field, "Decimal fora dos limites: 24 dígitos, 12 casas, módulo <= 10^12.")
    return d


def wire(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result
