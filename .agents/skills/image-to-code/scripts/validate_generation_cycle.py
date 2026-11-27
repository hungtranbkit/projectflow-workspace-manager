#!/usr/bin/env python3
"""Validate evidence for screenshot -> reference-conditioned scene -> typography/code.

The script checks deliverables/provenance and review records. It cannot know whether
an external image model matched the source visually. Human/image-model review must
actually inspect the pictures; do not treat this script as proof of pixel fidelity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

REVIEWS = (
    'subject_identity', 'anatomy_paws', 'integrated_lighting',
    'composition', 'scene_continuity', 'text_and_ui_absence',
)


def validate(contract: dict, root: Path) -> list[str]:
    failures: list[str] = []
    root = root.resolve()

    def get_file(raw: object, label: str) -> Path | None:
        if not isinstance(raw, str) or not raw.strip():
            failures.append(f'{label}: missing file path')
            return None
        p = Path(raw)
        if p.is_absolute() or '..' in p.parts:
            failures.append(f'{label}: unsafe relative path')
            return None
        full = (root / p).resolve()
        if not full.is_relative_to(root) or not full.is_file():
            failures.append(f'{label}: missing or escaping file {raw}')
            return None
        return full

    if contract.get('workflow') != 'reference_guided_two_stage_generation':
        failures.append('workflow must be reference_guided_two_stage_generation')

    reference = contract.get('reference', {})
    if not isinstance(reference, dict):
        reference = {}
    full_ref = get_file(reference.get('full_screenshot'), 'reference.full_screenshot')
    crop = get_file(reference.get('component_crop'), 'reference.component_crop')
    box = reference.get('crop_box')
    if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, int) and v >= 0 for v in box) or (isinstance(box, list) and len(box) == 4 and (box[2] < 1 or box[3] < 1)):
        failures.append('reference.crop_box must be [x,y,width,height] with positive dimensions')
    if reference.get('crop_usage') != 'generation_reference_only':
        failures.append('component crop must only be a generation reference, not the final asset')

    scene = contract.get('scene', {})
    if not isinstance(scene, dict):
        scene = {}
    prompt = get_file(scene.get('prompt_path'), 'scene.prompt_path')
    output = get_file(scene.get('generated_image'), 'scene.generated_image')
    if scene.get('origin') != 'image_generation_with_reference':
        failures.append('scene.origin must confirm image generation with a visual reference')
    if scene.get('text_free') is not True:
        failures.append('first-stage scene must be text_free=true')
    if scene.get('baked_lighting') is not True or scene.get('duplicate_css_glow') is not False:
        failures.append('native lighting required: baked_lighting=true and duplicate_css_glow=false')
    refs = scene.get('conditioned_on', [])
    if not isinstance(refs, list) or not all(isinstance(x, str) for x in refs) or not {reference.get('full_screenshot'), reference.get('component_crop')}.issubset(refs):
        failures.append('scene.conditioned_on must include full screenshot and component crop')
    if prompt:
        body = prompt.read_text(encoding='utf-8')
        if len(body) < 450:
            failures.append('scene prompt is too short for a faithful structured description')
        terms = ['identity', 'anatomy', 'lighting', 'composition', 'exclusion']
        missing = [term for term in terms if term not in body.lower()]
        if missing:
            failures.append('scene prompt missing explicit sections: '+', '.join(missing))
    if output:
        if full_ref and hashlib.sha256(output.read_bytes()).digest() == hashlib.sha256(full_ref.read_bytes()).digest():
            failures.append('generated image byte-identical to full reference screenshot')
        if crop and hashlib.sha256(output.read_bytes()).digest() == hashlib.sha256(crop.read_bytes()).digest():
            failures.append('generated image byte-identical to reference crop (cropping is not generation)')
        if Image is not None:
            try:
                with Image.open(output) as im:
                    im.verify()
                with Image.open(output) as im:
                    if min(im.size) < 256:
                        failures.append(f'generated image too small: {im.size}')
            except Exception as exc:
                failures.append(f'generated image cannot be decoded: {exc}')
    attempts = contract.get('attempts', [])
    if not isinstance(attempts, list) or not attempts:
        failures.append('generation attempts must be recorded')
    else:
        for i, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                failures.append(f'attempts[{i}] invalid'); continue
            get_file(attempt.get('generated_image'), f'attempts[{i}].generated_image')
            if attempt.get('status') not in ('accepted','rejected'):
                failures.append(f'attempts[{i}].status must be accepted or rejected')
            if attempt.get('status') == 'rejected' and not attempt.get('issues'):
                failures.append(f'attempts[{i}] missing rejection notes')
        if isinstance(attempts[-1], dict) and (attempts[-1].get('status') != 'accepted' or attempts[-1].get('generated_image') != scene.get('generated_image')):
            failures.append('last attempt must accept the scene.generated_image')

    reviews = contract.get('reviews', {})
    if not isinstance(reviews, dict):
        reviews = {}
    for role in REVIEWS:
        r = reviews.get(role)
        if not isinstance(r, dict):
            failures.append(f'reviews.{role} missing'); continue
        if r.get('status') != 'pass':
            failures.append(f'reviews.{role}: non-pass status')
        if not isinstance(r.get('notes'), str) or len(r['notes'].strip()) < 20:
            failures.append(f'reviews.{role}: insufficient explanation')
        evidence = r.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            failures.append(f'reviews.{role}: evidence list missing')
        else:
            for i, item in enumerate(evidence):
                get_file(item, f'reviews.{role}.evidence[{i}]')

    typography = contract.get('typography_stage', {})
    if not isinstance(typography, dict):
        typography = {}
    mode = typography.get('mode')
    if mode not in ('live_html_css_overlay', 'second_image_edit_pass'):
        failures.append('typography_stage.mode must be live_html_css_overlay or second_image_edit_pass')
    get_file(typography.get('spec_path'), 'typography_stage.spec_path')
    get_file(typography.get('final_render'), 'typography_stage.final_render')
    if not isinstance(typography.get('exact_copy'), list) or not typography['exact_copy'] or not all(isinstance(t, str) and t.strip() for t in typography['exact_copy']):
        failures.append('typography_stage.exact_copy must document the original text')
    if typography.get('preserved_scene') is not True:
        failures.append('typography stage must preserve generated scene without redesign')
    if typography.get('layout_checked') is not True:
        failures.append('typography_stage.layout_checked must be true after geometric comparison')

    return failures


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--file', required=True)
    p.add_argument('--root', required=True)
    args = p.parse_args()
    errors = validate(json.loads(Path(args.file).read_text(encoding='utf-8')), Path(args.root))
    print(json.dumps({'approved': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
