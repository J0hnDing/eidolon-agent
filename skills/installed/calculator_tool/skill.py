import ast
import json
import operator
import sys


OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def evaluate_expression(expression):
    tree = ast.parse(expression, mode="eval")
    return evaluate_node(tree.body)


def evaluate_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        left = evaluate_node(node.left)
        right = evaluate_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("Exponent is too large.")
        return OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY_OPERATORS:
        return UNARY_OPERATORS[type(node.op)](evaluate_node(node.operand))
    raise ValueError("Only numbers, parentheses, and arithmetic operators are allowed.")


def run(payload):
    expression = payload.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        return {"error": "Input must include a non-empty expression string."}
    try:
        result = evaluate_expression(expression)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
        return {"error": str(exc)}
    return {"result": result}


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            response = {"error": "Input must be a JSON object."}
        else:
            response = run(payload)
    except json.JSONDecodeError as exc:
        response = {"error": f"Invalid JSON input: {exc.msg}"}
    print(json.dumps(response))


if __name__ == "__main__":
    main()
