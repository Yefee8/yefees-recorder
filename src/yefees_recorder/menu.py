"""Arrow-key menus drawn in one place on screen.

A `Screen` owns a single `rich.live` region for a whole session. Every menu,
text field and slider redraws *that* region, so moving between pages replaces
what is on screen instead of printing another block underneath it.

rich draws and `keys` reads, so there is no dependency here beyond what the
CLI already uses.
"""

from __future__ import annotations

import math
import sys
from typing import Any, Callable, NamedTuple

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from . import keys

CANCELLED = object()


def _encodable(text: str) -> bool:
    """Whether this terminal can print `text` at all.

    A legacy Windows console runs on cp1252 and raises UnicodeEncodeError on
    arrows and block glyphs, which would crash the menu rather than look plain.
    """
    try:
        text.encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError, AttributeError):
        return False
    return True


FANCY = _encodable("❯↑↓⏎←⌫█░")

CURSOR = "❯" if FANCY else ">"
BAR_FULL = "█" if FANCY else "#"
BAR_EMPTY = "░" if FANCY else "-"
HINT_MENU = (
    "↑↓ move   ⏎ select   ← back" if FANCY
    else "up/down move   Enter select   Left back"
)
HINT_TEXT = (
    "⏎ accept   ⌫ delete   Esc cancel   (empty = automatic)" if FANCY
    else "Enter accept   Backspace delete   Esc cancel   (empty = automatic)"
)
HINT_SLIDER = (
    "←→ adjust   ↑↓ bigger steps   ⏎ accept   Esc cancel" if FANCY
    else "left/right adjust   up/down bigger steps   Enter accept   Esc cancel"
)


def safe(text: str) -> str:
    """Make `text` printable on this terminal.

    Window titles and device names are other people's data and routinely carry
    characters a legacy console cannot encode. rich.live raises on those rather
    than substituting, which would take the whole menu down, so anything
    unrepresentable becomes a question mark here instead.
    """
    if FANCY:
        return text
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


class Item(NamedTuple):
    """One row. `value` is returned when it is chosen."""

    label: str
    value: Any = None
    detail: str = ""       # shown greyed to the right, e.g. the current setting
    enabled: bool = True
    reason: str = ""       # why it is disabled, shown instead of `detail`


def as_decibels(gain: float) -> str:
    """A multiplier described the way audio people read it."""
    if gain <= 0:
        return "silent"
    if abs(gain - 1.0) < 1e-9:
        return "unchanged"
    return f"{20 * math.log10(gain):+.1f} dB"


class Screen:
    """One live region that every page of a session redraws."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._live: Live | None = None

    def __enter__(self) -> "Screen":
        # transient: the region is wiped on exit, so whatever is printed
        # afterwards starts on a clean line instead of under a stale menu.
        self._live = Live(Text(""), console=self.console, auto_refresh=False, transient=True)
        self._live.__enter__()
        return self

    def __exit__(self, *exception) -> None:
        if self._live is not None:
            self._live.__exit__(*exception)
            self._live = None

    @staticmethod
    def _usable() -> bool:
        """No terminal means no keys will ever arrive; do not spin waiting."""
        return keys.interactive()

    def _draw(self, renderable) -> None:
        if self._live is not None:
            self._live.update(renderable, refresh=True)
        else:
            self.console.print(renderable)

    # ---------------------------------------------------------------- menus
    def choose(
        self,
        title: str,
        items: list[Item],
        *,
        cursor: int = 0,
        hint: str = "",
        on_left: bool = True,
    ) -> Any:
        """Show `items` and return the chosen value, or CANCELLED.

        Left, Esc and q all back out, which is what makes nested pages feel
        like one screen rather than a stack of prompts.
        """
        if not self._usable():
            return CANCELLED
        hint = hint or HINT_MENU
        usable = [i for i, item in enumerate(items) if item.enabled]
        if not usable:
            return CANCELLED
        cursor = cursor if cursor in usable else usable[0]

        def step(start: int, delta: int) -> int:
            position = usable.index(start) if start in usable else 0
            return usable[(position + delta) % len(usable)]

        self._draw(_menu_view(title, items, cursor, hint))
        with keys.raw_mode():
            while True:
                key = keys.read_key(0.3)
                if key is None:
                    continue
                if key == keys.UP:
                    cursor = step(cursor, -1)
                elif key == keys.DOWN:
                    cursor = step(cursor, 1)
                elif key in (keys.ENTER, keys.RIGHT, " "):
                    return items[cursor].value
                elif key in (keys.ESC, "q", "Q") or (on_left and key == keys.LEFT):
                    return CANCELLED
                else:
                    continue
                self._draw(_menu_view(title, items, cursor, hint))

    # ----------------------------------------------------------- text field
    def ask_text(
        self,
        prompt: str,
        *,
        current: str = "",
        validate: Callable[[str], str | None] | None = None,
    ) -> Any:
        """A one-line text field. Returns the text, "" for unset, or CANCELLED."""
        if not self._usable():
            return CANCELLED
        buffer = list(current)
        error = ""

        self._draw(_text_view(prompt, buffer, error))
        with keys.raw_mode():
            while True:
                key = keys.read_key(0.3)
                if key is None:
                    continue
                if key == keys.ENTER:
                    text = "".join(buffer).strip()
                    error = validate(text) if (validate and text) else None
                    if not error:
                        return text
                elif key == keys.ESC:
                    return CANCELLED
                elif key == keys.BACKSPACE:
                    if buffer:
                        buffer.pop()
                    error = ""
                elif key in (keys.LEFT, keys.RIGHT, keys.UP, keys.DOWN):
                    continue
                elif len(key) == 1 and key.isprintable():
                    buffer.append(key)
                    error = ""
                self._draw(_text_view(prompt, buffer, error or ""))

    # --------------------------------------------------------------- slider
    def slider(
        self,
        title: str,
        *,
        value: float,
        minimum: float,
        maximum: float,
        step: float,
        coarse: float | None = None,
        describe: Callable[[float], str] | None = None,
    ) -> Any:
        """Adjust a number with the arrow keys. Returns the value or CANCELLED.

        Left and right move by `step`, up and down by `coarse`, so a range wide
        enough to be useful is still crossable in a few presses.
        """
        if not self._usable():
            return CANCELLED
        coarse = coarse if coarse is not None else step * 10
        # Round to the step, or repeated adds drift into 1.9000000000000001.
        digits = max(0, -math.floor(math.log10(step))) if step < 1 else 0
        value = min(max(value, minimum), maximum)

        def shift(current: float, amount: float) -> float:
            return round(min(max(current + amount, minimum), maximum), digits)

        self._draw(_slider_view(title, value, minimum, maximum, describe))
        with keys.raw_mode():
            while True:
                key = keys.read_key(0.3)
                if key is None:
                    continue
                if key == keys.LEFT:
                    value = shift(value, -step)
                elif key == keys.RIGHT:
                    value = shift(value, step)
                elif key == keys.DOWN:
                    value = shift(value, -coarse)
                elif key == keys.UP:
                    value = shift(value, coarse)
                elif key == keys.ENTER:
                    return value
                elif key in (keys.ESC, "q", "Q"):
                    return CANCELLED
                else:
                    continue
                self._draw(_slider_view(title, value, minimum, maximum, describe))


# ------------------------------------------------------------------- views
def _menu_view(title: str, items: list[Item], cursor: int, hint: str) -> Group:
    lines: list[Any] = []
    if title:
        lines.append(Text(safe(title), style="bold"))
        lines.append(Text(""))
    for index, item in enumerate(items):
        selected = index == cursor
        row = Text()
        row.append(f"  {CURSOR} " if selected else "    ")
        if not item.enabled:
            row.append(safe(item.label), style="dim strike" if selected else "dim")
        else:
            row.append(safe(item.label), style="reverse bold" if selected else "")
        detail = item.reason if not item.enabled else item.detail
        if detail:
            row.append("   ")
            row.append(safe(detail), style="yellow" if not item.enabled else "dim")
        lines.append(row)
    lines.append(Text(""))
    lines.append(Text(safe(hint), style="dim"))
    return Group(*lines)


def _text_view(prompt: str, buffer: list[str], error: str) -> Group:
    typed = Text()
    typed.append(safe("  " + prompt + ": "), style="bold")
    typed.append(safe("".join(buffer)) or " ", style="reverse")
    rows: list[Any] = [typed, Text("")]
    if error:
        rows.append(Text(safe("  " + error), style="red"))
        rows.append(Text(""))
    rows.append(Text("  " + HINT_TEXT, style="dim"))
    return Group(*rows)


def _slider_view(
    title: str,
    value: float,
    minimum: float,
    maximum: float,
    describe: Callable[[float], str] | None,
    width: int = 28,
) -> Group:
    span = maximum - minimum
    filled = round((value - minimum) / span * width) if span else 0
    bar = Text()
    bar.append("  ")
    bar.append(BAR_FULL * filled, style="cyan")
    bar.append(BAR_EMPTY * (width - filled), style="dim")
    bar.append(f"  {value:g}", style="bold")
    if describe:
        bar.append(f"   {describe(value)}", style="dim")

    scale = Text(f"  {minimum:g}" + " " * max(1, width - 6) + f"{maximum:g}", style="dim")
    return Group(
        Text(safe(title), style="bold"),
        Text(""),
        bar,
        scale,
        Text(""),
        Text("  " + HINT_SLIDER, style="dim"),
    )


# --------------------------------------------- one-off helpers (no session)
def choose(title: str, items: list[Item], *, console: Console, **options) -> Any:
    """A single menu outside an editing session, e.g. `record --pick`."""
    with Screen(console) as screen:
        return screen.choose(title, items, **options)


def ask_text(prompt: str, *, console: Console, **options) -> Any:
    with Screen(console) as screen:
        return screen.ask_text(prompt, **options)
