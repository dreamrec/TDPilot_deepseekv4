

def handle_custom_parameters(body):
    """Create or update custom parameter pages and parameters on a COMP."""
    path = body.get('path')
    page_name = body.get('page')
    param_specs = body.get('params', [])

    if not path or not page_name:
        return {'error': 'Missing required fields: path and page'}
    if not isinstance(param_specs, list) or not param_specs:
        return {'error': 'Missing required field: params'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}
    if not getattr(node, 'isCOMP', False):
        return {'error': f'Node is not a COMP: {path}'}

    try:
        page, page_created = _find_custom_page(node, page_name)
        created = []
        for spec in param_specs:
            if not isinstance(spec, dict):
                return {'error': 'Each param specification must be an object'}
            created.append(_create_custom_parameter(page, spec))
        return {
            'success': True,
            'path': path,
            'page': page_name,
            'page_created': page_created,
            'count': len(created),
            'parameters': created,
        }
    except Exception as exc:
        return {'error': f'Failed to create custom parameters: {str(exc)}'}


def _normalize_for_check(code):
    """Lowercase and collapse whitespace before '(' to defeat bypass via 'open (' etc."""
    return re.sub(r'\s+\(', '(', code.lower())


def _restricted_exec_violation(code):
    if RESTRICTED_IMPORT_RE.search(code):
        return 'restricted mode blocks import statements'
    normalized = _normalize_for_check(code)
    for token in RESTRICTED_TOKENS:
        if token in normalized:
            return f'restricted mode blocks token: {token}'
    # TD-side sandbox escape: `op(...).create(textDAT)` then write `.text` lets
    # a caller stash arbitrary Python in a DAT and then execute it via
    # `mod.<dat>.func()`. Block the pieces of that pattern in restricted mode.
    # Users who genuinely need DAT authoring should run in standard or full.
    dat_escape_tokens = (
        'create(textdat',
        'create(textDAT'.lower(),
        '.text=',
        '.text =',
        '.par.file=',
        '.par.file =',
    )
    for token in dat_escape_tokens:
        if token in normalized:
            return f'restricted mode blocks DAT-exec pattern: {token}'
    return None


def _standard_exec_violation(code):
    normalized = _normalize_for_check(code)
    for token in STANDARD_BLOCKED_TOKENS:
        if token in normalized:
            return f'standard mode blocks token: {token}'

    for match in RESTRICTED_IMPORT_RE.finditer(code):
        line = match.group(0).strip()
        parts = line.split()
        if parts[0] == 'from':
            mod_name = parts[1]
        else:
            mod_name = parts[1]
        top_level = mod_name.split('.')[0]
        if top_level not in STANDARD_ALLOWED_IMPORTS:
            return f'standard mode blocks import of: {top_level}'

    return None


def _build_exec_globals(exec_mode):
    import importlib as _importlib
    context = {
        'op': op,
        'ops': ops,
        'project': project,
        'app': app,
        'absTime': absTime,
        'me': me,
        'parent': parent,
        'mod': mod,
        'ui': ui,
        'tdu': tdu,
    }
    # Builtins safe for both standard and restricted modes — no exec/eval/open/import/__subclasses__
    safe_builtins = {
        'abs': abs,
        'all': all,
        'any': any,
        'bool': bool,
        'dict': dict,
        'enumerate': enumerate,
        'float': float,
        'getattr': getattr,
        'hasattr': hasattr,
        'isinstance': isinstance,
        'issubclass': issubclass,
        'int': int,
        'len': len,
        'list': list,
        'map': map,
        'filter': filter,
        'max': max,
        'min': min,
        'pow': pow,
        'print': print,
        'range': range,
        'repr': repr,
        'reversed': reversed,
        'round': round,
        'set': set,
        'sorted': sorted,
        'str': str,
        'sum': sum,
        'tuple': tuple,
        'type': type,
        'zip': zip,
    }
    if exec_mode == 'standard':
        for _mod_name in STANDARD_ALLOWED_IMPORTS:
            try:
                context[_mod_name] = _importlib.import_module(_mod_name)
            except ImportError:
                pass
        context['__builtins__'] = safe_builtins
        return context
    if exec_mode != 'restricted':
        return context

    # Restricted mode — even fewer builtins (no getattr/hasattr/type/isinstance)
    restricted_builtins = {k: v for k, v in safe_builtins.items()
                           if k not in ('getattr', 'hasattr', 'isinstance', 'issubclass', 'type', 'map', 'filter', 'repr')}
    safe = {'__builtins__': restricted_builtins}
    safe.update(context)
    return safe


def _json_safe_value(value, depth=0, max_items=128):
    """Best-effort conversion of TouchDesigner/Python values to JSON-safe payloads."""
    if depth > 4:
        return str(value)

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, bytes):
        return base64.b64encode(value).decode('ascii')

    if isinstance(value, dict):
        result = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= max_items:
                result['__truncated__'] = True
                break
            result[str(key)] = _json_safe_value(item, depth + 1, max_items=max_items)
        return result

    if isinstance(value, (list, tuple, set)):
        result = []
        for i, item in enumerate(value):
            if i >= max_items:
                result.append({'__truncated__': True})
                break
            result.append(_json_safe_value(item, depth + 1, max_items=max_items))
        return result

    if hasattr(value, 'path') and hasattr(value, 'name'):
        return {
            'path': str(getattr(value, 'path', '')),
            'name': str(getattr(value, 'name', '')),
            'type': str(getattr(value, 'type', value.__class__.__name__)),
        }

    if all(hasattr(value, attr) for attr in ('x', 'y')):
        coords = [float(value.x), float(value.y)]
        if hasattr(value, 'z'):
            coords.append(float(value.z))
        return coords

    return str(value)


def _serialize_exec_result(value):
    structured_types = (type(None), bool, int, float, str, dict, list, tuple, set)
    return {
        'result': _json_safe_value(value),
        'result_type': type(value).__name__ if value is not None else 'NoneType',
        'result_is_structured': isinstance(value, structured_types),
    }


def _safe_td_call(target, attr_name, *args, **kwargs):
    attr = getattr(target, attr_name, None)
    if attr is None:
        return None
    if not callable(attr):
        return attr
    try:
        return attr(*args, **kwargs)
    except TypeError:
        try:
            return attr(*args)
        except Exception:
            return None
    except Exception:
        return None


def _parse_attribute_descriptor(item):
    text = str(item)
    match = re.match(r"^([^:]+):\s*(\d+)\s*<class '([^']+)'>$", text)
    if match:
        return {
            'name': match.group(1),
            'size': int(match.group(2)),
            'type': match.group(3),
            'raw': text,
        }
    return {'name': text, 'raw': text}


def _collect_attribute_descriptors(collection):
    descriptors = []
    try:
        for item in collection:
            descriptors.append(_parse_attribute_descriptor(item))
    except Exception:
        pass
    return descriptors


def _attribute_names(descriptors):
    names = []
    for item in descriptors:
        name = item.get('name')
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _serialize_bounds(bounds):
    if bounds is None:
        return None

    def _vec(obj):
        if obj is None:
            return None
        result = []
        for attr in ('x', 'y', 'z'):
            if hasattr(obj, attr):
                result.append(float(getattr(obj, attr)))
        return result or None

    return {
        'min': _vec(getattr(bounds, 'min', None)),
        'max': _vec(getattr(bounds, 'max', None)),
        'center': _vec(getattr(bounds, 'center', None)),
        'size': _vec(getattr(bounds, 'size', None)),
    }


def _sample_pop_attribute(node, method_name, attr_name, start, count, delayed):
    method = getattr(node, method_name, None)
    if not callable(method):
        return {'error': f'{method_name} unavailable'}
    try:
        values = method(attr_name, startIndex=start, count=count, delayed=delayed)
    except TypeError:
        try:
            values = method(attr_name, start, count, delayed)
        except Exception as exc:
            return {'error': str(exc)}
    except Exception as exc:
        return {'error': str(exc)}

    return {
        'attribute': attr_name,
        'start': start,
        'count': count,
        'values': _json_safe_value(values),
    }


def _pick_default_pop_samples(available_names):
    preferred = ['P', 'PartVel', 'PartAge', 'PartLifeSpan', 'Noise', 'PartForce', 'PartId']
    result = []
    for name in preferred:
        if name in available_names:
            result.append(name)
        if len(result) >= 4:
            break
    return result


def _group_members(group):
    if group is None:
        return []
    if isinstance(group, (list, tuple)):
        return list(group)
    try:
        members = list(group)
        if members:
            return members
    except Exception:
        pass
    return [group]


def _broadcast_values(value, count):
    if count <= 0:
        return []
    if isinstance(value, (list, tuple)):
        values = list(value)
        if not values:
            return [None] * count
        if len(values) >= count:
            return values[:count]
        return values + [values[-1]] * (count - len(values))
    return [value] * count


def _set_attr_if_supported(target, attr_name, value):
    if value is None or target is None:
        return
    try:
        setattr(target, attr_name, value)
    except Exception:
        pass


def _assign_custom_group_defaults(group, spec):
    members = _group_members(group)
    if not members:
        return

    if spec.get('menu_names'):
        menu_names = list(spec.get('menu_names') or [])
        menu_labels = list(spec.get('menu_labels') or menu_names)
        for target in [group] + members[:1]:
            _set_attr_if_supported(target, 'menuNames', menu_names)
            _set_attr_if_supported(target, 'menuLabels', menu_labels)

    for attr_name in ('min', 'max', 'norm_min', 'norm_max', 'clamp_min', 'clamp_max'):
        source_value = spec.get(attr_name)
        target_attr = {
            'norm_min': 'normMin',
            'norm_max': 'normMax',
            'clamp_min': 'clampMin',
            'clamp_max': 'clampMax',
        }.get(attr_name, attr_name)
        for member in members:
            _set_attr_if_supported(member, target_attr, source_value)

    if spec.get('default', None) is None:
        return

    for member, value in zip(members, _broadcast_values(spec.get('default'), len(members))):
        _set_attr_if_supported(member, 'default', value)
        _set_attr_if_supported(member, 'val', value)


def _find_custom_page(node, page_name):
    for collection_name in ('customPages', 'pages'):
        pages = getattr(node, collection_name, None)
        if pages is None:
            continue
        try:
            for page in pages:
                if str(getattr(page, 'name', '')) == page_name:
                    return page, False
        except Exception:
            continue
    return node.appendCustomPage(page_name), True


def _create_custom_parameter(page, spec):
    kind = str(spec.get('kind', '')).lower()
    method_name = {
        'float': 'appendFloat',
        'int': 'appendInt',
        'toggle': 'appendToggle',
        'menu': 'appendMenu',
        'str': 'appendStr',
        'string': 'appendStr',
        'rgb': 'appendRGB',
        'rgba': 'appendRGBA',
        'pulse': 'appendPulse',
        'file': 'appendFile',
        'filesave': 'appendFileSave',
        'folder': 'appendFolder',
        'chop': 'appendCHOP',
        'comp': 'appendCOMP',
        'dat': 'appendDAT',
        'mat': 'appendMAT',
        'header': 'appendHeader',
    }.get(kind)
    if not method_name:
        raise ValueError(f"Unsupported custom parameter kind: {kind}")

    method = getattr(page, method_name, None)
    if not callable(method):
        raise ValueError(f"Page does not support method {method_name}")

    kwargs = {
        'label': spec.get('label'),
        'order': spec.get('order'),
        'replace': bool(spec.get('replace', True)),
    }
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    if kind in ('float', 'int'):
        kwargs['size'] = int(spec.get('size', 1) or 1)

    group = method(spec.get('name'), **kwargs)
    _assign_custom_group_defaults(group, spec)
    members = _group_members(group)
    return {
        'kind': kind,
        'name': spec.get('name'),
        'label': spec.get('label') or spec.get('name'),
        'member_count': len(members),
        'members': [str(getattr(member, 'name', member)) for member in members],
    }
