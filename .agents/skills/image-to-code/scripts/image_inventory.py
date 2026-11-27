#!/usr/bin/env python3
"""Read image dimensions and a candidate palette. No font/asset classification."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageOps


def inventory(image_path: Path, max_colors: int):
    original = image_path.read_bytes()
    with Image.open(image_path) as src:
        exif_orientation = src.getexif().get(274)
        im = ImageOps.exif_transpose(src)
        width, height = im.size
        rgb = im.convert('RGB')
        rgb.thumbnail((512, 512), Image.Resampling.LANCZOS)
        palette = rgb.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
        colors = palette.getcolors(rgb.width * rgb.height) or []
        lut = palette.getpalette()
        total = rgb.width * rgb.height
        candidates = []
        for count, idx in sorted(colors, reverse=True)[:max_colors]:
            channel = lut[idx * 3:idx * 3 + 3]
            candidates.append({'hex': '#%02X%02X%02X' % tuple(channel), 'fraction': round(count / total, 4)})
        return {
            'image': str(image_path), 'sha256': hashlib.sha256(original).hexdigest(),
            'width_px': width, 'height_px': height, 'mode': im.mode,
            'exif_orientation': exif_orientation, 'palette_candidates': candidates,
            'warning': 'Colors are samples only. Layer boundaries, fonts, icons, and design intent require visual analysis.'
        }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('image', type=Path)
    p.add_argument('--out', type=Path)
    p.add_argument('--max-colors', type=int, default=10)
    args = p.parse_args()
    if not (2 <= args.max_colors <= 64):
        p.error('--max-colors must be between 2 and 64')
    result = inventory(args.image, args.max_colors)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + '\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
