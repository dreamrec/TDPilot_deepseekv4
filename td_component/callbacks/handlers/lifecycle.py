

def _project_lifecycle_status():
    modified = getattr(project, 'modified', [])
    try:
        modified_count = len(modified)
    except Exception:
        modified_count = 0

    undo_obj = getattr(ui, 'undo', None)
    undo_stack = list(getattr(undo_obj, 'undoStack', []) or []) if undo_obj is not None else []
    redo_stack = list(getattr(undo_obj, 'redoStack', []) or []) if undo_obj is not None else []

    return {
        'project': {
            'name': str(getattr(project, 'name', '')),
            'folder': str(getattr(project, 'folder', '')),
            'saveVersion': str(getattr(project, 'saveVersion', '')),
            'saveBuild': str(getattr(project, 'saveBuild', '')),
            'modifiedCount': modified_count,
        },
        'undo': {
            'state': bool(getattr(undo_obj, 'state', False)) if undo_obj is not None else False,
            'globalState': bool(getattr(undo_obj, 'globalState', False)) if undo_obj is not None else False,
            'undoStack': undo_stack,
            'redoStack': redo_stack,
        },
    }


def handle_project_lifecycle(body):
    """Inspect and control save/load/undo lifecycle operations."""
    action = str(body.get('action', 'status')).strip().lower() or 'status'
    path = body.get('path')
    save_external_toxs = bool(body.get('save_external_toxs', False))
    undo_obj = getattr(ui, 'undo', None)

    if action == 'status':
        payload = {'success': True, 'action': action}
        payload.update(_project_lifecycle_status())
        return payload

    try:
        if action == 'save':
            if path:
                saved = project.save(path, saveExternalToxs=save_external_toxs)
            else:
                saved = project.save(saveExternalToxs=save_external_toxs)
            payload = {
                'success': bool(saved),
                'action': action,
                'path': path or str(getattr(project, 'name', '')),
                'save_external_toxs': save_external_toxs,
            }
            payload.update(_project_lifecycle_status())
            return payload

        if action == 'load':
            if not path:
                return {'error': 'Path is required for action=load'}
            project.load(path)
            payload = {'success': True, 'action': action, 'path': path}
            payload.update(_project_lifecycle_status())
            return payload

        if undo_obj is None:
            return {'error': 'ui.undo is unavailable in this TouchDesigner build/context'}

        soft_warning = None
        if action == 'undo':
            undo_obj.undo()
        elif action == 'redo':
            undo_obj.redo()
        elif action == 'start_undo_block':
            undo_obj.startBlock(body.get('name') or 'TDPilot Edit', enable=bool(body.get('enable', True)))
        elif action == 'end_undo_block':
            # TD auto-closes the active block on certain cascading mutations
            # (e.g. deleting the parent COMP that contained the block scope).
            # Calling endBlock() on an already-closed block raises "Cannot
            # end non existent undo operation". Treat that as a no-op with a
            # soft warning instead of a hard failure — the block is already
            # closed, which is the caller's desired end state.
            try:
                undo_obj.endBlock()
            except Exception as block_exc:
                msg = str(block_exc).lower()
                if 'non existent' in msg or 'nonexistent' in msg or 'no undo' in msg:
                    soft_warning = (
                        'Undo block was already closed (TouchDesigner auto-closes '
                        'blocks on certain cascading mutations like cross-scope deletes). '
                        'Treating end_undo_block as idempotent.'
                    )
                else:
                    raise
        elif action == 'clear_undo':
            undo_obj.clear()
        else:
            return {'error': f'Unknown project lifecycle action: {action}'}

        payload = {'success': True, 'action': action}
        if soft_warning is not None:
            payload['warning'] = soft_warning
        payload.update(_project_lifecycle_status())
        return payload
    except Exception as exc:
        return {'error': f'Project lifecycle action failed: {str(exc)}', 'action': action}


def handle_timeline(body):
    """Get timeline/playback state."""
    timeline = None
    try:
        timeline = (op('/project1') or op('/')).time
    except Exception:
        timeline = None

    frame = absTime.frame
    seconds = absTime.seconds
    playing = project.realTime
    fps = project.cookRate
    start = project.cookRange[0] if hasattr(project, 'cookRange') else 1
    end = project.cookRange[1] if hasattr(project, 'cookRange') else 600

    if timeline is not None:
        for attr, target in (
            ('frame', 'frame'),
            ('seconds', 'seconds'),
            ('play', 'playing'),
            ('rate', 'fps'),
            ('start', 'start'),
            ('end', 'end'),
        ):
            try:
                value = getattr(timeline, attr)
            except Exception:
                continue
            if target == 'frame':
                frame = value
            elif target == 'seconds':
                seconds = value
            elif target == 'playing':
                playing = value
            elif target == 'fps':
                fps = value
            elif target == 'start':
                start = value
            elif target == 'end':
                end = value

    return {
        'frame': frame,
        'seconds': seconds,
        'playing': playing,
        'fps': fps,
        'start': start,
        'end': end,
    }


def handle_timeline_set(body):
    """Control timeline playback."""
    action = body.get('action')  # play, pause, frame
    frame = body.get('frame', None)
    fps = body.get('fps', None)
    timeline = None
    try:
        timeline = (op('/project1') or op('/')).time
    except Exception:
        timeline = None

    if action == 'play':
        if timeline is not None:
            try:
                timeline.play = True
            except Exception:
                project.realTime = True
        else:
            project.realTime = True
        return {'success': True, 'playing': True}
    elif action == 'pause':
        if timeline is not None:
            try:
                timeline.play = False
            except Exception:
                project.realTime = False
        else:
            project.realTime = False
        return {'success': True, 'playing': False}
    elif action == 'frame' and frame is not None:
        target = int(frame)
        if timeline is not None:
            try:
                timeline.frame = target
                return {'success': True, 'frame': timeline.frame}
            except Exception:
                pass
        try:
            absTime.frame = target
            return {'success': True, 'frame': absTime.frame}
        except Exception as e:
            return {'error': f'Failed to set frame: {str(e)}'}
    elif fps is not None:
        if timeline is not None:
            try:
                timeline.rate = float(fps)
            except Exception:
                pass
        project.cookRate = fps
        return {'success': True, 'fps': project.cookRate}
    else:
        return {'error': 'Provide action (play/pause/frame) or fps'}
