"""Score every downloaded listing photo so the reel uses the strongest frames, not the first ones per room.
Cheap pass on all photos: sharpness (Laplacian variance), exposure (mean luma, clipped fraction), colour distance from the set median,
orientation (landscape favoured for 16:9), resolution. Depth-axis score is added for the shortlist once depth maps exist.
Score 0–100; components kept so the UI can explain each pick."""
import numpy as np,cv2
from pathlib import Path
def _feat(path):
    im=cv2.imread(str(path))
    if im is None:return None
    h,w=im.shape[:2];small=cv2.resize(im,(480,int(480*h/w))) if w>480 else im
    g=cv2.cvtColor(small,cv2.COLOR_BGR2GRAY)
    sharp=float(cv2.Laplacian(g,cv2.CV_64F).var())
    luma=float(g.mean());clip=float(((g<8).mean()+(g>247).mean()))
    rgb=cv2.resize(im,(1,1),interpolation=cv2.INTER_AREA)[0,0][::-1].astype(float)
    return {'w':w,'h':h,'sharp':sharp,'luma':luma,'clip':clip,'rgb':rgb}
def score_all(paths,aspect='16:9'):
    feats={Path(p).name:_feat(p) for p in paths};feats={k:v for k,v in feats.items() if v}
    if not feats:return {}
    med=np.median(np.array([v['rgb'] for v in feats.values()]),axis=0);sh=np.array([v['sharp'] for v in feats.values()]);s_hi=float(np.percentile(sh,90)) or 1.0
    out={}
    for k,v in feats.items():
        s_sharp=min(1.0,v['sharp']/s_hi)                                   # relative to the set's sharpest
        s_exp=1.0-min(1.0,abs(v['luma']-128)/90)-min(0.5,v['clip']*4)        # mid-tones, little clipping
        s_col=1.0-min(1.0,float(np.linalg.norm(v['rgb']-med))/80)           # same shoot / light as the rest
        land=v['w']>=v['h'];s_or=1.0 if (land==(aspect!='9:16')) else 0.55
        s_res=min(1.0,min(v['w'],v['h'])/900)
        base=100*(0.30*s_sharp+0.25*max(0,s_exp)+0.20*s_col+0.15*s_or+0.10*s_res)
        out[k]={'score':round(base,1),'sharp':round(v['sharp']),'luma':round(v['luma']),'clip':round(v['clip'],3),'colour':round(s_col,2),'orientation':'landscape' if land else 'portrait','w':v['w'],'h':v['h']}
    return out
def add_depth(scores,depth_dir,weight=25):
    """Blend in the depth-axis score (0..1) for photos that have a depth map."""
    from app.aimotion import _depth_axis
    for k,v in scores.items():
        dp=Path(depth_dir)/(Path(k).stem+'.png')
        if dp.exists():
            d=_depth_axis(dp);v['depth']=round(d,2);v['score']=round(v['score']*(100-weight)/100+weight*d,1)
    return scores
