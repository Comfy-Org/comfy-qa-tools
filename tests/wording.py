"""What the tool said, with the line breaks taken out.

`say` wraps prose at a fixed 96 columns — refusals, warnings and explanations —
because 112 of the message literals in `comfy_qa/` are longer than that before a
single value is interpolated, and until it did, each one reached the terminal as
one unbroken line for the terminal to fold wherever the window happened to end.

That makes `assert "nothing has started ComfyUI there" in result.output` two
assertions where the author wrote one: that the tool says the sentence, and that
the sentence does not have a line break in the middle of it. The second was
never intended and is not a property of the tool — it is a property of a width
and of how long the host's name happened to be. Sixteen assertions across six
files were making it, and every one of them went red for a wrap rather than for
a wording.

So: `said(result)` collapses the run's whitespace and the assertion says the one
thing it meant. **It is for sentences, never for layout** — it flattens the
eight-space `to fix:` alignment, the `#` note columns and every padded table
with it, so a test about where something sits on the line must read
`result.output` as it comes.
"""

from __future__ import annotations


def said(printed) -> str:
    """Everything a run printed, as one line. A `Result`, or any text."""
    return " ".join(str(getattr(printed, "output", printed)).split())
