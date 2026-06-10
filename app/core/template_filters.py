"""Shared Jinja filters, registered onto a route's Jinja2Templates env."""


def _num(value, decimals: int = 2, sign: bool = False) -> str:
    """Format a number with comma thousand separators (— if None)."""
    if value is None:
        return "—"
    fmt = "{:+,.%df}" % decimals if sign else "{:,.%df}" % decimals
    return fmt.format(value)


def register_filters(templates) -> None:
    templates.env.filters["num"] = _num
