
def onHTTPRequest(webServerDAT, request, response):
    """
    Main entry point for all MCP server requests.
    Routes to handler functions based on URI path.
    """
    uri = request.get('uri', '/')
    method = request.get('method', 'GET')

    # Parse JSON body
    body = {}
    raw_data = request.get('data', None)
    if raw_data:
        try:
            if isinstance(raw_data, bytes):
                body = json.loads(raw_data.decode('utf-8'))
            elif isinstance(raw_data, str):
                body = json.loads(raw_data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}

    # CORS: apply only when a specific trusted origin is configured.
    # Never emit '*' — combined with weak auth, that lets any webpage drive TD.
    cors_origin = _current_cors_origin()
    if cors_origin:
        response['Access-Control-Allow-Origin'] = cors_origin
        response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-TD-MCP-Secret'
        response['Vary'] = 'Origin'

    # Handle OPTIONS preflight
    if method == 'OPTIONS':
        response['statusCode'] = 204
        response['statusReason'] = 'No Content'
        response['data'] = ''
        return response

    # Reject cross-site requests from browsers — MCP clients don't set Sec-Fetch-Site,
    # so this only blocks malicious webpage fetches. Same-origin/none is allowed.
    headers = _extract_headers(request)
    fetch_site = headers.get('sec-fetch-site', '')
    if fetch_site and fetch_site not in ('same-origin', 'none'):
        response['statusCode'] = 403
        response['statusReason'] = 'Forbidden'
        _send_json(response, {'error': f'cross-site fetch blocked (Sec-Fetch-Site={fetch_site})'})
        return response

    # C-1 part B (v2.5.4 audit follow-up). Defense-in-depth on top of
    # Sec-Fetch-Site: explicitly reject Origin headers that aren't
    # loopback. Empty / missing Origin is still accepted so non-browser
    # MCP clients (curl, npx tdpilot-dpsk4, custom integrations) keep
    # working. See _is_origin_allowed in _header.py for the contract.
    origin = headers.get('origin', '')
    if origin and not _is_origin_allowed(origin):
        response['statusCode'] = 403
        response['statusReason'] = 'Forbidden'
        _send_json(response, {'error': f'cross-origin request blocked (Origin={origin})'})
        return response

    auth_error = _check_auth_error(request)
    if auth_error:
        response['statusCode'] = 401
        response['statusReason'] = 'Unauthorized'
        _send_json(response, {'error': auth_error})
        return response

    try:
        # ── Route table ──
        routes = {
            '/api/health':              handle_health,
            '/api/info':                handle_info,
            '/api/nodes':               handle_get_nodes,
            '/api/node/detail':         handle_get_node_detail,
            '/api/node/params':         handle_get_params,
            '/api/node/params/set':     handle_set_params,
            '/api/node/create':         handle_create_node,
            '/api/node/delete':         handle_delete_node,
            '/api/node/connect':        handle_connect_nodes,
            '/api/node/disconnect':     handle_disconnect_nodes,
            '/api/node/connections':    handle_get_connections,
            '/api/node/errors':         handle_get_errors,
            '/api/node/content':        handle_get_content,
            '/api/node/content/set':    handle_set_content,
            '/api/node/copy':           handle_copy_node,
            '/api/node/rename':         handle_rename_node,
            '/api/custom-parameters':   handle_custom_parameters,
            '/api/exec':                handle_exec_python,
            '/api/screenshot':          handle_screenshot,
            '/api/chop/data':           handle_chop_data,
            '/api/geometry/data':       handle_geometry_data,
            '/api/pop/inspect':         handle_pop_inspect,
            '/api/cooking':             handle_cooking_info,
            '/api/search':              handle_search_nodes,
            '/api/families':            handle_list_families,
            '/api/python/help':         handle_python_help,
            '/api/python/classes':      handle_python_classes,
            '/api/project/lifecycle':   handle_project_lifecycle,
            '/api/timeline':            handle_timeline,
            '/api/timeline/set':        handle_timeline_set,
            '/api/pulse':               handle_pulse_param,
            '/api/monitor/subscribe':   handle_monitor_subscribe,
            '/api/monitor/unsubscribe': handle_monitor_unsubscribe,
            '/api/analyze_frame':       handle_analyze_frame,
        }

        handler = routes.get(uri)
        if handler:
            result = handler(body)
        else:
            result = {'error': f'Unknown endpoint: {uri}', 'available': list(routes.keys())}
            response['statusCode'] = 404
            response['statusReason'] = 'Not Found'
            _send_json(response, result)
            return response

        response['statusCode'] = 200
        response['statusReason'] = 'OK'
        _send_json(response, result)

    except Exception as e:
        # M-1 (v2.5.4 audit follow-up). Apply _redact_paths to the
        # traceback before serialising — strips $HOME / config-dir
        # absolute paths so a 500 response can't leak the user's
        # username / install location to a same-machine attacker who
        # hit an error path. The original chat-pipe-side equivalent
        # lives in tdpilot_api_config.redact_paths.
        error_result = {
            'error': _redact_paths(str(e)),
            'type': type(e).__name__,
            'traceback': _redact_paths(traceback.format_exc()),
        }
        response['statusCode'] = 500
        response['statusReason'] = 'Internal Server Error'
        _send_json(response, error_result)

    return response
