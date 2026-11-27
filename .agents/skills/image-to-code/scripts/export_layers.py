#!/usr/bin/env python3
"""Crop explicitly selected regions from a screenshot; optionally remove flat border-connected color."""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import re
from PIL import Image, ImageOps


def remove_border_background(image: Image.Image, tolerance: int):
    """Flood-fill similar pixels connected to the crop boundary; not general-purpose segmentation."""
    im = image.convert('RGBA')
    px = im.load()
    w, h = im.size
    if not w or not h:
        return im
    background = px[0, 0][:3]
    def close(x, y):
        r, g, b, _ = px[x, y]
        return max(abs(r - background[0]), abs(g - background[1]), abs(b - background[2])) <= tolerance
    checked = bytearray(w * h)
    queue = deque()
    for x in range(w):
        queue.append((x, 0)); queue.append((x, h - 1))
    for y in range(h):
        queue.append((0, y)); queue.append((w - 1, y))
    while queue:
        x, y = queue.popleft()
        k = y * w + x
        if checked[k]:
            continue
        checked[k] = 1
        if not close(x, y):
            continue
        px[x, y] = (px[x, y][0], px[x, y][1], px[x, y][2], 0)
        if x > 0: queue.append((x - 1, y))
        if x + 1 < w: queue.append((x + 1, y))
        if y > 0: queue.append((x, y - 1))
        if y + 1 < h: queue.append((x, y + 1))
    return im


def export(image_path: Path, regions_path: Path, out_dir: Path):
    doc = json.loads(regions_path.read_text(encoding='utf-8'))
    regions = doc['regions']
    if not isinstance(regions, list):
        raise ValueError('regions must be an array')
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    seen = set()
    with Image.open(image_path) as opened:
        src = ImageOps.exif_transpose(opened).convert('RGBA')
        for region in regions:
            ident = region['id']
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', ident) or ident in seen:
                raise ValueError(f'Unsafe or duplicated region id: {ident!r}')
            seen.add(ident)
            x, y, w, h = region['bounds']
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y, w, h)) or min(x, y) < 0 or min(w, h) <= 0 or x+w > src.width or y+h > src.height:
                raise ValueError(f'Invalid/out-of-bounds XYWH for {ident}: {region["bounds"]}')
            im = src.crop((x, y, x + w, y + h))
            remove = region.get('remove_border_background', False)
            if remove:
                tolerance = region.get('tolerance', 8)
                if type(tolerance) is not int or not 0 <= tolerance <= 50:
                    raise ValueError('tolerance must be integer 0..50')
                im = remove_border_background(im, tolerance)
            destination = out_dir / f'{ident}.png'
            im.save(destination, optimize=True)
            records.append({
                'id': ident, 'kind': region.get('kind', 'image'),
                'source_bounds_xywh': [x, y, w, h], 'width_px': w, 'height_px': h,
                'output': str(destination), 'removed_border_background': bool(remove),
                'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                'status': 'extracted',
                'caveat': 'Contains only visible screenshot pixels; verify alpha and hidden content.'
            })
    manifest = {'source': str(image_path), 'assets': records}
    (out_dir / 'crop-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('image', type=Path)
    p.add_argument('--regions', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(export(a.image, a.regions, a.out), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
