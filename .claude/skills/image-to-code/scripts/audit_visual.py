#!/usr/bin/env python3
"""Generate pixel difference evidence between equal-size reference and render; never call it a quality score."""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageChops, ImageEnhance, ImageDraw, ImageStat


def compare(reference: Path, rendered: Path, out_dir: Path, threshold: int = 20, masks_path: Path | None = None):
    if not 0 <= threshold <= 255:
        raise ValueError('threshold must be 0..255')
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(reference) as r1, Image.open(rendered) as r2:
        ref = r1.convert('RGB')
        result = r2.convert('RGB')
    if ref.size != result.size:
        raise ValueError(f'Image dimensions differ (reference={ref.size}, rendered={result.size}); capture at matching viewport/DPR.')
    w, h = ref.size
    valid = Image.new('L', (w, h), color=255)
    excluded = []
    if masks_path:
        doc = json.loads(masks_path.read_text(encoding='utf-8'))
        regions = doc['exclude']
        drawer = ImageDraw.Draw(valid)
        for rect in regions:
            x, y, rw, rh = [int(rect[k]) for k in ('x', 'y', 'width', 'height')]
            if x < 0 or y < 0 or rw <= 0 or rh <= 0 or x+rw > w or y+rh > h:
                raise ValueError(f'Invalid mask: {rect}')
            drawer.rectangle((x, y, x+rw-1, y+rh-1), fill=0)
            excluded.append(rect)
    diff = ImageChops.difference(ref, result)
    red, green, blue = diff.split()
    max_diff = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    binary = max_diff.point(lambda v: 255 if v > threshold else 0)
    mismatched = ImageChops.multiply(binary, valid).histogram()[255]
    valid_px = valid.histogram()[255]
    if not valid_px:
        raise ValueError('All pixels are masked out')
    mean = ImageStat.Stat(diff, mask=valid).mean
    pixel_mae = sum(mean) / 3
    # Masked pixels must not appear as defects in any exported diagnostic image.
    overlay = Image.blend(ref, result, alpha=0.5)
    diff_contrast = ImageEnhance.Contrast(diff).enhance(3)
    neutral = Image.new('RGB', (w, h), '#bcbcbc')
    overlay = Image.composite(overlay, neutral, valid)
    diff_contrast = Image.composite(diff_contrast, neutral, valid)
    binary = ImageChops.multiply(binary, valid)
    overlay.save(out_dir / 'overlay.png')
    diff_contrast.save(out_dir / 'diff.png')
    binary.save(out_dir / 'threshold-diff.png')
    valid.save(out_dir / 'valid-pixel-mask.png')
    report = {
        'reference': str(reference), 'rendered': str(rendered),
        'width_px': w, 'height_px': h, 'threshold_channel_value': threshold,
        'valid_pixels': valid_px, 'excluded_pixels': w*h - valid_px,
        'mismatch_pixels': mismatched, 'mismatch_fraction': round(mismatched / valid_px, 6),
        'mean_absolute_error_rgb_0_to_255': round(pixel_mae, 3),
        'excluded_regions': excluded,
        'overlay': str(out_dir / 'overlay.png'), 'diff': str(out_dir / 'diff.png'),
        'threshold_diff': str(out_dir / 'threshold-diff.png'), 'mask_visualization': str(out_dir / 'valid-pixel-mask.png'),
        'note': 'Diagnostic pixel metric; not a fidelity score. Fonts, DPR and browser rasterization can change pixels.'
    }
    (out_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference', type=Path, required=True)
    p.add_argument('--render', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--threshold', type=int, default=20)
    p.add_argument('--masks', type=Path, help='JSON {"exclude":[{"x":0,"y":0,"width":40,"height":20}]}')
    a = p.parse_args()
    print(json.dumps(compare(a.reference, a.render, a.out, a.threshold, a.masks), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
