from __future__ import annotations

import pytest

from hal.workflow_expressions import (
    BinaryExpression,
    ReferenceExpression,
    WorkflowExpressionError,
    evaluate_workflow_condition,
    evaluate_workflow_expression,
    parse_workflow_expression,
    validate_workflow_template,
)


def test_parses_and_evaluates_boolean_reference_expression() -> None:
    expression = parse_workflow_expression(
        "${{ nodes.tests.status == 'succeeded' && inputs.publish == true }}"
    )
    assert isinstance(expression, BinaryExpression)
    context = {
        "nodes": {"tests": {"status": "succeeded"}},
        "inputs": {"publish": True},
    }
    assert evaluate_workflow_condition(expression, context) is True
    context["inputs"]["publish"] = False
    assert evaluate_workflow_condition(expression, context) is False


def test_boolean_precedence_parentheses_not_and_short_circuit() -> None:
    context = {"inputs": {"enabled": True, "blocked": False}}
    assert evaluate_workflow_condition(
        "not inputs.blocked and (inputs.enabled or nodes.missing.status == 'ok')",
        context,
    ) is True
    assert evaluate_workflow_condition(
        "inputs.blocked and nodes.missing.status == 'ok'", context,
    ) is False


def test_reference_evaluation_uses_mappings_only() -> None:
    expression = parse_workflow_expression("inputs.request")
    assert expression == ReferenceExpression(("inputs", "request"))
    assert evaluate_workflow_expression(expression, {"inputs": {"request": "build"}}) == "build"
    with pytest.raises(WorkflowExpressionError, match="is unavailable"):
        evaluate_workflow_expression(expression, {"inputs": object()})


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "inputs.items[0]",
        "inputs.value + 1",
        "inputs.value < 2",
        "unknown.value == true",
        "inputs.one == inputs.two == inputs.three",
        "${{ inputs.ok }} trailing",
        "",
    ],
)
def test_rejects_code_and_unsupported_expression_syntax(expression: str) -> None:
    with pytest.raises(WorkflowExpressionError):
        parse_workflow_expression(expression)


def test_logical_operators_and_condition_results_require_booleans() -> None:
    with pytest.raises(WorkflowExpressionError, match="and.*boolean"):
        evaluate_workflow_expression("inputs.text and true", {"inputs": {"text": "yes"}})
    with pytest.raises(WorkflowExpressionError, match="condition must evaluate"):
        evaluate_workflow_condition("inputs.text", {"inputs": {"text": "yes"}})


def test_templates_validate_multiple_expressions_and_unmatched_wrappers() -> None:
    expressions = validate_workflow_template(
        "Build ${{ inputs.request }} after ${{ nodes.plan.status == 'succeeded' }}"
    )
    assert len(expressions) == 2
    with pytest.raises(WorkflowExpressionError, match="unmatched"):
        validate_workflow_template("Build ${{ inputs.request")
    with pytest.raises(WorkflowExpressionError, match="unmatched"):
        validate_workflow_template("Build inputs.request }}")


def test_expression_size_and_reference_depth_are_bounded() -> None:
    with pytest.raises(WorkflowExpressionError, match="exceeds 4096"):
        parse_workflow_expression("true" + " and true" * 500)
    deep = "inputs." + ".".join(f"part{index}" for index in range(17))
    with pytest.raises(WorkflowExpressionError, match="exceeds 16 segments"):
        parse_workflow_expression(deep)
