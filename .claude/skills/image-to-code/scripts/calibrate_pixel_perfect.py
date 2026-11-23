#!/usr/bin/env python3
"""Evidence-first, multi-state visual comparison for image-to-code.

Works with one reference per state, or one horizontal triptych via --columns.
Reference geometry is never silently changed; --normalize-reference must be explicit.
SSIM and MAE are diagnostic metrics, never a quality score or proof of likeness.
"""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageChops, ImageStat, ImageDraw, ImageFont


def crop_panels(image, columns):
    if columns < 1 or image.width % columns:
        raise ValueError('The reference width must be divisible by --columns; crop it explicitly first.')
    w = image.width // columns
    return [image.crop((i * w, 0, (i+1) * w, image.height)) for i in range(columns)]


def ssim_rgb(ref, actual):
    """Optional dependency; no fabricated score when scikit-image is unavailable."""
    try:
        import numpy as np
        from skimage.metrics import structural_similarity
    except ImportError:
        return None
    return float(structural_similarity(np.asarray(ref), np.asarray(actual), data_range=255, channel_axis=2))


def metrics(ref, actual, threshold=20):
    if ref.size != actual.size:
        raise ValueError(f'Mismatch {ref.size} vs {actual.size}; provide the same viewport or --normalize-reference.')
    diff = ImageChops.difference(ref, actual)
    channel_mean = ImageStat.Stat(diff).mean
    px_max = ImageChops.lighter(ImageChops.lighter(*diff.split()[:2]), diff.split()[2])
    mismatch = sum(px_max.histogram()[threshold+1:])
    ss = ssim_rgb(ref, actual)
    return {'ssim': round(ss, 5) if ss is not None else None,
            'mean_absolute_error_rgb': round(sum(channel_mean)/3, 3),
            'mismatch_fraction_over_threshold': round(mismatch/(ref.width*ref.height), 6),
            'threshold_channel_value': threshold}


def compare_states(refs, renders, out, baselines=None, normalize=False, threshold=20):
    if not refs or len(refs) != len(renders):
        raise ValueError('Reference count must equal render count.')
    if baselines and len(baselines) != len(refs):
        raise ValueError('Baseline count must equal reference count.')
    if threshold < 0 or threshold > 255:
        raise ValueError('Threshold must be in 0..255')
    out.mkdir(parents=True, exist_ok=True)
    evidence = []
    for i, (ref, render) in enumerate(zip(refs, renders), start=1):
        if ref.size != render.size and not normalize:
            raise ValueError(f'State {i}: {ref.size} reference != {render.size} render. '
                             'Use matching viewport or explicitly enable --normalize-reference.')
        if ref.size != render.size:
            ref = ref.resize(render.size, Image.Resampling.LANCZOS)
        state = {'state': i, 'reference_size': list(ref.size), 'render_size': list(render.size),
                 'reference_normalized': normalize, 'current': metrics(ref, render, threshold)}
        if baselines:
            base = baselines[i-1]
            if base.size != render.size:
                raise ValueError(f'State {i}: baseline {base.size} != render {render.size}.')
            state['baseline'] = metrics(ref, base, threshold)
            state['change'] = {
                'mae_delta': round(state['current']['mean_absolute_error_rgb'] - state['baseline']['mean_absolute_error_rgb'], 3),
                'ssim_delta': round(state['current']['ssim'] - state['baseline']['ssim'], 5)
                 if state['current']['ssim'] is not None and state['baseline']['ssim'] is not None else None
            }
        # Side-by-side at full resolution; no distortion or nonuniform scaling.
        w, h = render.size
        board = Image.new('RGB', (2*w+24, h+48), '#eff0f3')
        font = ImageFont.load_default()
        d = ImageDraw.Draw(board)
        d.text((12, 15), f'ORIGINAL / STATE {i}', font=font, fill='#171b21')
        d.text((w+24, 15), f'RENDERED / STATE {i}', font=font, fill='#171b21')
        board.paste(ref, (8, 40));board.paste(render, (w+16, 40))
        board.save(out/f'compare-state-{i}.png')
        delta = ImageChops.difference(ref, render)
        # Grayscale heatmap, no misleading gradient scores.
        r, g, b = delta.split()
        heat = ImageChops.lighter(ImageChops.lighter(r, g), b)
        heat.save(out/f'heatmap-state-{i}.png')
        state['compare_image'] = f'compare-state-{i}.png'
        state['heatmap_image'] = f'heatmap-state-{i}.png'
        evidence.append(state)
    summary = {
        'method': 'Pillow RGB MAE, threshold fraction, optional skimage SSIM',
        'normalization': 'reference resized with LANCZOS, only if explicitly requested' if normalize else 'none',
        'states': evidence,
        'average_mae': round(sum(x['current']['mean_absolute_error_rgb'] for x in evidence)/len(evidence), 3),
        'average_ssim': round(sum(x['current']['ssim'] for x in evidence)/len(evidence), 5)
        if all(x['current']['ssim'] is not None for x in evidence) else None,
        'note': 'Neither SSIM nor MAE is a 0-10 visual fidelity rating; review assets, geometry, typography, interactions manually.'
    }
    (out/'calibration-metrics.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    lines = ['# Pixel-perfect calibration', '', f"States compared: **{len(evidence)}**", '',
             '| State | SSIM (diagnostic) | RGB MAE | Mismatch > threshold |',
             '|---|---:|---:|---:|']
    for x in evidence:
        c=x['current']; lines.append(f"| {x['state']} | {c['ssim'] if c['ssim'] is not None else 'not installed'} | {c['mean_absolute_error_rgb']} | {c['mismatch_fraction_over_threshold']:.1%} |")
    lines += ['', 'Review each `compare-state-N.png` and `heatmap-state-N.png` at actual viewport size.',
              'Do not grant an overall 9.5/10 without browser interaction tests, occlusion checks, original asset provenance, typography and responsive QA.',
              'Unsupported/hidden image regions must be marked unrecoverable rather than reconstructed as fact.']
    (out/'calibration-report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference', type=Path, action='append', required=True)
    p.add_argument('--render', type=Path, action='append', required=True)
    p.add_argument('--baseline', type=Path, action='append')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--columns', type=int, default=1, help='Split one horizontal reference into N equal panels')
    p.add_argument('--normalize-reference', action='store_true', help='Explicitly resize reference panels to render dimensions; logged')
    p.add_argument('--threshold', type=int, default=20)
    a=p.parse_args()
    if a.columns != 1 and len(a.reference) != 1:
        p.error('--columns requires one reference image')
    with_images=lambda paths: [Image.open(path).convert('RGB') for path in paths]
    refs=with_images(a.reference)
    if a.columns!=1: refs=crop_panels(refs[0],a.columns)
    res=compare_states(refs,with_images(a.render),a.out,with_images(a.baseline) if a.baseline else None,a.normalize_reference,a.threshold)
    print(json.dumps(res,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
