
def handle_health(body):
    """Health check endpoint."""
    return {
        'status': 'ok',
        'api_version': API_VERSION,
        'timestamp': time.time(),
    }


def handle_info(body):
    """Get TouchDesigner environment info."""
    return {
        'version': app.version,
        'build': app.build,
        'osName': app.osName,
        'osVersion': app.osVersion,
        'product': app.product,
        'project_name': project.name,
        'project_folder': project.folder,
        'fps': project.cookRate,
        'realTime': project.realTime,
        'frame': absTime.frame,
        'seconds': absTime.seconds,
        'timeline_start': project.cookRange[0] if hasattr(project, 'cookRange') else 1,
        'timeline_end': project.cookRange[1] if hasattr(project, 'cookRange') else 600,
        'api_version': API_VERSION,
    }


def handle_get_nodes(body):
    """List children of a path, with optional filtering."""
    path = body.get('path', '/')
    family_filter = body.get('family', None)
    type_filter = body.get('type', None)
    depth = body.get('depth', 1)
    include_params = body.get('include_params', False)
    limit = body.get('limit', 100)
    offset = body.get('offset', 0)

    target = op(path)
    if target is None:
        return {'error': f'Node not found: {path}'}

    if not target.isCOMP:
        return {'error': f'Node is not a COMP (cannot have children): {path}', 'node_type': target.type}

    children = target.children

    # Apply filters
    if family_filter:
        family_filter = family_filter.upper()
        children = [c for c in children if c.family == family_filter]
    if type_filter:
        children = [c for c in children if c.type == type_filter]

    total = len(children)
    children = children[offset:offset + limit]

    nodes = [_serialize_op(c, include_params=include_params) for c in children]

    return {
        'path': path,
        'total': total,
        'count': len(nodes),
        'offset': offset,
        'has_more': total > offset + len(nodes),
        'nodes': nodes,
    }


def handle_get_node_detail(body):
    """Get detailed info about a single node.

    Caps parameter serialization at NODE_DETAIL_PARAM_LIMIT entries (default 50)
    so a single COMP with hundreds of internal params can't blow the model
    context. Callers needing full params should use td_get_params with paging.
    """
    path = body.get('path')
    if not path:
        return {'error': 'Missing required field: path'}

    # Allow callers to override the cap via the request body, otherwise use
    # the default. Hard ceiling at 200 to keep responses bounded.
    try:
        param_limit = int(body.get('param_limit', 50))
    except (TypeError, ValueError):
        param_limit = 50
    param_limit = max(1, min(200, param_limit))

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    detail = _serialize_op(node, include_params=False)

    # Manual capped param serialization. We deliberately don't reuse
    # _serialize_op(include_params=True) because that takes ALL params and
    # the v1.5.2 audit found a single COMP yielding ~88 KB of JSON, which
    # exceeded the model context budget on the client side.
    full_params = _serialize_params(node)
    if len(full_params) > param_limit:
        names = list(full_params.keys())[:param_limit]
        detail['parameters'] = {n: full_params[n] for n in names}
        detail['parameters_truncated'] = True
        detail['parameters_total'] = len(full_params)
        detail['parameters_returned'] = param_limit
        detail['parameters_hint'] = (
            f'Only first {param_limit}/{len(full_params)} parameters serialized to '
            'cap response size. Use td_get_params with name or page filters '
            'for the rest.'
        )
    else:
        detail['parameters'] = full_params
        detail['parameters_truncated'] = False
        detail['parameters_total'] = len(full_params)
        detail['parameters_returned'] = len(full_params)

    # Add connection info
    detail['inputs'] = []
    for conn in node.inputConnectors:
        for c in conn.connections:
            detail['inputs'].append({
                'from': c.owner.path,
                'from_index': c.index,
                'to_index': conn.index,
            })

    detail['outputs'] = []
    for conn in node.outputConnectors:
        for c in conn.connections:
            detail['outputs'].append({
                'to': c.owner.path,
                'to_index': c.index,
                'from_index': conn.index,
            })

    # Children count if COMP
    if node.isCOMP:
        detail['children_count'] = len(node.children)
        detail['child_names'] = [c.name for c in node.children[:50]]

    return detail


def handle_get_params(body):
    """Get parameters for a specific node."""
    path = body.get('path')
    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    page_filter = body.get('page', None)
    name_filter = body.get('names', None)

    params = {}
    for p in node.pars():
        if page_filter and p.page and p.page.name != page_filter:
            continue
        if name_filter and p.name not in name_filter:
            continue
        try:
            info = {
                'value': p.eval(),
                'default': p.default,
                'label': p.label,
                'page': p.page.name if p.page else '',
                'style': p.style,
                'readOnly': p.readOnly,
                'isPulse': p.isPulse,
                'isMenu': p.isMenu,
                'menuNames': list(p.menuNames) if p.isMenu else [],
            }
            # Include expression info
            try:
                info['expr'] = p.expr if p.expr else ''
                info['mode'] = str(p.mode)
            except Exception:
                info['expr'] = ''
                info['mode'] = 'CONSTANT'
            params[p.name] = info
        except Exception:
            params[p.name] = {'value': str(p), 'error': 'Could not serialize'}

    return {'path': path, 'type': node.type, 'parameters': params}


def handle_set_params(body):
    """Set one or more parameters on a node.

    Each param value can be:
      - A plain value (int, float, str, bool) → sets p.val (static constant)
      - A dict with 'expr' key → sets p.expr (Python expression that updates every frame)
        Example: {"seed": {"expr": "absTime.seconds * 10"}}
        Example: {"tx": {"expr": "op('noise1')['chan1']"}}
      - A dict with 'val' key → explicitly sets p.val (same as plain value)
      - A dict with 'reset': true → resets to default value and clears expression
      - A dict with 'mode': 'constant' → clears expression, optionally sets 'val'

    Expressions make networks REACTIVE — the parameter updates every frame.
    Without expressions, values are static snapshots.
    """
    path = body.get('path')
    params = body.get('params', {})

    if not path:
        return {'error': 'Missing required field: path'}
    if not params:
        return {'error': 'Missing required field: params'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    # v1.4.6 - Bug J silent-null guard for reference-style parameters.
    # TD accepts a plain string assignment to DAT/OP/CHOP/SOP/TOP/COMP/MAT/POP/
    # POPX reference params without raising, but then resolves the value to
    # None internally and emits a node-level warning. Pre-v1.4.6 this handler
    # reported success=True with new_value=null, hiding the failure from the
    # MCP caller. The _silent_null_set check below catches that pattern and
    # flips the per-param result to success=False with the TD warning text.
    # Single-OP styles plus multi-OP list styles. The list styles caught a
    # render TOP regression in 2026-04 where camera/lights/geometry on a
    # render TOP silently resolved to None when assigned a plain string path
    # because their style is COMPS/OPS (list), not COMP/OP (single), so the
    # original singleton-only set let the silent-null slip through.
    REFERENCE_PAR_STYLES = {
        # Single-OP reference styles.
        'DAT', 'OP', 'CHOP', 'SOP', 'TOP', 'COMP', 'MAT', 'POP', 'POPX',
        # Multi-OP list reference styles (TD uses the plural form for
        # render TOP camera/lights/geometry, attribute COMP COMPs, etc.).
        'DATS', 'OPS', 'CHOPS', 'SOPS', 'TOPS', 'COMPS', 'MATS', 'POPS', 'POPXS',
        # OPLIST shows up in some TD builds for the same role.
        'OPLIST',
    }

    def _is_silent_null(par, requested_value, resolved_value):
        """Return True iff the set succeeded syntactically but TD resolved to
        None because the param needs an OP reference (or expression), not a
        plain string. Numeric zeros, empty strings on Str params, and False
        on Toggles all resolve to non-None, so this specifically targets
        None + reference-style + non-empty str."""
        if resolved_value is not None:
            return False
        if not isinstance(requested_value, str):
            return False
        if not requested_value.strip():
            return False
        try:
            style = getattr(par, 'style', '')
        except Exception:
            style = ''
        return style in REFERENCE_PAR_STYLES

    def _build_silent_null_result(par_name, par, requested_value, node_for_warnings):
        """Shape the success=False payload when _is_silent_null trips."""
        try:
            warn_text = (node_for_warnings.warnings() or '').strip()
        except Exception:
            warn_text = ''
        try:
            style = getattr(par, 'style', '')
        except Exception:
            style = ''
        err = (
            'Parameter {0!r} is a reference-style param (style={1}) and '
            "needs an actual OP reference, not a plain string. The value "
            "{2!r} did not resolve - TD assigned null."
        ).format(par_name, style or 'unknown', requested_value)
        # Only cite the TD warning if it looks related to this set.
        if warn_text and (
            'Invalid path' in warn_text
            or 'not found' in warn_text.lower()
            or par_name in warn_text
        ):
            err += ' TD warning: ' + warn_text
        return {
            'success': False,
            'mode': 'constant',
            'error': err,
            'style': style,
            'new_value': None,
        }

    results = {}
    for name, value in params.items():
        try:
            p = getattr(node.par, name, None)
            if p is None:
                results[name] = {'success': False, 'error': f'Parameter not found: {name}'}
                continue
            if p.readOnly:
                results[name] = {'success': False, 'error': f'Parameter is read-only: {name}'}
                continue

            # Expression mode: {"param": {"expr": "absTime.seconds * 10"}}
            if isinstance(value, dict) and 'expr' in value:
                p.expr = value['expr']
                results[name] = {
                    'success': True,
                    'mode': 'expression',
                    'expr': value['expr'],
                    'current_value': p.eval(),
                }
            # Reset to default: {"param": {"reset": true}}
            elif isinstance(value, dict) and value.get('reset'):
                p.val = p.default
                p.expr = ''
                results[name] = {'success': True, 'mode': 'reset', 'new_value': p.eval()}
            # Clear expression, force constant: {"param": {"mode": "constant", "val": 42}}
            elif isinstance(value, dict) and value.get('mode') == 'constant':
                p.expr = ''
                if 'val' in value:
                    p.val = value['val']
                    resolved = p.eval()
                    if _is_silent_null(p, value['val'], resolved):
                        results[name] = _build_silent_null_result(name, p, value['val'], node)
                        continue
                results[name] = {'success': True, 'mode': 'constant', 'new_value': p.eval()}
            # Explicit val mode: {"param": {"val": 42}}
            elif isinstance(value, dict) and 'val' in value:
                p.val = value['val']
                resolved = p.eval()
                if _is_silent_null(p, value['val'], resolved):
                    results[name] = _build_silent_null_result(name, p, value['val'], node)
                    continue
                results[name] = {'success': True, 'mode': 'constant', 'new_value': resolved}
            # Plain value (backwards compatible)
            else:
                p.val = value
                resolved = p.eval()
                if _is_silent_null(p, value, resolved):
                    results[name] = _build_silent_null_result(name, p, value, node)
                    continue
                results[name] = {'success': True, 'mode': 'constant', 'new_value': resolved}
        except Exception as e:
            results[name] = {'success': False, 'error': str(e)}

    return {'path': path, 'results': results}


def handle_create_node(body):
    """Create a new node with optional positioning."""
    parent_path = body.get('parent_path', '/')
    node_type = body.get('node_type')
    name = body.get('name', None)
    node_x = body.get('nodeX', None)
    node_y = body.get('nodeY', None)

    if not node_type:
        return {'error': 'Missing required field: node_type'}

    parent_node = op(parent_path)
    if parent_node is None:
        return {'error': f'Parent node not found: {parent_path}'}

    if not parent_node.isCOMP:
        return {'error': f'Parent is not a COMP: {parent_path}'}

    try:
        new_node = parent_node.create(node_type, name)

        # Set position if provided — keeps networks readable
        if node_x is not None:
            new_node.nodeX = int(node_x)
        if node_y is not None:
            new_node.nodeY = int(node_y)

        return {
            'success': True,
            'node': _serialize_op(new_node),
        }
    except Exception as e:
        return {'error': f'Failed to create node: {str(e)}', 'node_type': node_type}


def handle_delete_node(body):
    """Delete a node by path."""
    path = body.get('path')
    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    node_info = {'name': node.name, 'path': node.path, 'type': node.type}

    try:
        node.destroy()
        return {'success': True, 'deleted': node_info}
    except Exception as e:
        return {'error': f'Failed to delete node: {str(e)}'}


def handle_connect_nodes(body):
    """Connect output of one node to input of another."""
    source_path = body.get('source_path')
    target_path = body.get('target_path')
    source_index = body.get('source_index', 0)
    target_index = body.get('target_index', 0)

    if not source_path or not target_path:
        return {'error': 'Missing required fields: source_path and target_path'}

    source = op(source_path)
    target = op(target_path)

    if source is None:
        return {'error': f'Source node not found: {source_path}'}
    if target is None:
        return {'error': f'Target node not found: {target_path}'}

    # Validate connector indices before access
    num_outputs = len(source.outputConnectors)
    num_inputs = len(target.inputConnectors)
    if source_index >= num_outputs:
        return {'error': f'source_index {source_index} out of range — {source.path} has {num_outputs} output(s) (0–{num_outputs - 1})'}
    if target_index >= num_inputs:
        return {'error': f'target_index {target_index} out of range — {target.path} has {num_inputs} input(s) (0–{num_inputs - 1})'}

    try:
        source.outputConnectors[source_index].connect(target.inputConnectors[target_index])
        return {
            'success': True,
            'connection': {
                'source': source.path,
                'source_index': source_index,
                'target': target.path,
                'target_index': target_index,
            }
        }
    except Exception as e:
        return {'error': f'Failed to connect: {str(e)}'}


def handle_disconnect_nodes(body):
    """Disconnect a node's input or output."""
    path = body.get('path')
    connector_type = body.get('connector_type', 'input')  # 'input' or 'output'
    index = body.get('index', 0)

    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    # Validate connector index
    if connector_type == 'input':
        num_connectors = len(node.inputConnectors)
    else:
        num_connectors = len(node.outputConnectors)
    if index >= num_connectors:
        return {'error': f'{connector_type} index {index} out of range — {path} has {num_connectors} {connector_type}(s) (0–{num_connectors - 1})'}

    try:
        if connector_type == 'input':
            node.inputConnectors[index].disconnect()
        else:
            node.outputConnectors[index].disconnect()
        return {'success': True, 'path': path, 'connector_type': connector_type, 'index': index}
    except Exception as e:
        return {'error': f'Failed to disconnect: {str(e)}'}


def handle_get_connections(body):
    """Get all connections for a node."""
    path = body.get('path')
    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    inputs = []
    for conn in node.inputConnectors:
        for c in conn.connections:
            inputs.append({
                'from_path': c.owner.path,
                'from_index': c.index,
                'to_index': conn.index,
            })

    outputs = []
    for conn in node.outputConnectors:
        for c in conn.connections:
            outputs.append({
                'to_path': c.owner.path,
                'to_index': c.index,
                'from_index': conn.index,
            })

    return {'path': path, 'inputs': inputs, 'outputs': outputs}


def handle_get_errors(body):
    """Get errors/warnings for a node, optionally recursive with depth limit."""
    path = body.get('path', '/')
    recurse = body.get('recurse', True)
    max_depth = body.get('max_depth', 10)  # prevent runaway on huge projects

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    results = []
    truncated = False

    def collect_errors(n, depth=0):
        nonlocal truncated
        errs = n.errors(recurse=False) if hasattr(n, 'errors') else ''
        warns = n.warnings(recurse=False) if hasattr(n, 'warnings') else ''
        if errs or warns:
            results.append({
                'path': n.path,
                'name': n.name,
                'type': n.type,
                'errors': errs,
                'warnings': warns,
            })
        if recurse and n.isCOMP and depth < max_depth:
            for child in n.children:
                collect_errors(child, depth + 1)
        elif recurse and n.isCOMP and depth >= max_depth:
            truncated = True

    collect_errors(node)
    result = {'path': path, 'recurse': recurse, 'count': len(results), 'issues': results}
    if truncated:
        result['warning'] = f'Search truncated at depth {max_depth}. Use max_depth to go deeper.'
    return result


def handle_get_content(body):
    """Get text/table content from a DAT."""
    path = body.get('path')
    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    if not node.isDAT:
        return {'error': f'Node is not a DAT: {path} (type: {node.type})'}

    # Prefer the DAT's own isTable flag over heuristic numRows check.
    # textDAT.numRows is 1 (the full text counted as one row) which previously
    # caused textDAT to be returned as a 1x1 table — unintuitive for callers
    # expecting plain text back from a text DAT.
    is_table = bool(getattr(node, 'isTable', False))
    try:
        if is_table:
            rows = []
            for r in range(node.numRows):
                row = []
                for c in range(node.numCols):
                    row.append(node[r, c].val)
                rows.append(row)
            return {
                'path': path,
                'format': 'table',
                'numRows': node.numRows,
                'numCols': node.numCols,
                'data': rows,
            }
        return {
            'path': path,
            'format': 'text',
            'text': node.text if hasattr(node, 'text') else '',
        }
    except Exception as e:
        return {
            'path': path,
            'format': 'text',
            'text': node.text if hasattr(node, 'text') else '',
            'warning': f'Content read failed, fell back to text: {str(e)}',
        }


def handle_set_content(body):
    """Set text/table content on a DAT."""
    path = body.get('path')
    text = body.get('text', None)
    table = body.get('table', None)

    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    if not node.isDAT:
        return {'error': f'Node is not a DAT: {path}'}

    try:
        if text is not None:
            node.text = text
            return {'success': True, 'path': path, 'format': 'text', 'length': len(text)}
        elif table is not None:
            node.clear()
            wrote = False
            # Preferred path for Table DATs.
            if hasattr(node, 'appendRow'):
                try:
                    for row in table:
                        node.appendRow([str(v) for v in row])
                    wrote = True
                except Exception:
                    node.clear()

            # Fallback path for DATs that expose indexed assignment only.
            if not wrote:
                if hasattr(node, 'setSize'):
                    try:
                        rows = len(table)
                        cols = max((len(row) for row in table), default=0)
                        node.setSize(rows, cols)
                    except Exception:
                        pass
                for r, row in enumerate(table):
                    for c, val in enumerate(row):
                        node[r, c] = val
            return {'success': True, 'path': path, 'format': 'table', 'rows': len(table)}
        else:
            return {'error': 'Provide either "text" or "table" field'}
    except Exception as e:
        return {'error': f'Failed to set content: {str(e)}'}


def handle_copy_node(body):
    """Copy/duplicate a node."""
    source_path = body.get('source_path')
    dest_parent = body.get('dest_parent', None)
    new_name = body.get('new_name', None)
    node_x = body.get('nodeX', None)
    node_y = body.get('nodeY', None)

    if not source_path:
        return {'error': 'Missing required field: source_path'}

    source = op(source_path)
    if source is None:
        return {'error': f'Source node not found: {source_path}'}

    parent = op(dest_parent) if dest_parent else source.parent()
    if parent is None:
        return {'error': f'Destination parent not found: {dest_parent}'}

    try:
        new_node = parent.copy(source, name=new_name)
        # TD's parent.copy() places the new node at the source's position which
        # causes overlap. If the caller supplied explicit coordinates, honor
        # them; otherwise offset by +150 X to keep the copy visible.
        try:
            if node_x is not None:
                new_node.nodeX = int(node_x)
            else:
                new_node.nodeX = int(getattr(source, 'nodeX', 0)) + 150
            if node_y is not None:
                new_node.nodeY = int(node_y)
            else:
                new_node.nodeY = int(getattr(source, 'nodeY', 0))
        except Exception:
            # Node position is cosmetic; don't fail the copy if it can't be set.
            pass
        return {'success': True, 'node': _serialize_op(new_node)}
    except Exception as e:
        return {'error': f'Failed to copy node: {str(e)}'}


def handle_rename_node(body):
    """Rename a node."""
    path = body.get('path')
    new_name = body.get('new_name')

    if not path or not new_name:
        return {'error': 'Missing required fields: path and new_name'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    old_name = node.name
    try:
        node.name = new_name
        return {'success': True, 'old_name': old_name, 'new_name': node.name, 'new_path': node.path}
    except Exception as e:
        return {'error': f'Failed to rename: {str(e)}'}
