

def handle_pulse_param(body):
    """Pulse a pulse-type parameter."""
    path = body.get('path')
    param_name = body.get('param')

    if not path or not param_name:
        return {'error': 'Missing required fields: path and param'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    p = getattr(node.par, param_name, None)
    if p is None:
        return {'error': f'Parameter not found: {param_name} on {path}'}

    try:
        p.pulse()
        return {'success': True, 'path': path, 'param': param_name}
    except Exception as e:
        return {'error': f'Failed to pulse: {str(e)}'}
