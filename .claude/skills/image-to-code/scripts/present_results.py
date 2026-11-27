#!/usr/bin/env python3
"""Generate chat-friendly side-by-side evidence for an image-to-code run.

Does not invent render evidence: all input images must already exist. Source images
are scaled uniformly, never stretched, masked, or cropped implicitly. If an
original desktop screenshot and matching render have different dimensions,
comparison is visibly flagged as non-equivalent and is not proof of fidelity.
"""

import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps

BACKGROUND = '#eef0f3'
PANEL = '#ffffff'
INK = '#171b22'
MUTED = '#5d6673'
WARNING = '#b3402a'


def font(size, bold=False):
    paths = ([
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf'
    ] if bold else [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'
    ])
    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def load(path):
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f'Imagem não encontrada: {src}')
    with Image.open(src) as im:
        return ImageOps.exif_transpose(im).convert('RGB')


def kind_from_image(image):
    ratio = image.width / image.height
    if ratio >= 1.28:
        return 'desktop'
    if ratio <= .78:
        return 'mobile'
    return 'tablet'


def layout(columns, output, heading, note=None, max_height=1500):
    """columns: (label, PIL image, panel width); returns geometry metadata."""
    padding = 22
    gap = 18
    title_h = 83 if note else 64
    label_h = 48
    canvas_w = padding * 2 + sum(pw for _, _, pw in columns) + gap * (len(columns) - 1)
    fitted = []
    for label, im, pw in columns:
        # Uniformly scale down large screenshots and use only positive scale.
        scale = min((pw - 24) / im.width, max_height / im.height)
        sz = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
        fitted.append((label, im.resize(sz, Image.Resampling.LANCZOS), pw, scale))
    max_img_h = max(im.height for _, im, _, _ in fitted)
    height = title_h + label_h + max_img_h + padding * 2
    canvas = Image.new('RGB', (canvas_w, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    draw.text((padding, 15), heading, font=font(27, bold=True), fill=INK)
    if note:
        draw.text((padding, 52), note, font=font(17), fill=WARNING)
    x = padding
    meta = []
    for label, im, pw, scale in fitted:
        img_y = title_h + label_h
        draw.rounded_rectangle((x, title_h, x + pw - 1, height - padding), radius=10, fill=PANEL)
        draw.text((x + 12, title_h + 10), label, font=font(22, bold=True), fill=INK)
        img_x = x + (pw - im.width) // 2
        canvas.paste(im, (img_x, img_y))
        meta.append({'label': label, 'x': img_x, 'y': img_y, 'rendered_width': im.width, 'rendered_height': im.height, 'scale': round(scale, 5)})
        x += pw + gap
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, optimize=True)
    return {'file': output.name, 'canvas_size': list(canvas.size), 'panels': meta}


def main():
    ap = argparse.ArgumentParser(description='Produz pranchas de demonstração sem cortes ou distorção.')
    ap.add_argument('--mobile', required=True, help='Captura real da versão mobile')
    ap.add_argument('--tablet', required=True, help='Captura real da versão tablet')
    ap.add_argument('--desktop', required=True, help='Captura real da versão desktop')
    ap.add_argument('--reference', help='Screenshot original fornecido pelo usuário, se houver')
    ap.add_argument('--desktop-reference-render', help='Captura desktop com viewport igual à referência; padrão: --desktop')
    ap.add_argument('--reference-kind', choices=['auto', 'desktop', 'tablet', 'mobile', 'other'], default='auto')
    ap.add_argument('--reference-crop', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'), help='Corte EXPLÍCITO da área de UI da referência, registrado no manifesto')
    ap.add_argument('--out', default='artifacts/presentation', help='Diretório das pranchas')
    ap.add_argument('--require-matching-desktop', action='store_true', help='Falhar se a referência desktop e a reconstrução não tiverem dimensões idênticas')
    args = ap.parse_args()
    mobile, tablet, desktop = load(args.mobile), load(args.tablet), load(args.desktop)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {'version': '1.1.0', 'source': { 'mobile': args.mobile, 'tablet': args.tablet, 'desktop': args.desktop }, 'captures': {}, 'outputs': [], 'warnings': []}
    duo = layout([
        (f'MOBILE  ·  {mobile.width} px', mobile, 530),
        (f'TABLET  ·  {tablet.width} px', tablet, 820)
    ], out / 'mobile-tablet-side-by-side.png', 'MOBILE  +  TABLET')
    report['outputs'].append(duo)
    report['captures']['mobile'] = list(mobile.size)
    report['captures']['tablet'] = list(tablet.size)
    report['captures']['desktop'] = list(desktop.size)
    original = load(args.reference) if args.reference else None
    crop = None
    if args.reference_crop:
        if original is None:
            ap.error('--reference-crop exige --reference')
        x, y, w, h = args.reference_crop
        if x < 0 or y < 0 or w < 1 or h < 1 or x + w > original.width or y + h > original.height:
            ap.error('Corte explícito fora dos limites da referência')
        crop = [x, y, w, h]
        original = original.crop((x, y, x + w, y + h))
    ref_kind = args.reference_kind if args.reference_kind != 'auto' else (kind_from_image(original) if original else 'none')
    report['reference'] = { 'path': args.reference, 'kind': ref_kind, 'explicit_crop_xywh': crop, 'size_after_crop': list(original.size) if original else None }
    if ref_kind == 'desktop' and original:
        comparison = load(args.desktop_reference_render) if args.desktop_reference_render else desktop
        dimensions_match = original.size == comparison.size
        note = None if dimensions_match else 'ATENÇÃO: VIEWPORTS DIFERENTES — NÃO É COMPARAÇÃO DE FIDELIDADE'
        if not dimensions_match:
            report['warnings'].append('Referência desktop e render têm dimensões diferentes. Capture a UI no mesmo tamanho antes de avaliar fidelidade.')
        result = layout([
            (f'ORIGINAL  ·  {original.width} × {original.height}', original, 1060),
            (f'RECONSTRUÇÃO  ·  {comparison.width} × {comparison.height}', comparison, 1060)
        ], out / 'desktop-original-vs-render.png', 'COMPARAÇÃO DESKTOP', note=note)
        report['desktop_reference_viewport_match'] = dimensions_match
        report['desktop_comparison_eligible_for_visual_audit'] = dimensions_match
    else:
        result = layout([('DESKTOP RECONSTRUÍDO', desktop, 1380)], out / 'desktop-preview.png', 'DESKTOP')
        report['desktop_reference_viewport_match'] = None
        report['desktop_comparison_eligible_for_visual_audit'] = False
        if ref_kind != 'none':
            report['warnings'].append('Referência não é desktop. Exibir desktop separadamente; comparar também o breakpoint de origem se houver render correspondente.')
    report['outputs'].append(result)
    target = out / 'presentation-report.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'outputs': [str(out / p['file']) for p in report['outputs']], 'report': str(target), 'warnings': report['warnings']}, ensure_ascii=False, indent=2))
    if args.require_matching_desktop and ref_kind == 'desktop' and not report['desktop_reference_viewport_match']:
        raise SystemExit('BLOCKED: desktop screenshots must match before fidelity scoring')


if __name__ == '__main__':
    main()
