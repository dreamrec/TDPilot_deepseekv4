

def handle_search_nodes(body):
    """Search for nodes by name, type, or family."""
    query = body.get('query', '')
    search_path = body.get('path', '/')
    search_type = body.get('search_type', 'name')  # name, type, family, all
    limit = body.get('limit', 50)

    if not query:
        return {'error': 'Missing required field: query'}

    root = op(search_path)
    if root is None:
        return {'error': f'Search root not found: {search_path}'}

    query_lower = query.lower()
    results = []

    def search_recursive(n):
        if len(results) >= limit:
            return

        match = False
        if search_type in ('name', 'all') and query_lower in n.name.lower():
            match = True
        if search_type in ('type', 'all') and query_lower in n.type.lower():
            match = True
        if search_type in ('family', 'all') and query_lower in n.family.lower():
            match = True

        if match:
            results.append(_serialize_op(n))

        if n.isCOMP:
            for child in n.children:
                search_recursive(child)

    search_recursive(root)

    return {'query': query, 'search_type': search_type, 'count': len(results), 'nodes': results}


def handle_list_families(body):
    """List available operator families and types."""
    path = body.get('path', '/')

    root = op(path)
    if root is None:
        return {'error': f'Node not found: {path}'}

    families = {}

    def collect_types(n):
        fam = n.family
        if fam not in families:
            families[fam] = set()
        families[fam].add(n.type)
        if n.isCOMP:
            for child in n.children:
                collect_types(child)

    collect_types(root)

    return {
        'families': {k: sorted(list(v)) for k, v in sorted(families.items())},
    }


def handle_python_help(body):
    """Get Python help() output for a TD module/class."""
    target = body.get('target', '')
    if not target:
        return {'error': 'Missing required field: target (e.g. "td", "td.OP", "tdu")'}

    # v2.0.1: detect common agent mistakes BEFORE the regex catch-all so
    # the error message is actionable. Agents kept passing operator paths
    # or parameter expressions and bouncing off the generic "Invalid
    # target" error 3+ times per turn.
    target_stripped = target.strip()
    if not target_stripped:
        return {'error': 'Empty target. Pass a class/module name like "td.OP" or "tdu".'}

    # Operator reference — agent confused td_get_node_detail / td_get_params
    # (operator inspection) with td_python_help (class/module docs).
    if target_stripped.startswith('op(') or target_stripped.startswith("op[") or '/project1' in target_stripped:
        return {
            'error': (
                'td_python_help expects a CLASS or MODULE name, not an operator '
                'reference. You passed "{}". Use td_get_node_detail / td_get_params '
                'for live operator inspection. For class help, pass a type name '
                '(e.g. "td.geometryCOMP", "td.TOP", "td.CHOP").'
            ).format(target_stripped[:80]),
            'hint': 'Try td_get_node_detail({"path": "...your-op-path..."}) instead.',
        }

    # Parameter expression — agent passed something like `op('x').par.y`
    if '.par.' in target_stripped or '[' in target_stripped or '(' in target_stripped:
        return {
            'error': (
                'td_python_help expects a CLASS or MODULE name, not an expression. '
                'You passed "{}". For live parameter values use td_get_params. '
                'For parameter-class docs try "td.Par" or "td.ParCollection".'
            ).format(target_stripped[:80]),
            'hint': 'No parens, brackets, or attribute chains beyond the type name.',
        }

    # Security: only allow dotted identifiers — no arbitrary expressions.
    # This catch-all stays as the final guard.
    import re as _re_mod
    if not _re_mod.match(r'^[A-Za-z_][A-Za-z0-9_.]*$', target_stripped):
        return {
            'error': (
                'Invalid target: must be a dotted identifier. Got "{}".'
            ).format(target_stripped[:80]),
            'hint': (
                'Examples: "td", "td.OP", "td.geometryCOMP", "tdu", "tdu.Vector". '
                'No parens, brackets, quotes, or whitespace.'
            ),
        }
    target = target_stripped

    import io
    old_stdout = sys.stdout
    captured = io.StringIO()
    sys.stdout = captured

    try:
        # Resolve via getattr chain instead of eval for safety
        parts = target.split('.')
        obj = eval(parts[0])  # only the root identifier
        for attr in parts[1:]:
            obj = getattr(obj, attr)
        help(obj)
    except Exception as e:
        sys.stdout = old_stdout
        return {'error': f'Help failed for "{target}": {str(e)}'}
    finally:
        sys.stdout = old_stdout

    help_text = captured.getvalue()
    # Truncate if too long
    if len(help_text) > 10000:
        help_text = help_text[:10000] + '\n\n... (truncated, use more specific target)'

    return {'target': target, 'help': help_text}


def handle_python_classes(body):
    """List available TouchDesigner Python classes."""
    try:
        import td
        classes = [name for name in dir(td) if not name.startswith('_')]
        return {'module': 'td', 'classes': classes, 'count': len(classes)}
    except Exception as e:
        return {'error': f'Failed to list classes: {str(e)}'}
