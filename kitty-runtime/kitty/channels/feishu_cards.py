"""Feishu interactive message card builders.

Builders return the plain-dict card JSON Feishu expects for
``msg_type="interactive"``. They are pure functions with no runtime
dependency, so scenario packages can compose cards freely and tests can
assert on structure.

Button ``value`` dicts are echoed back verbatim inside the
``card.action.trigger`` event, so routing keys belong there, e.g.
``{"action": "resolve_ticket", "ticket_id": "123", "text": "已解决"}``.
The optional ``text`` key doubles as the synthetic user message the agent
sees when the button is clicked (see ``FeishuEventParser``).
"""
from __future__ import annotations

from typing import Any


TEMPLATE_BLUE = "blue"
TEMPLATE_GREEN = "green"
TEMPLATE_RED = "red"
TEMPLATE_GREY = "grey"
TEMPLATE_ORANGE = "orange"


def _header(title: str, template: str) -> dict[str, Any]:
    return {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
    }


def _note(note: str) -> dict[str, Any]:
    """A grey context footer, e.g. '✅ 已由 @张三 处理'."""

    return {"tag": "note", "elements": [{"tag": "lark_md", "content": note}]}


def text_card(
    text: str,
    *,
    title: str | None = None,
    template: str = TEMPLATE_BLUE,
    markdown: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    """Optional header + one text block + optional note footer."""

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md" if markdown else "plain_text",
                "content": text,
            },
        }
    ]
    if note:
        elements.append(_note(note))
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }
    if title:
        card["header"] = _header(title, template)
    return card


def button_card(
    text: str,
    buttons: list[dict[str, Any]],
    *,
    title: str | None = None,
    template: str = TEMPLATE_BLUE,
    note: str | None = None,
) -> dict[str, Any]:
    """A text block plus one row of action buttons.

    Each button entry::

        {
            "text": "解决",                                # label
            "value": {"action": "resolve", "text": "已解决"},  # echoed on click
            "type": "primary" | "default" | "danger",      # optional
            "url": "https://...",                          # optional link button
        }
    """

    actions: list[dict[str, Any]] = []
    for button in buttons:
        item: dict[str, Any] = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": str(button.get("text", ""))},
            "type": button.get("type", "default"),
        }
        if button.get("url"):
            item["url"] = button["url"]
        if "value" in button:
            item["value"] = button["value"]
        actions.append(item)

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": text}},
        {"tag": "action", "actions": actions},
    ]
    if note:
        elements.append(_note(note))
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }
    if title:
        card["header"] = _header(title, template)
    return card


def select_card(
    text: str,
    *,
    placeholder: str,
    options: list[dict[str, str]],
    value: dict[str, Any] | None = None,
    title: str | None = None,
    template: str = TEMPLATE_BLUE,
    note: str | None = None,
) -> dict[str, Any]:
    """A text block plus a static single-select menu.

    ``options`` is a list of ``{"text": "...", "value": "..."}``; ``value``
    is a base dict merged into the selection callback payload.
    """

    select_element: dict[str, Any] = {
        "tag": "select_static",
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "options": [
            {
                "text": {"tag": "plain_text", "content": option["text"]},
                "value": option["value"],
            }
            for option in options
        ],
    }
    if value is not None:
        select_element["value"] = value

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": text}},
        {"tag": "action", "actions": [select_element]},
    ]
    if note:
        elements.append(_note(note))
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "elements": elements,
    }
    if title:
        card["header"] = _header(title, template)
    return card
