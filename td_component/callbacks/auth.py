

def _extract_headers(request):
    headers = {}
    raw = request.get('headers', {})
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key is None:
                continue
            headers[str(key).strip().lower()] = str(value).strip()
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                headers[str(item[0]).strip().lower()] = str(item[1]).strip()
            elif isinstance(item, str) and ':' in item:
                key, value = item.split(':', 1)
                headers[key.strip().lower()] = value.strip()

    for key in ('authorization', 'x-td-mcp-secret'):
        if key in request and key not in headers:
            headers[key] = str(request.get(key)).strip()
    return headers


def _check_auth_error(request):
    # Read env per-request so updating TD_MCP_SHARED_SECRET or
    # TD_MCP_REQUIRE_AUTH takes effect without bouncing the callbacks module.
    # Module-level SHARED_SECRET/REQUIRE_AUTH stay as back-compat aliases only.
    secret = _current_shared_secret()
    require = _current_require_auth()

    if not secret:
        if require:
            return (
                'TD_MCP_SHARED_SECRET is not configured. Set it in the TD env or run '
                'scripts/render_mcp_config.py to generate one. To opt out for local dev, '
                'set TD_MCP_REQUIRE_AUTH=0 (not recommended).'
            )
        return None

    headers = _extract_headers(request)
    token = headers.get('x-td-mcp-secret', '')
    if not token:
        auth = headers.get('authorization', '')
        if auth.lower().startswith('bearer '):
            token = auth.split(' ', 1)[1].strip()

    if _constant_time_equals(token, secret):
        return None
    return 'Unauthorized: missing or invalid TD_MCP_SHARED_SECRET.'


def _constant_time_equals(a, b):
    """Compare two strings without early-exit timing leaks."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    if len(a) != len(b):
        # Still do a dummy compare to avoid length-timing signal
        _ = sum(ord(x) for x in a) ^ sum(ord(y) for y in b)
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def _send_json(response, data):
    """Helper to serialize and set JSON response."""
    response['data'] = json.dumps(data, default=str).encode('utf-8')
    response['content-type'] = 'application/json'
