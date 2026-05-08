

def _serialize_op(node, include_params=False):
    """Serialize a TD operator to a dict."""
    info = {
        'name': node.name,
        'path': node.path,
        'type': node.type,
        'family': node.family,
        'label': getattr(node, 'label', ''),
        'nodeX': node.nodeX,
        'nodeY': node.nodeY,
        'isCOMP': node.isCOMP,
        'isTOP': node.isTOP,
        'isCHOP': node.isCHOP,
        'isSOP': node.isSOP,
        'isDAT': node.isDAT,
        'isMAT': node.isMAT,
        'isPOP': getattr(node, 'isPOP', False),
        'bypass': node.bypass,
        'lock': node.lock,
        'display': node.display if hasattr(node, 'display') else False,
        'render': node.render if hasattr(node, 'render') else False,
        'errors': node.errors(recurse=False) if hasattr(node, 'errors') else '',
        'warnings': node.warnings(recurse=False) if hasattr(node, 'warnings') else '',
    }
    if include_params:
        info['parameters'] = _serialize_params(node)
    return info


def _serialize_params(node):
    """Serialize all parameters of a node."""
    params = {}
    for p in node.pars():
        try:
            info = {
                'value': p.eval(),
                'default': p.default,
                'label': p.label,
                'page': p.page.name if p.page else '',
                'style': p.style,
                'min': p.min if hasattr(p, 'min') else None,
                'max': p.max if hasattr(p, 'max') else None,
                'readOnly': p.readOnly,
                'isPulse': p.isPulse,
                'isMomentary': p.isMomentary,
                'isToggle': p.isToggle,
                'isMenu': p.isMenu,
                'menuNames': list(p.menuNames) if p.isMenu else [],
                'menuLabels': list(p.menuLabels) if p.isMenu else [],
            }
            # Include expression info — this tells the AI whether a param
            # is static or driven by an expression/export
            try:
                info['expr'] = p.expr if p.expr else ''
                info['mode'] = str(p.mode)
            except Exception:
                info['expr'] = ''
                info['mode'] = 'CONSTANT'
            params[p.name] = info
        except Exception:
            params[p.name] = {'value': str(p), 'error': 'Could not fully serialize'}
    return params


# ─────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────
