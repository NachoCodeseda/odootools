"""Terminal UI primitives used by the interactive menu.

This module is the only place in the codebase that talks to the underlying
UI library (currently `bullet`). The rest of the code calls `select()` and
`confirm()` and never imports `bullet` directly.

This is the seam meant to be swapped between branches: a branch targeting a
different library (e.g. `contextual`) only needs to reimplement this file
with the same function signatures, and the rest of the codebase stays
identical.
"""
from bullet import Bullet, YesNo


def select(choices, prompt=None):
    """Show a single-choice menu and return the selected item."""
    kwargs = {'choices': choices}
    if prompt is not None:
        kwargs['prompt'] = prompt
    return Bullet(**kwargs).launch()


def confirm(prompt, default='n'):
    """Show a yes/no prompt and return the user's choice as a bool."""
    return YesNo(prompt, default).launch()
