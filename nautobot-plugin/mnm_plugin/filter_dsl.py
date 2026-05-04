"""Expression-mode DSL parser for plugin list views (E6).

Translates a small filter DSL into a single ``django.db.models.Q``
object the filterset applies via ``QuerySet.filter(Q(...))``.

Grammar (recursive descent, hand-rolled — pyparsing rejected to
avoid a new plugin dep and keep the security surface tight):

    expression  := term ("OR" term)*
    term        := factor ("AND" factor)*
    factor      := comparison | "(" expression ")"
    comparison  := identifier op value
                 | identifier "is" "sentinel"
                 | identifier "is" "not" "sentinel"
    op          := "=" | "!=" | "~" | "contains" | ">=" | "<=" | "<" | ">"
                 | "in" | "not" "in"
    value       := quoted_string | number | duration | identifier | list
    list        := "[" value ("," value)* "]"
    duration    := number unit ("ago" | "from now")
    unit        := "seconds" | "minutes" | "hours" | "days" | "weeks"

Security gate (per E0 §8 R6):
- The set of valid identifiers used as field names is the
  allowlist passed by the caller — the model's
  ``FilterSet.Meta.fields``. Any unknown identifier raises
  ``DslError`` at parse time.
- Operators are the fixed list above; no others permitted.
- Values are scalars, durations, allowlisted identifiers, or
  lists of those — no SQL fragments, no Python expressions, no
  template tags, no ``eval()`` / ``exec()`` anywhere.
- The parser translates the entire expression to a single ``Q``
  object. Filter execution is always parameterized by the Django
  ORM. NEVER ``RawSQL``, NEVER ``extra(where=...)``, NEVER
  string-interpolation into the queryset.

Adversarial inputs (covered in ``test_filter_dsl.py``):
- ``field = "1" OR 1=1`` — closing-quote injection; parses as a
  string-equality, no SQL injection vector reaches the database.
- ``mac = "x'; DROP TABLE foo; --"`` — escape-injection; the
  parameterized binding makes the embedded SQL harmless.
- ``field = (SELECT ...)`` — sub-select; rejected at parse time
  because ``(SELECT ...)`` is not a valid value production.
- ``__class__ = "X"`` — Python attribute access; rejected because
  ``__class__`` is not in any model's allowlist.
- Nested boolean: ``field = "x" OR (1=1 AND 2=2)`` — both sides
  parse cleanly, neither has a SQL injection vector.
- Empty / malformed / unknown-field: all return ``DslError``,
  never raise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from django.db.models import Q


@dataclass(frozen=True)
class DslError:
    """Returned from :func:`parse_dsl` on any parse failure.

    The view surfaces ``message`` as a banner via the list-view
    ``extra_context()`` (see ``views.py``).
    """

    message: str


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<STRING_DOUBLE>"(?:\\.|[^"\\])*")
    | (?P<STRING_SINGLE>'(?:\\.|[^'\\])*')
    | (?P<NUMBER>-?\d+)
    | (?P<OP_GE>>=)
    | (?P<OP_LE><=)
    | (?P<OP_NE>!=)
    | (?P<OP_EQ>=)
    | (?P<OP_TILDE>~)
    | (?P<OP_GT>>)
    | (?P<OP_LT><)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<LBRACK>\[)
    | (?P<RBRACK>\])
    | (?P<COMMA>,)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

# Reserved words that are tokenized as their own kinds rather than
# IDENT. Lookup is case-insensitive (operators write `AND` or `and`
# at will).
_KEYWORDS = {"and", "or", "not", "in", "is", "sentinel", "contains"}

_DURATION_UNITS = {
    "second": "seconds",
    "seconds": "seconds",
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
}

_DURATION_RE = re.compile(
    r"^\s*(\d+)\s+([A-Za-z]+)\s+(ago|from\s+now)\s*$",
    re.IGNORECASE,
)

_SENTINEL_REGEX = r"^ifindex:\d+$"


@dataclass
class _Token:
    kind: str
    value: Any
    pos: int


def _unescape_string(s: str) -> str:
    """Single-pass backslash unescape limited to a fixed safe set.

    Avoids ``codecs.decode(..., 'unicode_escape')`` which would
    allow arbitrary ``\\xNN`` / ``\\uNNNN`` sequences and surprise
    operators with non-ASCII expansion.
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            else:
                # \" -> " ; \' -> ' ; \\ -> \ ; anything-else -> literal
                out.append(nxt)
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while pos < len(expression):
        m = _TOKEN_RE.match(expression, pos)
        if not m:
            raise _ParseError(
                f"unexpected character at position {pos}: "
                f"{expression[pos]!r}"
            )
        kind = m.lastgroup
        text = m.group(0)
        if kind == "WS":
            pos = m.end()
            continue
        if kind in ("STRING_DOUBLE", "STRING_SINGLE"):
            tokens.append(
                _Token("STRING", _unescape_string(text[1:-1]), pos)
            )
        elif kind == "NUMBER":
            tokens.append(_Token("NUMBER", int(text), pos))
        elif kind == "IDENT":
            lower = text.lower()
            if lower in _KEYWORDS:
                tokens.append(_Token(lower.upper(), lower, pos))
            else:
                tokens.append(_Token("IDENT", text, pos))
        else:
            tokens.append(_Token(kind, text, pos))
        pos = m.end()
    tokens.append(_Token("EOF", None, pos))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _ParseError(Exception):
    """Internal — caught at the entry point and converted to ``DslError``."""


class _Parser:
    def __init__(self, tokens: list[_Token], allowlist: set[str]):
        self.tokens = tokens
        self.pos = 0
        self.allowlist = allowlist

    def _peek(self) -> _Token:
        return self.tokens[self.pos]

    def _eat(self, kind: str) -> _Token:
        tok = self._peek()
        if tok.kind != kind:
            raise _ParseError(
                f"expected {kind} at position {tok.pos}, got "
                f"{tok.kind} ({tok.value!r})"
            )
        self.pos += 1
        return tok

    def parse(self) -> Q:
        result = self._parse_or()
        if self._peek().kind != "EOF":
            tok = self._peek()
            raise _ParseError(
                f"unexpected token {tok.value!r} at position {tok.pos}"
            )
        return result

    def _parse_or(self) -> Q:
        left = self._parse_and()
        while self._peek().kind == "OR":
            self._eat("OR")
            right = self._parse_and()
            left = left | right
        return left

    def _parse_and(self) -> Q:
        left = self._parse_factor()
        while self._peek().kind == "AND":
            self._eat("AND")
            right = self._parse_factor()
            left = left & right
        return left

    def _parse_factor(self) -> Q:
        if self._peek().kind == "LPAREN":
            self._eat("LPAREN")
            inner = self._parse_or()
            self._eat("RPAREN")
            return inner
        return self._parse_comparison()

    def _parse_comparison(self) -> Q:
        ident_tok = self._eat("IDENT")
        field = ident_tok.value
        if field not in self.allowlist:
            raise _ParseError(
                f"Field {field!r} is not filterable on this view"
            )

        nxt = self._peek().kind

        # 'is sentinel' / 'is not sentinel'
        if nxt == "IS":
            self._eat("IS")
            negate = False
            if self._peek().kind == "NOT":
                self._eat("NOT")
                negate = True
            self._eat("SENTINEL")
            sentinel_q = Q(**{f"{field}__regex": _SENTINEL_REGEX})
            return ~sentinel_q if negate else sentinel_q

        # 'not in [...]'
        if nxt == "NOT":
            self._eat("NOT")
            self._eat("IN")
            values = self._parse_list()
            return ~Q(**{f"{field}__in": values})

        # 'in [...]'
        if nxt == "IN":
            self._eat("IN")
            values = self._parse_list()
            return Q(**{f"{field}__in": values})

        # 'contains'
        if nxt == "CONTAINS":
            self._eat("CONTAINS")
            value = self._parse_value()
            return Q(**{f"{field}__icontains": _coerce_to_text(value)})

        # Symbolic operators
        op_kinds = {
            "OP_EQ", "OP_NE", "OP_TILDE",
            "OP_GE", "OP_LE", "OP_GT", "OP_LT",
        }
        if nxt in op_kinds:
            op_tok = self._peek()
            self.pos += 1
            value = self._parse_value()
            return _build_comparison(field, op_tok.kind, value)

        raise _ParseError(
            f"expected operator after {field!r} at position "
            f"{self._peek().pos}; incomplete comparison"
        )

    def _parse_list(self) -> list:
        self._eat("LBRACK")
        items: list = []
        if self._peek().kind != "RBRACK":
            items.append(self._parse_value())
            while self._peek().kind == "COMMA":
                self._eat("COMMA")
                items.append(self._parse_value())
        self._eat("RBRACK")
        return items

    def _parse_value(self):
        tok = self._peek()
        if tok.kind == "STRING":
            self.pos += 1
            # Quoted strings may also be durations like "7 days ago".
            duration = _try_duration(tok.value)
            if duration is not None:
                return duration
            return tok.value

        if tok.kind == "NUMBER":
            number = tok.value
            self.pos += 1
            # Look ahead for an unquoted duration: NUMBER UNIT (ago|from now)
            ahead = self._peek()
            if ahead.kind == "IDENT" and ahead.value.lower() in _DURATION_UNITS:
                unit_key = _DURATION_UNITS[ahead.value.lower()]
                self._eat("IDENT")
                tail = self._peek()
                if tail.kind == "IDENT" and tail.value.lower() == "ago":
                    self._eat("IDENT")
                    return _now() - timedelta(**{unit_key: number})
                if tail.kind == "IDENT" and tail.value.lower() == "from":
                    self._eat("IDENT")
                    nxt = self._peek()
                    if nxt.kind == "IDENT" and nxt.value.lower() == "now":
                        self._eat("IDENT")
                        return _now() + timedelta(**{unit_key: number})
                    raise _ParseError(
                        f"expected 'now' after 'from' at position {nxt.pos}"
                    )
                raise _ParseError(
                    f"duration must end with 'ago' or 'from now' at "
                    f"position {tail.pos}"
                )
            return number

        if tok.kind == "IDENT":
            self.pos += 1
            lower = tok.value.lower()
            if lower == "true":
                return True
            if lower == "false":
                return False
            if lower == "null":
                return None
            # Bare identifier-as-value is treated as a literal string;
            # operators write `vendor = juniper` without quotes.
            return tok.value

        raise _ParseError(
            f"expected value at position {tok.pos}, got {tok.kind}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_duration(s: str):
    """Parse a duration embedded in a quoted string.

    ``"7 days ago"`` -> datetime now - 7 days
    ``"5 minutes from now"`` -> datetime now + 5 minutes
    Anything else -> None.
    """
    m = _DURATION_RE.match(s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    direction = m.group(3).lower().replace(" ", "").replace("\t", "")
    if unit not in _DURATION_UNITS:
        return None
    delta = timedelta(**{_DURATION_UNITS[unit]: n})
    return _now() - delta if direction == "ago" else _now() + delta


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_to_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _build_comparison(field: str, op_kind: str, value) -> Q:
    if op_kind == "OP_EQ":
        if isinstance(value, str):
            return Q(**{f"{field}__iexact": value})
        return Q(**{field: value})
    if op_kind == "OP_NE":
        if isinstance(value, str):
            return ~Q(**{f"{field}__iexact": value})
        return ~Q(**{field: value})
    if op_kind == "OP_TILDE":
        return Q(**{f"{field}__iregex": _coerce_to_text(value)})
    if op_kind == "OP_GE":
        return Q(**{f"{field}__gte": value})
    if op_kind == "OP_LE":
        return Q(**{f"{field}__lte": value})
    if op_kind == "OP_GT":
        return Q(**{f"{field}__gt": value})
    if op_kind == "OP_LT":
        return Q(**{f"{field}__lt": value})
    raise _ParseError(f"internal: unknown operator {op_kind}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_dsl(expression: str, allowlist_fields: Iterable[str]):
    """Parse DSL expression into a Django ``Q`` object.

    Args:
        expression: the raw DSL string from ``?q=...`` (URL-decoded).
        allowlist_fields: model field names valid as identifiers
            (typically ``FilterSet.Meta.fields``).

    Returns:
        ``Q`` on success, or ``DslError(message)`` on parse failure.
        Never raises — all errors caught and returned as ``DslError``.

    Empty / whitespace-only expressions return ``Q()`` (identity —
    matches everything; effectively a no-op filter).
    """
    if not expression or not expression.strip():
        return Q()
    allowlist = (
        allowlist_fields
        if isinstance(allowlist_fields, set)
        else set(allowlist_fields)
    )
    try:
        tokens = _tokenize(expression)
        return _Parser(tokens, allowlist).parse()
    except _ParseError as exc:
        return DslError(str(exc))
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        return DslError(f"unexpected parse error: {exc}")
