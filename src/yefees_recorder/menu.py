"""Arrow-key menus.

rich draws the list and `keys` reads the arrows, so there is no dependency
here beyond what the CLI already uses. Menus redraw in place through
`rich.live` rather than scrolling the terminal.
"""

from __future__ import annotations

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
    arrows and pointers, which would crash the menu rather than look plain.
    """
    try:
        text.encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError, AttributeError):
        return False
    return True


FANCY = _encodable("\u276f\u2191\u2193\u23ce\u2190\u232b")

CURSOR = "\u276f" if FANCY else ">"
HINT_MENU = (
    "\u2191\u2193 move   \u23ce select   \u2190 back" if FANCY
    else "up/down move   Enter select   Left back"
)
HINT_TEXT = (
    "\u23ce accept   \u232b delete   Esc cancel   (empty = automatic)" if FANCY
    else "Enter accept   Backspace delete   Esc cancel   (empty = automatic)"
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


def _render(title: str, items: list[Item], cursor: int, hint: str) -> Group:
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


def choose(
    title: str,
    items: list[Item],
    *,
    console: Console,
    cursor: int = 0,
    hint: str = "",
    on_left: bool = True,
) -> Any:
    """Show `items` and return the chosen value, or CANCELLED.

    Left arrow, Esc and q all back out, which is what makes nested menus feel
    like one thing rather than a stack of prompts.
    """
    hint = hint or HINT_MENU
    usable = [i for i, item in enumerate(items) if item.enabled]
    if not usable:
        return CANCELLED
    cursor = cursor if cursor in usable else usable[0]

    def step(start: int, delta: int) -> int:
        position = usable.index(start) if start in usable else 0
        return usable[(position + delta) % len(usable)]

    with Live(_render(title, items, cursor, hint), console=console, auto_refresh=False) as live:
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
                live.update(_render(title, items, cursor, hint), refresh=True)


def ask_text(
    prompt: str,
    *,
    console: Console,
    current: str = "",
    validate: Callable[[str], str | None] | None = None,
) -> Any:
    """A one-line text field with the same feel as `choose`.

    Returns the text (possibly empty, meaning "unset") or CANCELLED. `validate`
    returns an error message to reject a value, or None to accept it.
    """
    buffer = list(current)
    error = ""

    def view() -> Group:
        typed = Text()
        typed.append(safe("  " + prompt + ": "), style="bold")
        typed.append(safe("".join(buffer)) or " ", style="reverse")
        rows: list[Any] = [typed, Text("")]
        if error:
            rows.append(Text(safe("  " + error), style="red"))
            rows.append(Text(""))
        rows.append(Text("  " + HINT_TEXT, style="dim"))
        return Group(*rows)

    with Live(view(), console=console, auto_refresh=False) as live:
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
                live.update(view(), refresh=True)
