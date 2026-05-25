"""ASCII banner — printed at the top of `sunday start` and `sunday init`.

Hand-crafted block letters so we have no font dependency. Rendered with
Rich for the amber gradient so it matches Sunday's accent color.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Console
from rich.text import Text

_SUNDAY = r"""
 ███████╗██╗   ██╗███╗   ██╗██████╗  █████╗ ██╗   ██╗
 ██╔════╝██║   ██║████╗  ██║██╔══██╗██╔══██╗╚██╗ ██╔╝
 ███████╗██║   ██║██╔██╗ ██║██║  ██║███████║ ╚████╔╝
 ╚════██║██║   ██║██║╚██╗██║██║  ██║██╔══██║  ╚██╔╝
 ███████║╚██████╔╝██║ ╚████║██████╔╝██║  ██║   ██║
 ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚═════╝ ╚═╝  ╚═╝   ╚═╝
"""

# Six-row vertical gradient from soft cream → amber → warm rust. Each
# value is the color for one row of the block art.
_GRADIENT = ["#f5e6c4", "#f3c969", "#e8a44a", "#d97f2c", "#b65c1b", "#8e3c0e"]


def render(console: Console | None = None, tagline: str | None = None) -> None:
    """Print the SUNDAY banner, gradient-colored, plus an optional tagline."""
    out = console or Console()
    lines = _SUNDAY.strip("\n").splitlines()
    txt = Text()
    for i, line in enumerate(lines):
        color = _GRADIENT[min(i, len(_GRADIENT) - 1)]
        txt.append(line, style=color)
        txt.append("\n")
    out.print()
    out.print(Align.left(txt))
    if tagline:
        out.print(f"  [dim]{tagline}[/dim]")
    out.print()
