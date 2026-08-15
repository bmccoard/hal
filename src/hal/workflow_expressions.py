"""A small side-effect-free expression language for workflow definitions."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import re
from typing import Any, Mapping


_MAX_EXPRESSION_CHARS = 4_096
_MAX_TOKENS = 256
_MAX_REFERENCE_SEGMENTS = 16
_REFERENCE_ROOTS = frozenset({"inputs", "nodes", "node", "attempt", "workflow"})
_TEMPLATE_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_TOKEN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<equal>==)"
    r"|(?P<not_equal>!=)"
    r"|(?P<and_symbol>&&)"
    r"|(?P<or_symbol>\|\|)"
    r"|(?P<not_symbol>!)"
    r"|(?P<left>\()"
    r"|(?P<right>\))"
    r"|(?P<dot>\.)"
    r"|(?P<string>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_-]*)"
)


class WorkflowExpressionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LiteralExpression:
    value: Any


@dataclass(frozen=True, slots=True)
class ReferenceExpression:
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnaryExpression:
    operator: str
    operand: Expression


@dataclass(frozen=True, slots=True)
class BinaryExpression:
    operator: str
    left: Expression
    right: Expression


Expression = LiteralExpression | ReferenceExpression | UnaryExpression | BinaryExpression


@dataclass(frozen=True, slots=True)
class _TokenValue:
    kind: str
    value: Any
    offset: int


def parse_workflow_expression(text: str) -> Expression:
    """Parse one condition/reference expression without evaluating host code."""
    if not isinstance(text, str):
        raise WorkflowExpressionError("workflow expression must be a string")
    value = text.strip()
    if value.startswith("${{") and value.endswith("}}"):
        value = value[3:-2].strip()
    elif "${{" in value or "}}" in value:
        raise WorkflowExpressionError("workflow expression wrapper must contain the entire value")
    if not value:
        raise WorkflowExpressionError("workflow expression must not be empty")
    if len(value) > _MAX_EXPRESSION_CHARS:
        raise WorkflowExpressionError(
            f"workflow expression exceeds {_MAX_EXPRESSION_CHARS} characters"
        )
    return _Parser(_tokenize(value)).parse()


def validate_workflow_template(text: str) -> tuple[Expression, ...]:
    """Validate every expression embedded in a string template."""
    if not isinstance(text, str):
        raise WorkflowExpressionError("workflow template must be a string")
    expressions = tuple(
        parse_workflow_expression(match.group(1))
        for match in _TEMPLATE_EXPRESSION.finditer(text)
    )
    remainder = _TEMPLATE_EXPRESSION.sub("", text)
    if "${{" in remainder or "}}" in remainder:
        raise WorkflowExpressionError("workflow template contains an unmatched expression wrapper")
    return expressions


def evaluate_workflow_expression(expression: Expression | str, context: Mapping[str, Any]) -> Any:
    """Evaluate an expression using mapping lookup only—never attribute access."""
    parsed = parse_workflow_expression(expression) if isinstance(expression, str) else expression
    if isinstance(parsed, LiteralExpression):
        return parsed.value
    if isinstance(parsed, ReferenceExpression):
        value: Any = context
        for segment in parsed.path:
            if not isinstance(value, Mapping) or segment not in value:
                raise WorkflowExpressionError(
                    f"workflow reference {'.'.join(parsed.path)!r} is unavailable"
                )
            value = value[segment]
        return value
    if isinstance(parsed, UnaryExpression):
        value = evaluate_workflow_expression(parsed.operand, context)
        if not isinstance(value, bool):
            raise WorkflowExpressionError("operator 'not' requires a boolean operand")
        return not value
    left = evaluate_workflow_expression(parsed.left, context)
    if parsed.operator == "and":
        if not isinstance(left, bool):
            raise WorkflowExpressionError("operator 'and' requires boolean operands")
        if not left:
            return False
        right = evaluate_workflow_expression(parsed.right, context)
        if not isinstance(right, bool):
            raise WorkflowExpressionError("operator 'and' requires boolean operands")
        return right
    if parsed.operator == "or":
        if not isinstance(left, bool):
            raise WorkflowExpressionError("operator 'or' requires boolean operands")
        if left:
            return True
        right = evaluate_workflow_expression(parsed.right, context)
        if not isinstance(right, bool):
            raise WorkflowExpressionError("operator 'or' requires boolean operands")
        return right
    right = evaluate_workflow_expression(parsed.right, context)
    if parsed.operator == "==":
        return left == right
    if parsed.operator == "!=":
        return left != right
    raise WorkflowExpressionError(f"unsupported workflow operator {parsed.operator!r}")


def evaluate_workflow_condition(expression: Expression | str, context: Mapping[str, Any]) -> bool:
    result = evaluate_workflow_expression(expression, context)
    if not isinstance(result, bool):
        raise WorkflowExpressionError("workflow condition must evaluate to true or false")
    return result


def render_workflow_template(text: str, context: Mapping[str, Any]) -> Any:
    """Resolve a direct expression to its native type or interpolate a string template."""
    expressions = validate_workflow_template(text)
    matches = tuple(_TEMPLATE_EXPRESSION.finditer(text))
    if len(matches) == 1 and matches[0].span() == (0, len(text)):
        return evaluate_workflow_expression(expressions[0], context)
    pieces: list[str] = []
    offset = 0
    for match, expression in zip(matches, expressions):
        pieces.append(text[offset:match.start()])
        value = evaluate_workflow_expression(expression, context)
        if isinstance(value, str):
            pieces.append(value)
        elif value is None:
            pieces.append("null")
        elif callable(getattr(value, "summary", None)):
            pieces.append(json.dumps(value.summary(), sort_keys=True, separators=(",", ":")))
        else:
            pieces.append(json.dumps(value, sort_keys=True, separators=(",", ":")))
        offset = match.end()
    pieces.append(text[offset:])
    return "".join(pieces)


def workflow_expression_references(
    expression: Expression | str,
) -> tuple[ReferenceExpression, ...]:
    """Return references in stable source-tree order for static validation."""
    parsed = parse_workflow_expression(expression) if isinstance(expression, str) else expression
    if isinstance(parsed, ReferenceExpression):
        return (parsed,)
    if isinstance(parsed, LiteralExpression):
        return ()
    if isinstance(parsed, UnaryExpression):
        return workflow_expression_references(parsed.operand)
    return (
        *workflow_expression_references(parsed.left),
        *workflow_expression_references(parsed.right),
    )


def _tokenize(text: str) -> tuple[_TokenValue, ...]:
    tokens: list[_TokenValue] = []
    offset = 0
    while offset < len(text):
        match = _TOKEN.match(text, offset)
        if match is None:
            raise WorkflowExpressionError(
                f"unexpected character at expression offset {offset}: {text[offset]!r}"
            )
        kind = match.lastgroup or ""
        raw = match.group()
        offset = match.end()
        if kind == "space":
            continue
        if kind == "identifier":
            lowered = raw.lower()
            if lowered in {"and", "or", "not"}:
                kind, raw = lowered, lowered
            elif lowered == "true":
                kind, raw = "literal", True
            elif lowered == "false":
                kind, raw = "literal", False
            elif lowered == "null":
                kind, raw = "literal", None
        elif kind == "string":
            try:
                raw = ast.literal_eval(raw)
            except (SyntaxError, ValueError) as exc:
                raise WorkflowExpressionError("invalid workflow string literal") from exc
        elif kind == "number":
            raw = float(raw) if "." in raw else int(raw)
        kind = {
            "equal": "==", "not_equal": "!=", "and_symbol": "and",
            "or_symbol": "or", "not_symbol": "not", "left": "(",
            "right": ")", "dot": ".", "string": "literal", "number": "literal",
        }.get(kind, kind)
        tokens.append(_TokenValue(kind, raw, match.start()))
        if len(tokens) > _MAX_TOKENS:
            raise WorkflowExpressionError(
                f"workflow expression exceeds {_MAX_TOKENS} tokens"
            )
    tokens.append(_TokenValue("end", None, len(text)))
    return tuple(tokens)


class _Parser:
    def __init__(self, tokens: tuple[_TokenValue, ...]) -> None:
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> _TokenValue:
        return self.tokens[self.index]

    def take(self, kind: str) -> _TokenValue | None:
        if self.current.kind != kind:
            return None
        token = self.current
        self.index += 1
        return token

    def require(self, kind: str) -> _TokenValue:
        token = self.take(kind)
        if token is None:
            raise WorkflowExpressionError(
                f"expected {kind!r} at expression offset {self.current.offset}"
            )
        return token

    def parse(self) -> Expression:
        expression = self.parse_or()
        if self.current.kind != "end":
            raise WorkflowExpressionError(
                f"unexpected token {self.current.value!r} at expression offset {self.current.offset}"
            )
        return expression

    def parse_or(self) -> Expression:
        expression = self.parse_and()
        while self.take("or"):
            expression = BinaryExpression("or", expression, self.parse_and())
        return expression

    def parse_and(self) -> Expression:
        expression = self.parse_not()
        while self.take("and"):
            expression = BinaryExpression("and", expression, self.parse_not())
        return expression

    def parse_not(self) -> Expression:
        if self.take("not"):
            return UnaryExpression("not", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Expression:
        expression = self.parse_primary()
        if self.current.kind in {"==", "!="}:
            operator = self.current.kind
            self.index += 1
            expression = BinaryExpression(operator, expression, self.parse_primary())
            if self.current.kind in {"==", "!="}:
                raise WorkflowExpressionError("chained workflow comparisons are not supported")
        return expression

    def parse_primary(self) -> Expression:
        literal = self.take("literal")
        if literal is not None:
            return LiteralExpression(literal.value)
        if self.take("("):
            expression = self.parse_or()
            self.require(")")
            return expression
        identifier = self.take("identifier")
        if identifier is None:
            raise WorkflowExpressionError(
                f"expected a literal, reference, or '(' at expression offset {self.current.offset}"
            )
        path = [str(identifier.value)]
        while self.take("."):
            path.append(str(self.require("identifier").value))
        if path[0] not in _REFERENCE_ROOTS:
            roots = ", ".join(sorted(_REFERENCE_ROOTS))
            raise WorkflowExpressionError(
                f"workflow reference root {path[0]!r} is not allowed (available: {roots})"
            )
        if len(path) > _MAX_REFERENCE_SEGMENTS:
            raise WorkflowExpressionError(
                f"workflow reference exceeds {_MAX_REFERENCE_SEGMENTS} segments"
            )
        return ReferenceExpression(tuple(path))
