"""Terminal UI primitives used by the interactive menu.

This module is the only place in the codebase that talks to the underlying
UI library (currently `textual`). The rest of the code calls `run_session()`,
`select()`, `confirm()`, `menu()`, `prompt()`, `multi_select()`, `clear()`
and the suggester factories, and never imports `textual` directly.

This is the seam meant to be swapped between branches: the branch targeting
`bullet` reimplements this file with the same function signatures, and the
rest of the codebase stays identical.

Architecture: a single persistent `App` runs for the whole `otools` session,
occupying the main thread (Textual's Linux driver installs signal handlers,
which only works there), while the business logic in `main.py` runs in a
background thread. Everything it prints is captured automatically into one
continuous scrolling log (via `App.begin_capture_print`) instead of the
window opening and closing for every single prompt. Menus/confirmations/
free-text prompts/checkbox lists are shown as modal screens pushed on top of
that same log, so the window itself never closes — only the modal
appears/disappears. The bridge between the business-logic thread and the
app's own asyncio loop is `App.call_from_thread` plus a worker running
`push_screen_wait`.
"""
import concurrent.futures
import os
import threading

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, RichLog, SelectionList
from textual.widgets.option_list import Option

_CSS = """
_DialogScreen {
    align: center middle;
}
_DialogScreen Vertical {
    width: auto;
    height: auto;
    align: center middle;
}
#ui-prompt {
    padding: 0 0 1 0;
    text-style: bold;
}
OptionList, SelectionList, Input {
    width: auto;
    min-width: 40;
    max-width: 90%;
}
OptionList, SelectionList {
    border: round $accent;
}
"""


# --------------------------------------------------------------------------
# Suggesters (ghost-text completion for free-text prompts)
# --------------------------------------------------------------------------

class _PathSuggester(Suggester):
    """Completes filesystem paths, one path segment at a time."""

    def __init__(self):
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        expanded = os.path.expanduser(value)
        directory = os.path.dirname(expanded) or '.'
        base = os.path.basename(expanded)
        try:
            entries = os.listdir(directory)
        except OSError:
            return None
        matches = sorted(e for e in entries if e.startswith(base))
        if not matches or (len(matches) == 1 and matches[0] == base):
            return None
        match = matches[0]
        suggestion = value[: len(value) - len(base)] + match if base else value + match
        if os.path.isdir(os.path.join(directory, match)):
            suggestion += '/'
        return suggestion


def path_suggester():
    """Return a suggester that completes filesystem paths."""
    return _PathSuggester()


# --------------------------------------------------------------------------
# The persistent log
# --------------------------------------------------------------------------

class _CaptureLog(RichLog):
    """A RichLog that also receives everything printed via print()."""

    def on_print(self, event: events.Print) -> None:
        self.write(event.text.rstrip("\n"))


# --------------------------------------------------------------------------
# Modal screens (shared by the persistent app and the standalone fallback)
# --------------------------------------------------------------------------

class _DialogScreen(ModalScreen):
    """Shared base for the small centered/translucent modal screens below.

    Textual's CSS type selectors only apply reliably when the selector names
    the exact class declaring the rule — a rule for `Screen` (or any other
    ancestor name) written inside a subclass's own DEFAULT_CSS is silently
    ignored. So the shared layout rules live here, on their own base class
    matching its own name, and every dialog screen subclasses this instead
    of repeating `DEFAULT_CSS = _CSS` (which didn't actually work).
    """

    DEFAULT_CSS = _CSS


class _SelectScreen(_DialogScreen):
    """One-shot menu: highlights/selects one of `choices`, then dismisses with it."""

    def __init__(self, choices, prompt=None):
        super().__init__()
        self._choices = list(choices)
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            if self._prompt:
                yield Label(str(self._prompt), id="ui-prompt")
            yield OptionList(*(Option(str(choice)) for choice in self._choices))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._choices[event.option_index])


class _ConfirmScreen(_DialogScreen):
    """One-shot yes/no prompt."""

    BINDINGS = [
        ("y", "answer('yes')", "Yes"),
        ("n", "answer('no')", "No"),
    ]

    def __init__(self, prompt, default='n'):
        super().__init__()
        self._prompt = prompt
        self._default_yes = str(default).lower().startswith('y')

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(str(self._prompt), id="ui-prompt")
            yield OptionList(Option("Yes", id="yes"), Option("No", id="no"))

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0 if self._default_yes else 1

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id == "yes")

    def action_answer(self, choice: str) -> None:
        self.dismiss(choice == "yes")


class _MenuScreen(_DialogScreen):
    """Main navigation menu: a centered overlay, same as every other screen.

    The connected DB is shown directly in this screen's own label (so it's
    always visible, regardless of terminal rendering of the translucent
    overlay) and also set on `self.app` (shown by the Header on the
    persistent base screen behind this one).
    """

    def __init__(self, options, env_name=None):
        super().__init__()
        self._options = list(options)
        self._env_name = env_name

    def compose(self) -> ComposeResult:
        title = f"Odootools (env: {self._env_name})" if self._env_name else "Odootools"
        with Vertical():
            yield Label(title, id="ui-prompt")
            yield OptionList(*(Option(str(option)) for option in self._options))

    def on_mount(self) -> None:
        self.app.title = "Odootools"
        self.app.sub_title = f"env: {self._env_name}" if self._env_name else ""

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._options[event.option_index])


class _MultiSelectScreen(_DialogScreen):
    """Checkbox-style multi-select list.

    Space toggles the highlighted item; the Confirm/Cancel buttons (also
    reachable via Ctrl+S / Escape) close the dialog.
    """

    DEFAULT_CSS = """
    _MultiSelectScreen #ui-dialog-buttons {
        width: auto;
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    _MultiSelectScreen #ui-dialog-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        ("ctrl+s", "confirm", "Confirm"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, choices, prompt=None):
        super().__init__()
        self._choices = list(choices)
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            hint = "space: toggle"
            label = f"{self._prompt}\n({hint})" if self._prompt else hint
            yield Label(label, id="ui-prompt")
            yield SelectionList(*((str(choice), choice) for choice in self._choices))
            with Horizontal(id="ui-dialog-buttons"):
                yield Button("Confirm", variant="success", id="confirm-btn")
                yield Button("Cancel", variant="error", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-btn":
            self.action_confirm()
        else:
            self.action_cancel()

    def action_confirm(self) -> None:
        self.dismiss(self.query_one(SelectionList).selected)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _PromptScreen(_DialogScreen):
    """Free-text input, replacing plain input() calls."""

    def __init__(self, message, default='', suggester=None):
        super().__init__()
        self._message = message
        self._default = default
        self._suggester = suggester

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(str(self._message), id="ui-prompt")
            yield Input(value=self._default, suggester=self._suggester)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


# --------------------------------------------------------------------------
# The persistent app + the thread bridge
# --------------------------------------------------------------------------

class _App(App):
    """The single persistent window for the whole session: a continuous log,
    with menus/prompts shown as modal overlays on top of it."""

    CSS = _CSS

    def compose(self) -> ComposeResult:
        yield Header()
        yield _CaptureLog()
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Odootools"
        self.begin_capture_print(self.query_one(_CaptureLog))
        _ready.set()

    def ask_threadsafe(self, screen):
        """Push `screen` from a worker and return a Future with its result.

        Must be called via `call_from_thread` from the business-logic
        thread: `push_screen_wait` requires an active worker, which
        `call_from_thread` alone does not provide.
        """
        future: "concurrent.futures.Future" = concurrent.futures.Future()

        async def _runner():
            try:
                future.set_result(await self.push_screen_wait(screen))
            except BaseException as exc:
                future.set_exception(exc)

        self.run_worker(_runner())
        return future


class _OneShotApp(App):
    """Standalone fallback used when no persistent session is running."""

    def __init__(self, screen):
        super().__init__()
        self._screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._screen, callback=self.exit)


_app = None
_ready = threading.Event()


def run_session(target):
    """Run `target()` while the persistent Textual app owns the whole session.

    Textual's Linux driver installs signal handlers (SIGTSTP/SIGCONT), which
    only works in the main thread of the main interpreter — so the app must
    run in *this* (assumed main) thread, blocking until it exits. `target`
    (the actual business logic: `main.py`'s `_run()`) runs in a background
    thread instead, calling select()/confirm()/menu()/prompt()/print() as
    usual; those bridge back into the app via `call_from_thread`.
    """
    global _app
    _ready.clear()
    app = _App()
    _app = app

    errors = []

    def _worker():
        if not _ready.wait(timeout=10):
            errors.append(RuntimeError("Textual app failed to start within 10 seconds"))
            return
        try:
            target()
        except BaseException as exc:
            errors.append(exc)
        finally:
            try:
                app.call_from_thread(app.exit)
            except Exception:
                pass

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    try:
        app.run()
    finally:
        _app = None
        thread.join()

    if errors:
        raise errors[0]


def _ask(screen):
    if _app is not None:
        future = _app.call_from_thread(_app.ask_threadsafe, screen)
        return future.result()
    return _OneShotApp(screen).run()


def select(choices, prompt=None):
    """Show a single-choice menu and return the selected item."""
    return _ask(_SelectScreen(choices, prompt))


def confirm(prompt, default='n'):
    """Show a yes/no prompt and return the user's choice as a bool."""
    return _ask(_ConfirmScreen(prompt, default))


def menu(options, env_name=None):
    """Render the main navigation menu and return the chosen option."""
    return _ask(_MenuScreen(options, env_name))


def prompt(message, default='', suggester=None):
    """Show a free-text input prompt and return what the user typed."""
    return _ask(_PromptScreen(message, default, suggester))


def multi_select(choices, prompt=None):
    """Show a checkbox list and return the selected items, or None if cancelled."""
    return _ask(_MultiSelectScreen(choices, prompt))


def clear():
    """Clear the screen between actions.

    No-op here: the persistent app's log keeps scrolling with the full
    history visible (that's the point), and each modal is a translucent
    overlay rather than a new window, so there's nothing to clear.
    """
