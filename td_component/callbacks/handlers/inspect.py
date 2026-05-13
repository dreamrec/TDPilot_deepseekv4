

def handle_screenshot(body):
    """Capture a TOP as a JPEG image and return base64.

    Uses JPEG with configurable quality (0.0–1.0 scale, TD 2025+) to keep
    captures well under the 1 MB MCP response-size limit after base64 encoding.
    """
    path = body.get('path', None)
    quality = float(body.get('quality', 0.5))  # TD 2025 uses 0.0–1.0 scale

    try:
        if path:
            target = op(path)
            if target is None:
                return {'error': f'Node not found: {path}'}
            if not target.isTOP:
                return {'error': f'Node is not a TOP: {path} (type: {target.type})'}
        else:
            return {'error': 'Provide path to a TOP node to screenshot'}

        # Use saveByteArray for in-memory JPEG capture
        img_bytes = target.saveByteArray('.jpg', quality=quality)
        img_b64 = base64.b64encode(bytes(img_bytes)).decode('ascii')

        return {
            'success': True,
            'path': target.path,
            'width': target.width,
            'height': target.height,
            'format': 'jpeg',
            'data_base64': img_b64,
            'size_bytes': len(img_bytes),
        }
    except Exception as e:
        # Fallback: try file-based save
        try:
            target.save(SCREENSHOT_TEMP_PATH, quality=quality)
            with open(SCREENSHOT_TEMP_PATH, 'rb') as f:
                img_bytes = f.read()
            img_b64 = base64.b64encode(img_bytes).decode('ascii')
            return {
                'success': True,
                'path': target.path,
                'format': 'jpeg',
                'data_base64': img_b64,
                'size_bytes': len(img_bytes),
                'method': 'file_fallback',
            }
        except Exception as e2:
            return {'error': f'Screenshot failed: {str(e)} / fallback: {str(e2)}'}


def handle_chop_data(body):
    """Read channel data from a CHOP."""
    path = body.get('path')
    channel_names = body.get('channels', None)  # list of names, or None for all
    sample_range = body.get('range', None)  # [start, end] or None for all

    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    if not node.isCHOP:
        return {'error': f'Node is not a CHOP: {path}'}

    result = {
        'path': path,
        'numChans': node.numChans,
        'numSamples': node.numSamples,
        'rate': node.rate,
        'channels': {},
    }

    for chan in node.chans():
        if channel_names and chan.name not in channel_names:
            continue

        samples = list(chan.vals)
        if sample_range:
            start = max(0, sample_range[0])
            end = min(len(samples), sample_range[1])
            samples = samples[start:end]

        # Limit to 1000 samples max to avoid huge responses
        if len(samples) > 1000:
            step = len(samples) // 1000
            samples = samples[::step]
            result['channels'][chan.name] = {
                'values': samples,
                'downsampled': True,
                'original_length': node.numSamples,
            }
        else:
            result['channels'][chan.name] = {
                'values': samples,
                'downsampled': False,
            }

    return result


def handle_geometry_data(body):
    """Read geometry data from a SOP or POP."""
    path = body.get('path')
    include_points = body.get('include_points', True)
    include_prims = body.get('include_prims', False)
    limit = body.get('limit', 500)

    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    is_sop = bool(getattr(node, 'isSOP', False))
    is_pop = bool(getattr(node, 'isPOP', False))
    if not (is_sop or is_pop):
        return {'error': f'Node is not a SOP/POP geometry operator: {path}'}

    def _safe_count(attr_name):
        value = getattr(node, attr_name, 0)
        if callable(value):
            try:
                value = value()
            except Exception:
                return 0
        try:
            return int(value or 0)
        except Exception:
            return 0

    family = 'POP' if is_pop and not is_sop else 'SOP'
    num_points = _safe_count('numPoints')
    num_prims = _safe_count('numPrims')
    num_vertices = _safe_count('numVertices')

    result = {
        'path': path,
        'family': family,
        'numPoints': num_points,
        'numPrims': num_prims,
        'numVertices': num_vertices,
    }

    if include_points:
        points_iter = []
        points_attr = getattr(node, 'points', None)
        if points_attr is not None:
            try:
                points_iter = points_attr() if callable(points_attr) else points_attr
            except Exception:
                points_iter = []

        def _point_xyz(point):
            if all(hasattr(point, k) for k in ('x', 'y', 'z')):
                return float(point.x), float(point.y), float(point.z)
            for attr in ('P', 'position', 'pos'):
                if not hasattr(point, attr):
                    continue
                value = getattr(point, attr)
                try:
                    if callable(value):
                        value = value()
                    if isinstance(value, (tuple, list)) and len(value) >= 3:
                        return float(value[0]), float(value[1]), float(value[2])
                except Exception:
                    continue
            try:
                return float(point[0]), float(point[1]), float(point[2])
            except Exception:
                return None

        points = []
        for i, pt in enumerate(points_iter):
            if i >= limit:
                break
            point_data = {'index': int(getattr(pt, 'index', i))}
            xyz = _point_xyz(pt)
            if xyz is not None:
                point_data['x'] = xyz[0]
                point_data['y'] = xyz[1]
                point_data['z'] = xyz[2]
            else:
                point_data['raw'] = str(pt)
            points.append(point_data)
        result['points'] = points
        result['points_truncated'] = num_points > limit

    if include_prims:
        prims = []
        prims_attr = getattr(node, 'prims', None)
        if prims_attr is not None:
            try:
                prims_iter = prims_attr() if callable(prims_attr) else prims_attr
            except Exception:
                prims_iter = []
            for i, prim in enumerate(prims_iter):
                if i >= limit:
                    break
                # TD's Prim objects expose vertex count via len(prim), not a
                # numVertices attribute. The old getattr lookup always returned
                # the default 0 because TD never added that name. See N4 audit.
                try:
                    vert_count = int(len(prim))
                except Exception:
                    vert_count = int(getattr(prim, 'numVertices', 0) or 0)
                prims.append({
                    'index': int(getattr(prim, 'index', i)),
                    'numVertices': vert_count,
                })
        result['prims'] = prims
        result['prims_truncated'] = num_prims > limit

    return result


def handle_pop_inspect(body):
    """Read POP-specific metadata and attribute samples."""
    path = body.get('path')
    if not path:
        return {'error': 'Missing required field: path'}

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}
    if not bool(getattr(node, 'isPOP', False)):
        return {'error': f'Node is not a POP: {path}'}

    include_bounds = bool(body.get('include_bounds', True))
    include_attributes = bool(body.get('include_attributes', True))
    start = max(0, int(body.get('start', 0) or 0))
    count = max(1, min(int(body.get('count', 32) or 32), 2048))
    delayed = bool(body.get('delayed', False))

    point_descriptors = _collect_attribute_descriptors(getattr(node, 'pointAttributes', []))
    prim_descriptors = _collect_attribute_descriptors(getattr(node, 'primAttributes', []))
    vert_descriptors = _collect_attribute_descriptors(getattr(node, 'vertAttributes', []))

    point_names = _attribute_names(point_descriptors)
    prim_names = _attribute_names(prim_descriptors)
    vert_names = _attribute_names(vert_descriptors)

    requested_point = body.get('point_attributes')
    requested_prim = body.get('prim_attributes')
    requested_vert = body.get('vert_attributes')

    if not isinstance(requested_point, list):
        requested_point = _pick_default_pop_samples(point_names)
    if not isinstance(requested_prim, list):
        requested_prim = []
    if not isinstance(requested_vert, list):
        requested_vert = []

    payload = {
        'path': path,
        'family': 'POP',
        'summary': {
            'numPoints': _safe_td_call(node, 'numPoints', delayed) or 0,
            'numPointsAllocated': _safe_td_call(node, 'numPoints', False, True) or 0,
            'numPrims': _safe_td_call(node, 'numPrims', delayed) or 0,
            'numPrimsAllocated': _safe_td_call(node, 'numPrims', False, True) or 0,
            'numVerts': _safe_td_call(node, 'numVerts', delayed) or 0,
            'numVertsAllocated': _safe_td_call(node, 'numVerts', False, True) or 0,
            'dimension': str(getattr(node, 'dimension', '')),
            'maxVertsPerLineStrip': _safe_td_call(node, 'maxVertsPerLineStrip') or 0,
        },
        'sampling': {
            'start': start,
            'count': count,
            'delayed': delayed,
        },
    }

    if include_bounds:
        payload['bounds'] = _serialize_bounds(_safe_td_call(node, 'computeBounds', False, False, delayed))

    if include_attributes:
        payload['attributes'] = {
            'point': point_descriptors,
            'point_changed': _collect_attribute_descriptors(getattr(node, 'pointAttributesChanged', [])),
            'prim': prim_descriptors,
            'prim_changed': _collect_attribute_descriptors(getattr(node, 'primAttributesChanged', [])),
            'vert': vert_descriptors,
            'vert_changed': _collect_attribute_descriptors(getattr(node, 'vertAttributesChanged', [])),
        }

    point_samples = {}
    for attr_name in requested_point:
        if attr_name in point_names:
            point_samples[attr_name] = _sample_pop_attribute(node, 'points', attr_name, start, count, delayed)

    prim_samples = {}
    for attr_name in requested_prim:
        if attr_name in prim_names:
            prim_samples[attr_name] = _sample_pop_attribute(node, 'prims', attr_name, start, count, delayed)

    vert_samples = {}
    for attr_name in requested_vert:
        if attr_name in vert_names:
            vert_samples[attr_name] = _sample_pop_attribute(node, 'verts', attr_name, start, count, delayed)

    payload['samples'] = {
        'points': point_samples,
        'prims': prim_samples,
        'verts': vert_samples,
    }
    payload['notes'] = [
        'POP downloads can stall the GPU. Use delayed=true for repeat sampling workflows.',
        'Request only the attributes you need for debugging large particle systems.',
    ]
    return payload


def handle_cooking_info(body):
    """Get cooking/performance info for a node.

    Reports CPU cook time (``cookTime``/``cpuCookTime``), GPU cook time
    (``gpuCookTime`` — always 0 on non-TOP operators), and per-TOP VRAM
    footprint (``cudaMemoryBytes`` — ``None`` on non-TOPs and when TD
    can't measure). Use ``sort_by="gpuCookTime"`` to find GPU bottlenecks
    (feedback loops, large GLSL TOPs, heavy compositors) or
    ``sort_by="cudaMemoryBytes"`` to find VRAM hogs.
    """
    path = body.get('path', '/')
    recurse = body.get('recurse', False)
    # cookTime, cpuCookTime, gpuCookTime, cudaMemoryBytes
    sort_by = body.get('sort_by', 'cookTime')
    limit = body.get('limit', 20)

    node = op(path)
    if node is None:
        return {'error': f'Node not found: {path}'}

    results = []

    def collect_cook(n):
        try:
            entry = {
                'path': n.path,
                'name': n.name,
                'type': n.type,
                'cookTime': n.cookTime if hasattr(n, 'cookTime') else 0,
                'cpuCookTime': n.cpuCookTime if hasattr(n, 'cpuCookTime') else 0,
                'gpuCookTime': float(getattr(n, 'gpuCookTime', 0.0) or 0.0),
                'cookFrame': n.cookFrame if hasattr(n, 'cookFrame') else 0,
            }
            # cudaMemory() is per-pixel-format and exists only on TOPs.
            # Omit pixelFormat so TD reports the current format.
            if getattr(n, 'family', None) == 'TOP':
                try:
                    entry['cudaMemoryBytes'] = n.cudaMemory()
                except Exception:
                    entry['cudaMemoryBytes'] = None
            else:
                entry['cudaMemoryBytes'] = None
            results.append(entry)
        except Exception:
            # Skip nodes that can't provide cook info (e.g., locked or internal)
            pass  # intentional — not all nodes expose cook timing
        if recurse and n.isCOMP:
            for child in n.children:
                collect_cook(child)

    collect_cook(node)

    # Sort
    results.sort(key=lambda x: x.get(sort_by, 0), reverse=True)
    results = results[:limit]

    return {
        'path': path,
        'fps': project.cookRate,
        'realTime': project.realTime,
        'frame': absTime.frame,
        'total_nodes': len(results),
        'nodes': results,
    }
