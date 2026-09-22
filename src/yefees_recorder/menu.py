"""Arrow-key menus drawn in one place on screen.

A `Screen` owns a single `rich.live` region for a whole session, so moving
between pages replaces what is on screen instead of printing another block
underneath it.

Every page is built the same way: a *view* that turns state into something
rich can draw, and a *handler* that folds one keypress into new state. The
shared loop in `Screen._run` drains whatever keys are already buffered before
redrawing, which is what keeps a held-down arrow from lagging behind.
"""

from __future__ import annotations

import math
import sys
from typing import Any, Callable, NamedTuple

from rich.color import Color
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from . import keys

CANCELLED = object()


# --------------------------------------------------------------- appearance
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
    "↑↓ move   ⏎ select   ←/⌫ back" if FANCY
    else "up/down move   Enter select   Left/Backspace back"
)
HINT_TEXT = (
    "⏎ accept   ⌫ delete   Esc cancel   (empty = automatic)" if FANCY
    else "Enter accept   Backspace delete   Esc cancel   (empty = automatic)"
)
HINT_SLIDER = (
    "←→ adjust   ↑↓ bigger steps   ⏎ accept   ⌫ back" if FANCY
    else "left/right adjust   up/down bigger steps   Enter accept   Backspace back"
)

# The one colour the interface is built around. Anything rich can parse works:
# a hex value like "#5A4FCF" or a colour name like "blue_violet".
DEFAULT_ACCENT = "#5A4FCF"   # indigo

# Named alternatives offered in the settings menu.
ACCENT_CHOICES = (
    ("Indigo", DEFAULT_ACCENT),
    ("Blue", "#2F6FED"),
    ("Teal", "#0E8A8A"),
    ("Violet", "#8B5CF6"),
    ("Green", "#2E8B57"),
    ("Amber", "#B4690E"),
)


def _rgb(colour: str) -> tuple[int, int, int] | None:
    try:
        red, green, blue = Color.parse(colour).get_truecolor()
    except Exception:
        return None
    return red, green, blue


def is_colour(text: str) -> bool:
    """Whether rich can make sense of this as a colour."""
    return bool(text) and _rgb(text) is not None


def readable_on(background: str) -> str:
    """Black or white, whichever can actually be read on `background`.

    Picking one by eye is how a highlight ends up as white text on a pale
    block. Relative luminance decides it instead, so any accent stays legible.
    """
    parsed = _rgb(background)
    if parsed is None:
        return "white"
    red, green, blue = (channel / 255 for channel in parsed)
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return "black" if luminance > 0.55 else "white"


def _lighten(colour: str, amount: float = 0.45) -> str:
    """A paler version, for use as text where the accent itself would be dark."""
    parsed = _rgb(colour)
    if parsed is None:
        return colour
    channels = (round(channel + (255 - channel) * amount) for channel in parsed)
    return "#{:02x}{:02x}{:02x}".format(*channels)


# How the whole interface looks, rebuilt whenever the accent changes.
# Everything here has to stay legible on both a dark and a light terminal.
#
# Ordinary text deliberately carries no colour at all: the terminal's own
# foreground is the one colour guaranteed to contrast with its background.
THEME: dict[str, str] = {}

# What a detail column means, rather than what colour it is. The basic ANSI
# colours are used on purpose: a terminal theme adjusts them to stay readable
# against its own background, which fixed 256-colour greys do not.
TONES = {
    "muted": "bright_black",
    "good": "green",
    "warn": "yellow",
    "info": "cyan",
    "loud": "bold red",
}
TONES_SELECTED: dict[str, str] = {}


def apply_theme(accent: str | None = None) -> None:
    """Rebuild the palette around `accent`. Unparseable colours fall back."""
    accent = accent or DEFAULT_ACCENT
    if _rgb(accent) is None:
        accent = DEFAULT_ACCENT
    on_accent = readable_on(accent)
    text_accent = _lighten(accent)

    THEME.update({
        "accent": accent,
        "title": f"bold {text_accent}",
        "cursor": f"bold {text_accent}",
        "selected": f"bold {on_accent} on {accent}",
        "label": "",                    # terminal default: maximum contrast
        "disabled": "bright_black",
        "reason": "yellow",
        "hint": "bright_black",
        "error": "bold red",
        "value": "bold",
        "scale": "bright_black",
        "field": f"bold {on_accent} on {accent}",
    })
    # Detail colours vanish against the selection, so a selected row keeps one.
    TONES_SELECTED.clear()
    TONES_SELECTED.update(dict.fromkeys(TONES, f"{on_accent} on {accent}"))


apply_theme()


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


def gain_to_db(gain: float) -> float:
    """A volume multiplier as decibels. 0 becomes the quietest value we offer."""
    return 20 * math.log10(gain) if gain > 0 else MIN_DB


def db_to_gain(decibels: float) -> float:
    return round(10 ** (decibels / 20), 3)


MIN_DB, MAX_DB = -40.0, 24.0


def as_decibels(gain: float) -> str:
    """A multiplier described the way audio people read it."""
    if gain <= 0:
        return "silent"
    if abs(gain - 1.0) < 1e-9:
        return "unchanged"
    return f"{20 * math.log10(gain):+.1f} dB"


class Item(NamedTuple):
    """One row. `value` is returned when it is chosen."""

    label: str
    value: Any = None
    detail: str = ""       # shown to the right, e.g. the current setting
    enabled: bool = True
    reason: str = ""       # why it is disabled, shown instead of `detail`
    tone: str = "muted"    # what the detail means; see TONES


TYPE_IT = object()   # the slider asking to be given an exact value instead


class Done(NamedTuple):
    """Returned by a handler to end a page with this result."""

    value: Any


# -------------------------------------------------------------------- views
def _heading(title: str) -> list[Text]:
    return [Text(safe(title), style=THEME["title"]), Text("")] if title else []


def _footer(hint: str) -> list[Text]:
    return [Text(""), Text(safe(hint), style=THEME["hint"])]


def _item_row(item: Item, selected: bool) -> Text:
    """One row, highlighted as a single block when selected.

    Colouring only part of a selected row leaves it looking broken, so the
    cursor, label and detail all share the selection style.
    """
    palette = TONES_SELECTED if selected else TONES
    row = Text(style=THEME["selected"] if selected else "")
    row.append(f"  {CURSOR} " if selected else "    ",
               style="" if selected else THEME["cursor"])

    if not item.enabled:
        row.append(safe(item.label), style="" if selected else THEME["disabled"])
        detail = item.reason
        tone = "" if selected else THEME["reason"]
    else:
        row.append(safe(item.label), style="bold" if selected else THEME["label"])
        detail = item.detail
        tone = palette.get(item.tone, palette["muted"])

    if detail:
        row.append("   ")
        row.append(safe(detail), style=tone)
    return row


def _menu_view(title: str, items: list[Item], cursor: int, hint: str) -> Group:
    rows = [_item_row(item, index == cursor) for index, item in enumerate(items)]
    return Group(*_heading(title), *rows, *_footer(hint))


def _text_view(prompt: str, typed: str, error: str) -> Group:
    field = Text()
    field.append(safe("  " + prompt + ": "), style=THEME["title"])
    field.append(safe(typed) or " ", style=THEME["field"])
    problem = [Text(safe("  " + error), style=THEME["error"]), Text("")] if error else []
    return Group(field, Text(""), *problem, Text("  " + HINT_TEXT, style=THEME["hint"]))


def _bar_style(state: "SliderState") -> str:
    """Colour the bar by what the value means where that is known.

    A decibel slider cannot use "how far along the range" - 0 dB sits well past
    halfway on a -40..+24 scale yet is the neutral value - so a caller that
    knows where trouble starts passes thresholds instead.
    """
    if state.thresholds:
        caution, danger = state.thresholds
        if state.value >= danger:
            return "red"
        return "yellow" if state.value >= caution else "green"

    span = state.maximum - state.minimum
    share = (state.value - state.minimum) / span if span else 0
    if share > 0.75:
        return "red"
    return "yellow" if share > 0.45 else "green"


def _slider_view(state: "SliderState", width: int = 28) -> Group:
    span = state.maximum - state.minimum
    filled = round((state.value - state.minimum) / span * width) if span else 0

    bar = Text("  ")
    bar.append(BAR_FULL * filled, style=_bar_style(state))
    bar.append(BAR_EMPTY * (width - filled), style=THEME["disabled"])
    bar.append(f"  {state.value:g}", style=THEME["value"])
    if state.describe:
        bar.append(f"   {state.describe(state.value)}", style=TONES["info"])

    ends = f"  {state.minimum:g}" + " " * max(1, width - 6) + f"{state.maximum:g}"
    hint = HINT_SLIDER + ("   t type a value" if state.allow_typing else "")
    return Group(*_heading(state.title), bar, Text(ends, style=THEME["scale"]), *_footer(hint))


# ------------------------------------------------------------- page state
class MenuState:
    def __init__(self, items: list[Item], cursor: int) -> None:
        self.items = items
        self.selectable = [i for i, item in enumerate(items) if item.enabled]
        self.cursor = cursor if cursor in self.selectable else self.selectable[0]

    def move(self, delta: int) -> None:
        position = self.selectable.index(self.cursor)
        self.cursor = self.selectable[(position + delta) % len(self.selectable)]

    @property
    def chosen(self) -> Any:
        return self.items[self.cursor].value


class TextState:
    def __init__(self, current: str, validate: Callable[[str], str | None] | None) -> None:
        self.typed = current
        self.validate = validate
        self.error = ""

    def accept(self) -> Done | None:
        text = self.typed.strip()
        self.error = (self.validate(text) if self.validate and text else None) or ""
        return None if self.error else Done(text)


class SliderState:
    def __init__(self, title: str, value: float, minimum: float, maximum: float,
                 step: float, coarse: float, describe, allow_typing: bool = False,
                 thresholds: tuple[float, float] | None = None) -> None:
        self.title, self.minimum, self.maximum = title, minimum, maximum
        self.step, self.coarse, self.describe = step, coarse, describe
        self.allow_typing = allow_typing
        self.thresholds = thresholds
        # Round to the step, or repeated presses drift to 1.9000000000000001.
        self.digits = max(0, -math.floor(math.log10(step))) if step < 1 else 0
        self.value = self._clamp(value)

    def _clamp(self, value: float) -> float:
        return round(min(max(value, self.minimum), self.maximum), self.digits)

    def shift(self, amount: float) -> None:
        self.value = self._clamp(self.value + amount)


# ----------------------------------------------------------------- handlers
# Ways to leave a page. Backspace is included because reaching for it to go
# back is a reflex; the text field keeps it for deleting instead.
BACK_KEYS = (keys.ESC, keys.BACKSPACE, "q", "Q")


def _handle_menu(state: MenuState, key: str, on_left: bool) -> Done | None:
    if key == keys.UP:
        state.move(-1)
    elif key == keys.DOWN:
        state.move(1)
    elif key in (keys.ENTER, keys.RIGHT, " "):
        return Done(state.chosen)
    elif key in BACK_KEYS or (on_left and key == keys.LEFT):
        return Done(CANCELLED)
    return None


def _handle_text(state: TextState, key: str) -> Done | None:
    if key == keys.ENTER:
        return state.accept()
    if key == keys.ESC:
        return Done(CANCELLED)
    if key == keys.BACKSPACE:
        state.typed, state.error = state.typed[:-1], ""
    elif len(key) == 1 and key.isprintable():
        state.typed, state.error = state.typed + key, ""
    return None


def _handle_slider(state: SliderState, key: str, allow_typing: bool) -> Done | None:
    steps = {keys.LEFT: -state.step, keys.RIGHT: state.step,
             keys.DOWN: -state.coarse, keys.UP: state.coarse}
    if key in steps:
        state.shift(steps[key])
    elif key == keys.ENTER:
        return Done(state.value)
    elif allow_typing and key in ("t", "T"):
        return Done(TYPE_IT)
    elif key in BACK_KEYS:
        return Done(CANCELLED)
    return None


# ------------------------------------------------------------------- screen
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

    def _draw(self, renderable) -> None:
        if self._live is not None:
            self._live.update(renderable, refresh=True)
        else:
            self.console.print(renderable)

    @staticmethod
    def _fold_in_waiting_keys(key: str, handle: Callable[[str], Done | None]) -> Done | None:
        """Apply `key` and everything already buffered behind it."""
        while key is not None:
            finished = handle(key)
            if finished is not None:
                return finished
            key = keys.read_key(0)
        return None

    def notice(self, message: str) -> None:
        """Say what is happening during something slow, so it is not a freeze."""
        self._draw(Text(f"  {safe(message)}", style=THEME["title"]))

    def _run(self, view: Callable[[], Any], handle: Callable[[str], Done | None]) -> Any:
        """Read keys until a handler finishes, redrawing once per batch.

        Keys already waiting are all folded in before redrawing: without that,
        holding an arrow down queues one repaint per repeat and the highlight
        visibly trails the keyboard.
        """
        if not keys.interactive():
            return CANCELLED  # no terminal: no key can ever arrive, so do not spin

        self._draw(view())
        with keys.raw_mode():
            while True:
                finished = self._fold_in_waiting_keys(keys.read_key(0.3), handle)
                if finished is not None:
                    return finished.value
                self._draw(view())

    # ------------------------------------------------------------ the pages
    def choose(self, title: str, items: list[Item], *, cursor: int = 0,
               hint: str = "", on_left: bool = True) -> Any:
        """Show `items` and return the chosen value, or CANCELLED.

        Left, Esc and q all back out, which is what makes nested pages feel
        like one screen rather than a stack of prompts.
        """
        if not any(item.enabled for item in items):
            return CANCELLED
        state = MenuState(items, cursor)
        hint = hint or HINT_MENU
        return self._run(
            lambda: _menu_view(title, state.items, state.cursor, hint),
            lambda key: _handle_menu(state, key, on_left),
        )

    def ask_text(self, prompt: str, *, current: str = "",
                 validate: Callable[[str], str | None] | None = None) -> Any:
        """A one-line text field. Returns the text, "" for unset, or CANCELLED."""
        state = TextState(current, validate)
        return self._run(
            lambda: _text_view(prompt, state.typed, state.error),
            lambda key: _handle_text(state, key),
        )

    def slider(self, title: str, *, value: float, minimum: float, maximum: float,
               step: float, coarse: float | None = None, describe=None,
               allow_typing: bool = True, unit: str = "",
               thresholds: tuple[float, float] | None = None) -> Any:
        """Adjust a number with the arrow keys. Returns the value or CANCELLED.

        Left and right move by `step`, up and down by `coarse`, so a range wide
        enough to be useful is still crossable in a few presses. Pressing t
        gives a text field instead, for a value the steps cannot land on.
        """
        state = SliderState(title, value, minimum, maximum, step,
                            coarse if coarse is not None else step * 10, describe,
                            allow_typing, thresholds)
        while True:
            result = self._run(lambda: _slider_view(state),
                               lambda key: _handle_slider(state, key, allow_typing))
            if result is not TYPE_IT:
                return result
            typed = self.ask_text(f"{title} ({state.minimum:g} to {state.maximum:g}{unit})",
                                  current=f"{state.value:g}",
                                  validate=_number_within(state.minimum, state.maximum))
            if typed not in (CANCELLED, ""):
                state.value = state._clamp(float(typed))


def _number_within(minimum: float, maximum: float):
    """Reject anything that is not a number inside the slider's own range."""
    def check(text: str) -> str | None:
        try:
            value = float(text)
        except ValueError:
            return "Enter a number."
        if not minimum <= value <= maximum:
            return f"Enter something between {minimum:g} and {maximum:g}."
        return None
    return check


# --------------------------------------------- one-off pages (no session)
def choose(title: str, items: list[Item], *, console: Console, **options) -> Any:
    """A single menu outside an editing session, e.g. `record --pick`."""
    with Screen(console) as screen:
        return screen.choose(title, items, **options)


def ask_text(prompt: str, *, console: Console, **options) -> Any:
    with Screen(console) as screen:
        return screen.ask_text(prompt, **options)
