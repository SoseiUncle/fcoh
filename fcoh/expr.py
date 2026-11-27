"""Safe AST arithmetic expression evaluator and CAD expression transpiler.

Never calls Python eval(). Only allows +, -, *, /, unary +/-, finite numbers,
defined parameter names, and whitelisted functions (min, max, abs).
"""
import ast
import math

ALLOWED_FUNCS = {'min': min, 'max': max, 'abs': abs}
MAX_EXPR_LEN = 128
MAX_AST_NODES = 64


class ExprError(ValueError):
    pass


def parse_expr(text):
    if not isinstance(text, str) or not text.strip():
        raise ExprError('Expression must be a non-empty string')
    if len(text) > MAX_EXPR_LEN:
        raise ExprError(f'Expression exceeds {MAX_EXPR_LEN} characters')
    try:
        tree = ast.parse(text.strip(), mode='eval')
    except SyntaxError as error:
        raise ExprError(f'Invalid expression syntax: {text!r}') from error
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise ExprError(f'Expression is too complex (>{MAX_AST_NODES} AST nodes)')
    for node in nodes:
        if isinstance(node, (ast.Expression, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)):
            continue
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                raise ExprError(f'Unsupported operator in expression: {text!r}')
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.UAdd, ast.USub)):
                raise ExprError(f'Unsupported unary operator in expression: {text!r}')
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ExprError(f'Only numeric literals are allowed: {text!r}')
            if not math.isfinite(node.value):
                raise ExprError(f'Non-finite literal in expression: {text!r}')
        elif isinstance(node, ast.Name):
            continue
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCS:
                raise ExprError(f'Unsupported function call in expression: {text!r}')
            if node.keywords:
                raise ExprError(f'Keyword arguments are not supported: {text!r}')
            if node.func.id == 'abs' and len(node.args) != 1:
                raise ExprError('abs() takes exactly 1 argument')
            if node.func.id in ('min', 'max') and len(node.args) < 2:
                raise ExprError(f'{node.func.id}() requires at least 2 arguments')
        else:
            raise ExprError(f'Unsupported syntax ({type(node).__name__}) in expression: {text!r}')
    return tree


def extract_refs(text):
    tree = parse_expr(text)
    refs = []
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FUNCS:
            if node.id not in seen:
                seen.add(node.id)
                refs.append(node.id)
    return refs


def _eval_node(node, env):
    """Evaluate AST node returning (numeric_value, dim) where dim in {'L', '1'}.

    Parameter names have dimension 'L' (mm). Numeric literals start dimensionless ('1')
    and promote cleanly to 'L' when added/subtracted/compared with 'L'.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value), '1'
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise ExprError(f'Unknown parameter: {node.id}')
        return float(env[node.id]), 'L'
    if isinstance(node, ast.UnaryOp):
        val, dim = _eval_node(node.operand, env)
        return (-val if isinstance(node.op, ast.USub) else +val), dim
    if isinstance(node, ast.BinOp):
        lv, ld = _eval_node(node.left, env)
        rv, rd = _eval_node(node.right, env)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            dim = 'L' if ('L' in (ld, rd)) else '1'
            res = lv + rv if isinstance(node.op, ast.Add) else lv - rv
            return res, dim
        if isinstance(node.op, ast.Mult):
            if ld == 'L' and rd == 'L':
                raise ExprError('Multiplying two lengths produces area, not a linear mm dimension')
            return lv * rv, ('L' if 'L' in (ld, rd) else '1')
        if isinstance(node.op, ast.Div):
            if abs(rv) < 1e-12:
                raise ExprError('Division by zero in expression')
            if ld == '1' and rd == 'L':
                raise ExprError('Dividing a number by a length produces inverse mm, not a linear dimension')
            dim = 'L' if (ld == 'L' and rd == '1') else '1'
            return lv / rv, dim
    if isinstance(node, ast.Call):
        fn = node.func.id
        evaluated = [_eval_node(a, env) for a in node.args]
        vals = [v for v, _ in evaluated]
        dims = [d for _, d in evaluated]
        dim = 'L' if 'L' in dims else '1'
        return float(ALLOWED_FUNCS[fn](*vals)), dim
    raise ExprError(f'Cannot evaluate AST node: {type(node).__name__}')


def _fmt_num(val, with_mm=False):
    text = f'{val:g}' if isinstance(val, float) else str(val)
    return f'{text} mm' if with_mm else text


def _transpile_node(node, env, param_prefix, arg_sep, want_mm=False):
    """Transpile AST into a CAD expression with unit-consistent mm literals."""
    _, own_dim = _eval_node(node, env)
    if isinstance(node, ast.Expression):
        inner = _transpile_node(node.body, env, param_prefix, arg_sep, want_mm=want_mm)
        return inner
    if isinstance(node, ast.Constant):
        return _fmt_num(node.value, with_mm=want_mm)
    if isinstance(node, ast.Name):
        return f'{param_prefix}{node.id}'
    if isinstance(node, ast.UnaryOp):
        op = '-' if isinstance(node.op, ast.USub) else '+'
        sub = _transpile_node(node.operand, env, param_prefix, arg_sep, want_mm=want_mm)
        if isinstance(node.operand, ast.BinOp):
            sub = f'({sub})'
        return f'{op}{sub}'
    if isinstance(node, ast.BinOp):
        _, ld = _eval_node(node.left, env)
        _, rd = _eval_node(node.right, env)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            op = '+' if isinstance(node.op, ast.Add) else '-'
            target_mm = want_mm or ('L' in (ld, rd))
            ls = _transpile_node(node.left, env, param_prefix, arg_sep, want_mm=target_mm)
            rs = _transpile_node(node.right, env, param_prefix, arg_sep, want_mm=target_mm)
            if isinstance(node.right, ast.BinOp) and isinstance(node.right.op, (ast.Add, ast.Sub)):
                rs = f'({rs})'
            return f'{ls} {op} {rs}'
        if isinstance(node.op, ast.Mult):
            # Attach mm to whichever side is dimensionless only if final needs mm and neither is L.
            l_mm = want_mm and ld == '1' and rd == '1'
            ls = _transpile_node(node.left, env, param_prefix, arg_sep, want_mm=l_mm)
            rs = _transpile_node(node.right, env, param_prefix, arg_sep, want_mm=False)
            if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, (ast.Add, ast.Sub)):
                ls = f'({ls})'
            if isinstance(node.right, ast.BinOp) and isinstance(node.right.op, (ast.Add, ast.Sub)):
                rs = f'({rs})'
            return f'{ls} * {rs}'
        if isinstance(node.op, ast.Div):
            l_mm = want_mm and ld == '1' and rd == '1'
            ls = _transpile_node(node.left, env, param_prefix, arg_sep, want_mm=l_mm)
            rs = _transpile_node(node.right, env, param_prefix, arg_sep, want_mm=False)
            if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, (ast.Add, ast.Sub)):
                ls = f'({ls})'
            if isinstance(node.right, ast.BinOp):
                rs = f'({rs})'
            return f'{ls} / {rs}'
    if isinstance(node, ast.Call):
        fn = node.func.id
        dims = [_eval_node(a, env)[1] for a in node.args]
        target_mm = want_mm or ('L' in dims)
        args = [_transpile_node(a, env, param_prefix, arg_sep, want_mm=target_mm) for a in node.args]
        return f'{fn}({arg_sep.join(args)})'
    raise ExprError(f'Cannot transpile AST node: {type(node).__name__}')


def evaluate_dimension(raw, env, positive=False):
    """Evaluate a number or expression string, returning (float_value, metadata_or_None)."""
    if isinstance(raw, bool):
        raise ExprError('Dimensions must be finite numbers or expressions')
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw):
            raise ExprError('Dimensions must be finite numbers')
        val = float(raw)
        if positive and val <= 0:
            raise ExprError('Lengths/radii must be greater than zero')
        return val, None
    if isinstance(raw, str):
        tree = parse_expr(raw)
        val, _ = _eval_node(tree, env)
        if not math.isfinite(val):
            raise ExprError('Expression evaluated to a non-finite number')
        if abs(val) > 10000:
            raise ExprError(f'Evaluated dimension {val} mm exceeds limit (-10000..10000)')
        if positive and val <= 0:
            raise ExprError(f'Lengths/radii must be greater than zero (got {val} from {raw!r})')
        refs = extract_refs(raw)
        meta = {
            'raw': raw.strip(),
            'refs': refs,
            'freecad_sheet': '=' + _transpile_node(tree, env, '', '; ', want_mm=True),
            'freecad_expr': _transpile_node(tree, env, 'Params.', '; ', want_mm=True),
            'fusion_expr': _transpile_node(tree, env, '', '; ', want_mm=True),
        }
        return float(val), meta
    raise ExprError('Dimensions must be finite numbers or expressions')


def resolve_params(params):
    """Resolve parameter dependency DAG in topological order.

    Returns (order, resolved_values, expr_meta_by_name).
    """
    deps = {}
    for name, spec in params.items():
        raw = spec['value']
        if isinstance(raw, str):
            refs = extract_refs(raw)
            for ref in refs:
                if ref not in params:
                    raise ExprError(f'Parameter {name!r} references unknown parameter {ref!r}')
            deps[name] = refs
        else:
            deps[name] = []

    order = []
    state = {}  # 0=unvisited, 1=visiting, 2=visited

    def visit(name, stack):
        st = state.get(name, 0)
        if st == 2:
            return
        if st == 1:
            cycle = ' -> '.join(stack[stack.index(name):] + [name])
            raise ExprError(f'Circular parameter reference: {cycle}')
        state[name] = 1
        for dep in deps[name]:
            visit(dep, stack + [name])
        state[name] = 2
        order.append(name)

    for name in params:
        visit(name, [])

    env = {}
    metas = {}
    for name in order:
        val, meta = evaluate_dimension(params[name]['value'], env, positive=False)
        env[name] = val
        metas[name] = meta
    return order, env, metas
