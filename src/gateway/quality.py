"""Quality SLI: is a completion the gateway returns schema-valid and usable?

A completion is ``valid`` when the response body validates against ``ChatCompletionOut`` (the
OpenAI-compatible shape the gateway promises), its content is non-empty after stripping whitespace, and it
was not cut off by the token limit (``finish_reason != "length"``). Task accuracy is out of scope: the
gateway cannot know the right answer, only whether the answer is well-formed.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Msg(BaseModel):
    role: Literal["assistant"]
    content: str


class _Choice(BaseModel):
    index: int = Field(ge=0)
    message: _Msg
    finish_reason: str


class _Usage(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class _GatewayMeta(BaseModel):
    model_config = ConfigDict(extra="allow")
    request_id: str
    provider: str
    request_class: str
    failovers: int = Field(ge=0)
    attempts: list[dict[str, Any]]


class ChatCompletionOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1)
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage
    gateway: _GatewayMeta


def classify_completion(body: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(valid, reason)``; reason is ``ok``, ``schema``, ``empty`` or ``truncated``."""
    try:
        out = ChatCompletionOut.model_validate(body)
    except ValidationError:
        return False, "schema"
    choice = out.choices[0]
    if not choice.message.content.strip():
        return False, "empty"
    if choice.finish_reason == "length":
        return False, "truncated"
    return True, "ok"
