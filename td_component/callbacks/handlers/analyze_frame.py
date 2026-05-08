
def handle_analyze_frame(body):
    """Analyze pixel data of a TOP node using numpy.

    Supported modes: histogram, luminance, alpha_coverage, color_dominant, roi_diff.
    Returns per-mode result dicts plus resolution metadata.
    """
    path = body.get('path')
    if not path:
        return {'error': 'Missing required field: path'}

    modes = body.get('modes', ['histogram', 'luminance'])
    if not isinstance(modes, list) or len(modes) == 0:
        modes = ['histogram', 'luminance']

    top = op(path)
    if top is None:
        return {'error': f'Node not found: {path}'}
    if not top.isTOP:
        return {'error': f'Node is not a TOP: {path} (type: {top.type})'}

    try:
        np = sys.modules.get('numpy')
        if np is None:
            import importlib
            np = importlib.import_module('numpy')
    except Exception as exc:
        return {'error': f'numpy not available: {str(exc)}'}

    try:
        arr = top.numpyArray()
    except Exception as exc:
        return {'error': f'Could not get pixel data: {str(exc)}'}

    if arr is None:
        return {'error': f'numpyArray() returned None for: {path}'}

    # Ensure 3D array — grayscale TOPs may return 2D (H, W) without channel axis
    if arr.ndim == 2:
        arr = arr[:, :, None]

    h, w = arr.shape[:2]
    num_channels = arr.shape[2]

    if h == 0 or w == 0:
        return {'error': f'Image has zero pixels ({w}x{h})', 'path': path}

    mode_results = {}

    for mode in modes:
        try:
            if mode == 'histogram':
                rgb = arr[:, :, :3] if num_channels >= 3 else arr[:, :, :1]
                bins = 16
                hists = {}
                chan_names = ['r', 'g', 'b'] if num_channels >= 3 else ['l']
                for i, name in enumerate(chan_names):
                    channel = rgb[:, :, i].flatten()
                    counts, edges = np.histogram(channel, bins=bins, range=(0.0, 1.0))
                    hists[name] = {
                        'counts': counts.tolist(),
                        'edges': [round(float(e), 4) for e in edges.tolist()],
                    }
                mode_results['histogram'] = {'bins': bins, 'channels': hists}

            elif mode == 'luminance':
                if num_channels >= 3:
                    lum = (0.2126 * arr[:, :, 0]
                           + 0.7152 * arr[:, :, 1]
                           + 0.0722 * arr[:, :, 2])
                else:
                    lum = arr[:, :, 0]
                mode_results['luminance'] = {
                    'mean': float(np.mean(lum)),
                    'min': float(np.min(lum)),
                    'max': float(np.max(lum)),
                    'std': float(np.std(lum)),
                    'p5': float(np.percentile(lum, 5)),
                    'p95': float(np.percentile(lum, 95)),
                }

            elif mode == 'alpha_coverage':
                if num_channels >= 4:
                    alpha = arr[:, :, 3]
                    total_pixels = h * w
                    above_half = int(np.sum(alpha > 0.5))
                    mode_results['alpha_coverage'] = {
                        'mean_alpha': float(np.mean(alpha)),
                        'opaque_fraction': above_half / float(total_pixels) if total_pixels > 0 else 0.0,
                        'fully_transparent_fraction': float(np.mean(alpha < 0.01)),
                    }
                else:
                    mode_results['alpha_coverage'] = {'error': f'No alpha channel (channels={num_channels})'}

            elif mode == 'color_dominant':
                if num_channels >= 3:
                    rgb_flat = arr[:, :, :3].reshape(-1, 3)
                    # quantize to 4-bit per channel (16 levels) then find mode
                    quantized = (rgb_flat * 15).astype(np.uint8)
                    # Use np.unique instead of pure-Python loop (orders of magnitude faster)
                    unique_colors, color_counts = np.unique(quantized, axis=0, return_counts=True)
                    best_idx = int(np.argmax(color_counts))
                    dominant_q = unique_colors[best_idx]
                    dominant = [round(float(c) / 15.0, 3) for c in dominant_q]
                    best_count = int(color_counts[best_idx])
                    mode_results['color_dominant'] = {
                        'rgb': dominant,
                        'hex': f'#{int(dominant[0] * 255):02x}{int(dominant[1] * 255):02x}{int(dominant[2] * 255):02x}',
                        'pixel_count': best_count,
                        'fraction': round(best_count / float(h * w), 4) if h * w > 0 else 0.0,
                    }
                else:
                    mode_results['color_dominant'] = {'error': 'Need at least 3 channels for color_dominant'}

            elif mode == 'roi_diff':
                roi = body.get('roi')
                reference_path = body.get('reference_path')
                if roi is None or reference_path is None:
                    mode_results['roi_diff'] = {
                        'error': 'roi_diff requires roi=[x,y,w,h] and reference_path in request body'
                    }
                else:
                    rx, ry, rw, rh = int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])
                    # Clamp to image bounds
                    rx = max(0, min(rx, w - 1))
                    ry = max(0, min(ry, h - 1))
                    rw = max(1, min(rw, w - rx))
                    rh = max(1, min(rh, h - ry))

                    ref_top = op(reference_path)
                    if ref_top is None:
                        mode_results['roi_diff'] = {'error': f'Reference node not found: {reference_path}'}
                    elif not ref_top.isTOP:
                        mode_results['roi_diff'] = {'error': f'Reference is not a TOP: {reference_path}'}
                    else:
                        ref_arr = ref_top.numpyArray()
                        if ref_arr is None:
                            mode_results['roi_diff'] = {'error': 'Reference numpyArray() returned None'}
                        elif ref_arr.ndim == 2:
                            ref_arr = ref_arr[:, :, None]
                        if ref_arr is not None and ref_arr.ndim >= 3:
                            patch_a = arr[ry:ry + rh, rx:rx + rw, :3].astype(float)
                            ref_h, ref_w = ref_arr.shape[:2]
                            ref_rx = max(0, min(rx, ref_w - 1))
                            ref_ry = max(0, min(ry, ref_h - 1))
                            ref_rw = max(1, min(rw, ref_w - ref_rx))
                            ref_rh = max(1, min(rh, ref_h - ref_ry))
                            patch_b = ref_arr[ref_ry:ref_ry + ref_rh, ref_rx:ref_rx + ref_rw, :3].astype(float)
                            # Crop both patches to their common overlap size
                            if patch_a.shape != patch_b.shape:
                                min_h = min(patch_a.shape[0], patch_b.shape[0])
                                min_w = min(patch_a.shape[1], patch_b.shape[1])
                                patch_a = patch_a[:min_h, :min_w, :]
                                patch_b = patch_b[:min_h, :min_w, :]
                            diff = patch_a - patch_b
                            mode_results['roi_diff'] = {
                                'roi': [rx, ry, rw, rh],
                                'reference_path': reference_path,
                                'mean_abs_diff': float(np.mean(np.abs(diff))),
                                'max_abs_diff': float(np.max(np.abs(diff))),
                                'rmse': float(np.sqrt(np.mean(diff ** 2))),
                            }

            else:
                mode_results[mode] = {'error': f'Unknown mode: {mode}'}

        except Exception as exc:
            mode_results[mode] = {'error': f'Mode {mode} failed: {str(exc)}'}

    return {
        'path': path,
        'resolution': [w, h],
        'channels': num_channels,
        'modes': mode_results,
    }
