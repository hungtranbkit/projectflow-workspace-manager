#!/usr/bin/env python3
"""Byte-verified font provenance + metadata. No font binaries enter generated reports.

When the original font file isn't available, identity remains UNVERIFIED even if a
font-family name, kerning, and apparent text wrap all match.
"""
import argparse
from hashlib import sha256
from pathlib import Path
import json
import sys


def get_names(font):
    names={}
    for code,label in [(1,'family'),(2,'subfamily'),(4,'full_name'),(6,'postscript')]:
        found=[]
        for rec in font['name'].names:
            if rec.nameID==code:
                try: name=rec.toUnicode()
                except Exception: continue
                if name not in found:found.append(name)
        names[label]=sorted(found)
    return names


def inspect_file(path):
    from fontTools.ttLib import TTFont
    b=path.read_bytes()
    obj=TTFont(str(path),lazy=True)
    try:
        names=get_names(obj)
        glyphs=obj.getBestCmap() or {}
        return {'sha256':sha256(b).hexdigest(),'file_bytes':len(b),'names':names,'units_per_em':obj['head'].unitsPerEm,
                'weight_class':obj['OS/2'].usWeightClass if 'OS/2' in obj else None,
                'width_class':obj['OS/2'].usWidthClass if 'OS/2' in obj else None,
                'glyph_count':len(glyphs),'italic':bool(obj['head'].macStyle & 0b10)}
    finally:obj.close()


def audit(manifest,root):
    records=[]
    for item in manifest.get('fonts',[]):
        def fullpath(key):
            rel=item.get(key)
            if not rel:return None
            p=(root/rel).resolve()
            if not p.is_relative_to(root.resolve()):raise ValueError('Font path outside workspace')
            return p
        source,candidate=fullpath('approved_original'),fullpath('candidate')
        rec={'id':item['id'],'source_available':bool(source and source.is_file()),'candidate_available':bool(candidate and candidate.is_file()),'status':None}
        if source and source.is_file():rec['original']=inspect_file(source)
        if candidate and candidate.is_file():rec['candidate']=inspect_file(candidate)
        declared=item.get('original_sha256')
        if declared:
            rec['declared_hash_match']=bool(rec.get('original',rec.get('candidate',{})).get('sha256')==declared)
        if rec['source_available'] and rec['candidate_available']:
            rec['status']='IDENTICAL_BYTES' if rec['original']['sha256']==rec['candidate']['sha256'] else 'DIFFERENT_BYTES'
        else:rec['status']='ORIGINAL_UNAVAILABLE' if not rec['source_available'] else 'CANDIDATE_UNAVAILABLE'
        if declared and rec['candidate_available'] and rec['candidate']['sha256']==declared:
            rec['status']='IDENTICAL_TO_APPROVED_SHA256'
        elif declared and rec['candidate_available'] and rec['candidate']['sha256']!=declared:
            rec['status']='HASH_MISMATCH'
        # no source or hash: metadata matching is insufficient
        records.append(rec)
    return {'schema_version':1,'verified':bool(records) and all(x['status'] in ('IDENTICAL_BYTES','IDENTICAL_TO_APPROVED_SHA256') for x in records),
            'fonts':records,'notice':'Requires licensed source bytes or owner-confirmed SHA-256; CSS fallback checks are supplementary. No font files are exported.'}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest',type=Path,required=True)
    ap.add_argument('--root',type=Path,default=Path.cwd())
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--strict',action='store_true')
    args=ap.parse_args()
    result=audit(json.loads(args.manifest.read_text()),args.root.resolve())
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'verified':result['verified'],'status':{r['id']:r['status'] for r in result['fonts']},'report':str(args.out)},ensure_ascii=False))
    if args.strict and not result['verified']:sys.exit(2)

if __name__=='__main__':main()