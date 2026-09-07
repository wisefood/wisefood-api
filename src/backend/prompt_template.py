"""What a Langfuse prompt is made of, for people reading it in the console.

Langfuse stores a template; the SDKs fill it at runtime. Someone reading the
template in the console has neither the SDK nor the runtime, so this module
does the reading for them: which variables it expects, where messages will be
spliced in, which other prompts it pulls in, and which brace convention it
follows — because this platform has two.

Langfuse's own convention is mustache, ``{{variable}}``. FoodChat's prompts
predate that and use Python ``str.format`` — ``{variable}``, with ``{{`` and
``}}`` as literal braces — and FoodChat substitutes them itself, on purpose
(see the note above ``_Prompt`` in foodchat/src/prompts.py). A console that
only knew mustache would read a FoodChat prompt's escaped JSON braces as
variables and its real variables as nothing. So the convention is detected
per prompt and reported, and the console says which one it found.

Pure functions, no I/O. Nothing here should ever be the reason a prompt fails
to load — every function takes whatever Langfuse returned and does its best.
"""
import re
from typing import Any, Dict, List, Optional

#: ``{{ name }}`` — Langfuse variables. Whitespace inside the braces is allowed
#: by the SDKs' compile, so it is allowed here.
MUSTACHE = re.compile(r"{{\s*([A-Za-z_][\w.-]*)\s*}}")
#: ``{name}`` not doubled on either side — Python format fields. A bare
#: ``{}`` (positional) or ``{0}`` is not something a prompt uses.
FORMAT = re.compile(r"(?<!{){([A-Za-z_]\w*)(?::[^{}]*)?}(?!})")
#: ``@@@langfusePrompt:name=other|version=2@@@`` — a reference to another
#: prompt, present only when the prompt is fetched with ``resolve=false``.
REFERENCE = re.compile(r"@@@langfusePrompt:([^@]+?)@@@")


def _texts(prompt: Optional[Dict[str, Any]]) -> List[str]:
    """Every piece of template text in a prompt, text or chat."""
    body = (prompt or {}).get("prompt")
    if isinstance(body, str):
        return [body]
    if isinstance(body, list):
        return [
            m.get("content") for m in body
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        ]
    return []


def _unique(names) -> List[str]:
    seen: List[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def variables(prompt: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Variable names by convention: ``mustache`` and ``format``.

    Both are always reported; ``convention()`` decides which one the prompt
    actually follows. Reference tags are stripped first so a ``{{`` inside one
    is not mistaken for a variable.
    """
    texts = [REFERENCE.sub("", text) for text in _texts(prompt)]
    mustache = _unique(m.group(1) for text in texts for m in MUSTACHE.finditer(text))
    # In format convention, ``{{name}}`` is a literal ``{name}`` — not a field.
    # Strip mustache runs before looking for single braces so the two do not
    # count the same characters.
    stripped = [MUSTACHE.sub("", text) for text in texts]
    fmt = _unique(m.group(1) for text in stripped for m in FORMAT.finditer(text))
    return {"mustache": mustache, "format": fmt}


def convention(found: Dict[str, List[str]]) -> str:
    """Which brace convention a prompt follows, from what ``variables`` found.

    Mustache wins when both are present: a prompt an engineer wrote for
    Langfuse may quote a JSON example with single braces, but a FoodChat
    prompt never contains a ``{{name}}`` that is meant as a variable — that
    would be a literal ``{name}`` to ``str.format``.
    """
    if found["mustache"]:
        return "mustache"
    if found["format"]:
        return "format"
    return "none"


def placeholders(prompt: Optional[Dict[str, Any]]) -> List[str]:
    """Names of message placeholders in a chat prompt, in order."""
    body = (prompt or {}).get("prompt")
    if not isinstance(body, list):
        return []
    return _unique(
        m.get("name") for m in body
        if isinstance(m, dict) and m.get("type") == "placeholder" and m.get("name")
    )


def references(prompt: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Other prompts this one pulls in, as ``{name, version|label}`` dicts.

    Only visible on an unresolved fetch; a resolved prompt has them inlined,
    and Langfuse reports that in ``resolutionGraph`` instead.
    """
    out: List[Dict[str, str]] = []
    for text in _texts(prompt):
        for m in REFERENCE.finditer(text):
            fields: Dict[str, str] = {}
            for pair in m.group(1).split("|"):
                key, _, value = pair.partition("=")
                if key and value:
                    fields[key.strip()] = value.strip()
            if fields.get("name") and fields not in out:
                out.append(fields)
    return out


def describe(prompt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Everything the console shows about a template, in one dict."""
    found = variables(prompt)
    kind = convention(found)
    return {
        "convention": kind,
        "variables": found[kind] if kind != "none" else [],
        "placeholders": placeholders(prompt),
        "references": references(prompt),
        "is_chat": isinstance((prompt or {}).get("prompt"), list),
    }
