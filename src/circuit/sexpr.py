"""Minimal generic s-expression tokenizer and parser."""

from __future__ import annotations

SExpr = str | list["SExpr"]


class SExprError(ValueError):
    """Raised when an s-expression is malformed."""


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
        elif character in "()":
            tokens.append(character)
            index += 1
        elif character == '"':
            index += 1
            value: list[str] = []
            while index < len(text):
                character = text[index]
                if character == '"':
                    index += 1
                    break
                if character == "\\":
                    index += 1
                    if index >= len(text):
                        raise SExprError("unterminated escape in quoted string")
                    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
                    value.append(escapes.get(text[index], text[index]))
                else:
                    value.append(character)
                index += 1
            else:
                raise SExprError("unterminated quoted string")
            tokens.append("".join(value))
        else:
            end = index
            while end < len(text) and not text[end].isspace() and text[end] not in "()":
                end += 1
            tokens.append(text[index:end])
            index = end
    return tokens


def _parse(tokens: list[str]) -> list[SExpr]:
    index = 0

    def parse_list() -> list[SExpr]:
        nonlocal index
        if index >= len(tokens) or tokens[index] != "(":
            raise SExprError("expected opening parenthesis")
        index += 1
        result: list[SExpr] = []
        while index < len(tokens) and tokens[index] != ")":
            if tokens[index] == "(":
                result.append(parse_list())
            else:
                result.append(tokens[index])
                index += 1
        if index >= len(tokens):
            raise SExprError("unterminated list")
        index += 1
        return result

    result = parse_list()
    if index != len(tokens):
        raise SExprError("unexpected tokens after root expression")
    return result


def parse_text(text: str) -> list[SExpr]:
    """Parse one root s-expression and return it as a list."""

    return _parse(_tokens(text))
