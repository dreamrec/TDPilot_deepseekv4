

def handle_exec_python(body):
    """Execute arbitrary Python code inside TouchDesigner.

    Supports an optional timeout_ms parameter (default 10000) to prevent
    runaway code from freezing TD.  Uses a threading.Timer to interrupt
    execution via a KeyboardInterrupt if it exceeds the limit.
    """
    code = body.get('code')
    if not code:
        return {'error': 'Missing required field: code'}

    _MODE_RANK = {'off': 0, 'restricted': 1, 'standard': 2, 'full': 3}
    default_mode = _current_exec_mode()  # read env per-request; see A-1
    exec_mode = str(body.get('exec_mode', default_mode)).strip().lower()
    if exec_mode not in _MODE_RANK:
        exec_mode = default_mode
    # Cap requested mode at server-configured default — clients cannot escalate
    if _MODE_RANK.get(exec_mode, 0) > _MODE_RANK.get(default_mode, 0):
        exec_mode = default_mode

    if exec_mode == 'off':
        return {
            'error': 'Python execution is disabled by TD_MCP_EXEC_MODE=off',
            'type': 'PermissionError',
            'exec_mode': exec_mode,
        }

    if exec_mode == 'restricted':
        violation = _restricted_exec_violation(code)
        if violation:
            return {
                'error': violation,
                'type': 'PermissionError',
                'exec_mode': exec_mode,
            }

    if exec_mode == 'standard':
        violation = _standard_exec_violation(code)
        if violation:
            return {
                'error': violation,
                'type': 'PermissionError',
                'exec_mode': exec_mode,
            }

    timeout_ms = body.get('timeout_ms', 10000)
    timeout_sec = max(1, min(timeout_ms / 1000, 30))  # clamp 1–30s

    # Capture stdout
    import ctypes
    import io
    import threading
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    timed_out = [False]

    def _timeout_handler():
        """Interrupt the main thread if execution takes too long."""
        timed_out[0] = True
        # Raise KeyboardInterrupt in the main thread
        try:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(threading.main_thread().ident),
                ctypes.py_object(KeyboardInterrupt)
            )
        except Exception:
            pass  # best-effort timeout

    timer = threading.Timer(timeout_sec, _timeout_handler)
    result_value = None
    try:
        sys.stdout = captured_out
        sys.stderr = captured_err
        timer.start()
        exec_globals = _build_exec_globals(exec_mode)

        # Try exec first, fall back to eval for expressions
        try:
            exec(code, exec_globals)
            # Check if there's a __result__ variable
            result_value = exec_globals.get('__result__', None)
        except SyntaxError:
            # Might be a simple expression
            result_value = eval(code, exec_globals)
    except KeyboardInterrupt:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        timer.cancel()
        return {
            'error': f'Execution timed out after {timeout_sec}s. Avoid infinite loops or blocking operations.',
            'type': 'TimeoutError',
            'exec_mode': exec_mode,
            'stdout': captured_out.getvalue(),
            'stderr': captured_err.getvalue(),
        }
    except Exception as e:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        timer.cancel()
        return {
            'error': str(e),
            'type': type(e).__name__,
            'exec_mode': exec_mode,
            'traceback': traceback.format_exc(),
            'stdout': captured_out.getvalue(),
            'stderr': captured_err.getvalue(),
        }
    finally:
        timer.cancel()
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    payload = {
        'success': True,
        'exec_mode': exec_mode,
        'stdout': captured_out.getvalue(),
        'stderr': captured_err.getvalue(),
    }
    payload.update(_serialize_exec_result(result_value))
    return payload
