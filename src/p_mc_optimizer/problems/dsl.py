"""Parse and interpret the bounded mathematical expression language."""

from __future__ import annotations

import ast
import io
import math
import operator
import tokenize
from dataclasses import dataclass

from p_mc_optimizer.domain.models import DomainError, finite_real


CONSTANTS = {"pi": math.pi, "e": math.e}
UNARY_OPERATORS = {"UAdd": operator.pos, "USub": operator.neg}
BINARY_OPERATORS = {
    "Add": operator.add, "Sub": operator.sub, "Mult": operator.mul,
    "Div": operator.truediv, "Pow": math.pow,
}
FUNCTIONS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "exp": math.exp,
    "log": math.log, "sqrt": math.sqrt, "abs": abs, "min": min, "max": max,
}
RESERVED_NAMES = frozenset(CONSTANTS | FUNCTIONS)


def invalid_expression(message: str) -> DomainError:
    return DomainError("INVALID_EXPRESSION", message)


def _check_tree_limits(tree: ast.AST) -> None:
    pending = [(tree, 1)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 512 or depth > 32:
            raise invalid_expression("حداکثر عمق و تعداد گره عبارت به‌ترتیب ۳۲ و ۵۱۲ است.")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


def _constant(node: ast.AST) -> float:
    if not isinstance(node, ast.Constant):
        raise invalid_expression("نمای توان باید عدد ثابت با قدرمطلق حداکثر ۱۶ باشد.")
    try:
        return finite_real(node.value) + 0.0
    except ValueError:
        raise invalid_expression("فقط عدد حقیقی متناهی در عبارت مجاز است.") from None


def _exponent(node: ast.AST) -> float:
    if isinstance(node, ast.UnaryOp) and type(node.op).__name__ in UNARY_OPERATORS:
        value = UNARY_OPERATORS[type(node.op).__name__](_constant(node.operand))
    else:
        value = _constant(node)
    if abs(value) > 16:
        raise invalid_expression("قدر مطلق نمای توان باید حداکثر ۱۶ باشد.")
    return value + 0.0


def _canonical(node: ast.AST, symbols: set[str] | frozenset[str]) -> tuple:
    if isinstance(node, ast.Constant):
        return ("literal", _constant(node))
    if isinstance(node, ast.Name):
        if node.id not in symbols and node.id not in CONSTANTS:
            raise DomainError("UNKNOWN_VARIABLE", "عبارت حاوی نام متغیر یا پارامتر ناشناخته است.")
        return ("name", node.id)
    if isinstance(node, ast.UnaryOp) and type(node.op).__name__ in UNARY_OPERATORS:
        operand = _canonical(node.operand, symbols)
        if operand == ("literal", 0.0):
            return operand
        return ("unary", type(node.op).__name__, operand)
    if isinstance(node, ast.BinOp) and type(node.op).__name__ in BINARY_OPERATORS:
        tag = type(node.op).__name__
        right = ("literal", _exponent(node.right)) if tag == "Pow" else _canonical(node.right, symbols)
        return ("binary", tag, _canonical(node.left, symbols), right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS:
        function = node.func.id
        arity_ok = 2 <= len(node.args) <= 32 if function in {"min", "max"} else len(node.args) == 1
        if node.keywords or not arity_ok:
            raise invalid_expression("تعداد یا نوع آرگومان تابع با گرامر سازگار نیست.")
        return ("call", function, tuple(_canonical(argument, symbols) for argument in node.args))
    raise invalid_expression("ساختار عبارت نامعتبر است. برای توان ** و برای ضرب * را به‌صراحت بنویسید.")


def _evaluate(node: tuple, symbols: dict[str, float]) -> float:
    kind, identifier, *arguments = node
    if kind == "literal":
        return identifier
    if kind == "name":
        return symbols[identifier]
    if kind == "unary":
        result = UNARY_OPERATORS[identifier](_evaluate(arguments[0], symbols))
    elif kind == "binary":
        result = BINARY_OPERATORS[identifier](_evaluate(arguments[0], symbols), _evaluate(arguments[1], symbols))
    else:
        result = FUNCTIONS[identifier](*(_evaluate(child, symbols) for child in arguments[0]))
    if not math.isfinite(result):
        raise ArithmeticError("Non-finite intermediate result")
    return result


@dataclass(frozen=True)
class MathExpression:
    canonical: tuple

    def evaluate(self, values: dict[str, float]) -> float:
        return _evaluate(self.canonical, CONSTANTS | values)


def parse_expression(text: str, symbols: set[str] | frozenset[str]) -> MathExpression:
    if not text.strip() or len(text) > 4096:
        raise invalid_expression("عبارت باید میان ۱ تا ۴۰۹۶ نویسه باشد.")
    wrapped = "(\n" + text.strip() + "\n)"
    try:
        for token in tokenize.generate_tokens(io.StringIO(wrapped).readline):
            if token.type == tokenize.NAME and not token.string.isascii():
                raise invalid_expression("نام‌های عبارت باید ASCII باشند.")
            if token.type == tokenize.COMMENT:
                raise invalid_expression("عبارت نباید توضیح یا comment داشته باشد.")
        tree = ast.parse(wrapped, mode="eval")
    except (SyntaxError, tokenize.TokenError, RecursionError, UnicodeError):
        raise invalid_expression("ساختار عبارت نامعتبر است. ضرب را با * و توان را با ** بنویسید.") from None
    _check_tree_limits(tree)
    return MathExpression(_canonical(tree.body, symbols))
