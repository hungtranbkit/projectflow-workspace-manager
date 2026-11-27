#!/usr/bin/env python3
"""Create an exact crop *for use as image-generation reference*, never a final page asset.

Example: python scripts/prepare_generation_reference.py source.png --box 0 0 390 475 \
  --out artifacts/reference/hero-component.png --manifest artifacts/reference/hero-crop.json
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from PIL import Image


def exact_crop(src: Path, out: Path, box: tuple[int, int, int, int]) -> dict:
    if len(box)!=4 or any(not isinstance(v,int) for v in box):
        raise ValueError('box must contain four integers [x,y,width,height]')
    x,y,w,h=box
    if x<0 or y<0 or w<1 or h<1:
        raise ValueError('invalid crop position/dimensions')
    with Image.open(src) as original:
        size=original.size
        if x+w>size[0] or y+h>size[1]:
            raise ValueError(f'crop exceeds source bounds {size}')
        cropped=original.crop((x,y,x+w,y+h))
        out.parent.mkdir(parents=True,exist_ok=True)
        cropped.save(out)
    return {
       'full_screenshot':str(src.resolve()),
       'component_crop':str(out.resolve()),
       'crop_box':[x,y,w,h],
       'source_dimensions':list(size),
       'crop_dimensions':[w,h],
       'original_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),
       'crop_sha256':hashlib.sha256(out.read_bytes()).hexdigest(),
       'crop_usage':'generation_reference_only',
       'warning':'This crop is a visual conditioning input only: do not ship it or claim it recovers hidden pixels.'
    }


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source')
    p.add_argument('--box',nargs=4,type=int,required=True,metavar=('X','Y','W','H'))
    p.add_argument('--out',required=True)
    p.add_argument('--manifest',required=True)
    a=p.parse_args()
    try:
        entry=exact_crop(Path(a.source),Path(a.out),tuple(a.box))
        target=Path(a.manifest)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(entry,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(entry,ensure_ascii=False,indent=2))
    except Exception as error:
        print(f'error: {error}',file=sys.stderr)
        sys.exit(1)


if __name__=='__main__':main()
