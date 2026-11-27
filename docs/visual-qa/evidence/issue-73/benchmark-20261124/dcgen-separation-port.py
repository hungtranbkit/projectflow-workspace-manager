"""Independent minimal port of the paper's row/column separator detector.
No WebPAI/DCGen source is imported or copied. Model/code-generation stages are omitted.
"""
from PIL import Image
import numpy as np, json, sys

VAR_THR=50.0
DIFF_THR=45.0
DIFF_PORTION=.9
WINDOW=50
MAX_DEPTH=2

def gray_crop(a):
    return (0.299*a[:,:,0]+0.587*a[:,:,1]+0.114*a[:,:,2]).astype(np.float32)

def find_lines(img):
    """Find low-variance bands bordered by a major brightness transition."""
    g=gray_crop(img)
    h,w=g.shape
    found=[]
    for i in range(WINDOW+1,h):
        window=g[i-WINDOW:i]
        if float(np.var(window)) >= VAR_THR: continue
        upper=g[i-WINDOW-1]
        first=window[0]
        lower=g[i]
        last=window[-1]
        top=float(np.mean(np.abs(upper-first)>DIFF_THR))>.9
        bottom=float(np.mean(np.abs(lower-last)>DIFF_THR))>.9
        if top or bottom:
            found.append(i if bottom else i-WINDOW)
    # Explicit single-row separator detection, as described alongside the
    # sliding-window detector in the paper.
    for i in range(1,h):
        if float(np.var(g[i])) < VAR_THR and float(np.mean(np.abs(g[i]-g[i-1])>DIFF_THR)) > DIFF_PORTION:
            found.append(i)
    # Merge near-identical edges caused by the sliding window.
    merged=[]
    for pos in found:
        if not merged or pos-merged[-1][-1]>WINDOW//2: merged.append([pos])
        else: merged[-1].append(pos)
    return [round(sum(group)/len(group)) for group in merged]

def recurse(img,x,y,depth=0,axis='horizontal'):
    h,w=img.shape[:2]
    if depth>=MAX_DEPTH: return [{'x':x,'y':y,'width':w,'height':h,'depth':depth,'leaf':True}]
    scan=img if axis=='horizontal' else np.transpose(img,(1,0,2))
    lines=find_lines(scan)
    extent=h if axis=='horizontal' else w
    candidates=[p for p in lines if min(p,extent-p)>=80]
    if not candidates:
        return [{'x':x,'y':y,'width':w,'height':h,'depth':depth,'leaf':True}]
    # use the most central detected separator to keep this minimal port deterministic
    cut=min(candidates,key=lambda p:abs(p-extent/2))
    if axis=='horizontal':
        first=img[:cut,:,:]; second=img[cut:,:,:]
        return recurse(first,x,y,depth+1,'vertical')+recurse(second,x,y+cut,depth+1,'vertical')
    first=img[:,:cut,:]; second=img[:,cut:,:]
    return recurse(first,x,y,depth+1,'horizontal')+recurse(second,x+cut,y,depth+1,'horizontal')

for path in sys.argv[1:]:
    a=np.asarray(Image.open(path).convert('RGB'))
    print(json.dumps({'source':Path(path).name,'image':{'width':a.shape[1],'height':a.shape[0]},'params':{'varThr':VAR_THR,'diffThr':DIFF_THR,'diffPortion':DIFF_PORTION,'window':WINDOW,'depth':MAX_DEPTH},'segments':recurse(a,0,0)}))
