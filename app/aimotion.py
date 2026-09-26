"""AI camera motion per the property-video-ai method (audit → author → generate → assemble).
- audit: is this photo set video-worthy? (count, single-shoot colour consistency, depth-axis frames)
- author: labelled prompt blocks per shot with the four tested moves (DOLLY / AXIS-LOCK / CRANE / ORBIT) + FIDELITY + NEGATIVE
- generate: Higgsfield API `bytedance/seedance-2.0/image-to-video` (official SDK), 5 s, single start frame, no audio
- profile/trim: frame-difference motion profile → steady window, frozen/dying-tail flags; reversal check for orbits
Costs observed 19 Aug 2026 (skill): Seedance 2.0 5 s 720p 22.5 cr, 1080p 45 cr. Always show the estimate before spending."""
import os,re,json,math,subprocess,shutil
from pathlib import Path
import numpy as np,cv2
MODEL='bytedance/seedance-2.0/image-to-video'
FIDELITY=("FIDELITY (critical, do not break): stay faithful to the reference photo. Preserve the exact architecture, layout, furniture, "
          "materials, colours and proportions shown. Do NOT add, remove, move or invent rooms, furniture, windows, doors or objects. "
          "Keep every straight line straight — no warping or bending of walls, doorframes, counters or windows.")
NEGATIVE="NEGATIVE: No people, no human figures, no reflections of a person or camera crew in glass. No text, no logos, no watermark."
ANTIWALK=("No handheld feel, no footstep bounce, no vertical bob, no side-to-side rocking, no operator sway. The camera never steps.")
MOVES={
 'DOLLY':"MOTION FEEL: mechanical dolly motion on a straight steel track — perfectly level, constant velocity, rigid and machine-driven. "+ANTIWALK+" It rolls.",
 'AXIS-LOCK':"MOTION FEEL: the camera has exactly one degree of freedom — forward translation. It does not rotate, tilt, pan, roll, orbit, arc or drift sideways. Its height and heading never change. Constant velocity, covering real ground — a purposeful travelling shot, not a slow zoom and not a static frame. "+ANTIWALK,
 'CRANE':"MOTION FEEL: a pure vertical lift at constant speed on a rigid crane. No forward, backward or lateral movement, no rotation, no roll, no sway. The camera only goes up. "+ANTIWALK,
 'ORBIT':"MOTION FEEL: one unbroken mechanical arc at constant angular speed on a motorised circular track. Fixed radius, fixed height, perfectly level. The camera is bolted to the rig and machine-driven. "+ANTIWALK+" It sweeps.",
}
# ---------------- audit ----------------
def _mean_rgb(path):
    im=cv2.imread(str(path));return None if im is None else cv2.resize(im,(1,1),interpolation=cv2.INTER_AREA)[0,0][::-1].astype(float)
def _depth_axis(depth_png):
    """How much of a depth axis a frame has: spread of the depth map (near vs far) and gradient strength. 0..1."""
    d=cv2.imread(str(depth_png),cv2.IMREAD_UNCHANGED)
    if d is None:return 0.0
    d=d.astype(np.float32)/65535.0;p5,p95=np.percentile(d,5),np.percentile(d,95);spread=float(p95-p5)
    gy,gx=np.gradient(cv2.resize(d,(160,90)));grad=float(np.mean(np.abs(gx))+np.mean(np.abs(gy)))*10
    return float(min(1.0,0.7*spread+0.3*min(1.0,grad)))
def audit(photos,depth_dir=None,rooms=None):
    """photos: list of paths (the candidate frames). Returns verdict PASS / WEAK / REJECT with reasons and per-photo scores."""
    photos=[Path(p) for p in photos];rooms=rooms or {}
    rgb=[(p,_mean_rgb(p)) for p in photos];rgb=[(p,v) for p,v in rgb if v is not None]
    arr=np.array([v for _,v in rgb]) if rgb else np.zeros((0,3));spread=float(np.mean(np.std(arr,axis=0))) if len(arr)>1 else 0.0
    depth=[]
    for p in photos:
        dp=Path(depth_dir)/(p.stem+'.png') if depth_dir else None;depth.append((p.name,_depth_axis(dp) if dp and dp.exists() else None))
    strong=[n for n,s in depth if s is not None and s>=0.45]
    reasons=[];score=100
    n=len(photos)
    if n<6:reasons.append(f'only {n} usable photos (need ~6 frames with a depth axis)');score-=40
    if spread>28:reasons.append(f'photos look like more than one shoot (mean-RGB spread {spread:.0f})');score-=25
    elif spread>18:reasons.append(f'colour consistency is borderline (spread {spread:.0f})');score-=10
    kinds=set(rooms.values()) if rooms else set()
    if rooms and not ({'living','kitchen'}&kinds):reasons.append('no wide living space in the set');score-=25
    if depth and len(strong)<max(3,n//2):reasons.append(f'only {len(strong)} of {n} frames have a strong depth axis');score-=20
    verdict='PASS' if score>=70 else 'WEAK' if score>=45 else 'REJECT'
    return {'verdict':verdict,'score':max(0,score),'photos':n,'rgb_spread':round(spread,1),'depth_scores':{k:(round(v,2) if v is not None else None) for k,v in depth},'strong_depth':strong,'reasons':reasons or ['single shoot, daylight, enough depth frames']}
# ---------------- author ----------------
def choose_move(room,idx,total):
    room=(room or 'other').lower()
    if idx==total-1 and room in ('garden','spa','exterior','view'):return 'CRANE'
    if room in ('entrance','hallway','corridor'):return 'AXIS-LOCK'
    if room in ('kitchen',):return 'ORBIT' if idx%2==0 else 'DOLLY'
    if room in ('living','bedroom','bathroom','garden','spa','exterior','view','other'):return 'DOLLY' if idx%3 else 'AXIS-LOCK'
    return 'DOLLY'
def author(room,move,facts,caption=''):
    """Labelled blocks. `facts` may include amenities, city; name real objects where we know them (room labels, amenities)."""
    room=(room or 'space').replace('_',' ');am=[a for a in (facts.get('amenities') or []) if len(a)<24][:3]
    objects={'kitchen':'the island, the worktop and the cabinetry','living':'the sofa, the coffee table and the far windows','bedroom':'the bed and the far window',
             'bathroom':'the bath, the basin and the tiled wall','garden':'the seating, the planting and the far boundary','spa':'the hot tub and the decking',
             'exterior':'the façade, the driveway and the front door','view':'the balcony rail and the horizon','entrance':'the hallway and the doorway at its end'}.get(room.split()[0],'the furniture and the far wall')
    if move=='ORBIT':
        shot=(f"SHOT: ONE single clean half-orbit around the main feature of the {room} ({objects.split(',')[0]}) — a continuous 180-degree arc in one direction only. "
              "It never reverses, never doubles back, never returns the way it came. The final frame must not resemble the opening frame.")
    elif move=='CRANE':shot=f"SHOT: from eye level in the {room}, a pure vertical lift revealing the ceiling and the space above {objects}, finishing high with clean space at the top of frame."
    elif move=='AXIS-LOCK':shot=f"SHOT: starting at the near end of the {room}, travelling straight forward past {objects}, continuing all the way to the bright far end of the space."
    else:shot=f"SHOT: a slow push forward into the {room}, past {objects}, arriving close to the far end of the room."
    look=f"LOOK: natural daylight exactly as in the photo, neutral clean grade, real-estate photography look{(' · '+', '.join(am)) if am else ''}."
    return '\n'.join([shot,MOVES[move],FIDELITY,look,NEGATIVE])
# ---------------- generate ----------------
def generate(image_path,prompt,out_path,resolution='1080p',seconds=5,aspect='16:9',cb=None):
    """Returns (ok, info). Uses the official SDK; never retries a submission whose outcome is unknown."""
    import httpx,higgsfield_client as hf
    from higgsfield_client import Failed,NSFW,Cancelled
    bad={'s':None}
    def upd(st):
        if isinstance(st,(Failed,NSFW,Cancelled)):bad['s']=type(st).__name__
    try:
        url=hf.upload_file(str(image_path))
        res=hf.subscribe(MODEL,arguments={'image_url':url,'prompt':prompt,'duration':int(seconds),'resolution':resolution,'aspect_ratio':aspect,'generate_audio':False},on_queue_update=upd)
    except Exception as e:return False,f'{type(e).__name__}: {str(e)[:140]}'
    if bad['s']:return False,f'request ended {bad["s"]}'
    v=(res or {}).get('video');vurl=v.get('url') if isinstance(v,dict) else v
    if not vurl:return False,'no video url in result'
    Path(out_path).write_bytes(httpx.get(vurl,timeout=300).content);return True,{'url':vurl}
# ---------------- profile / trim ----------------
def profile(clip,window=3.0):
    """Frame-difference motion per 0.1 s (mean abs luma diff, 0-255 scale) → best steady window, frozen and dying-tail flags."""
    cap=cv2.VideoCapture(str(clip));fps=cap.get(cv2.CAP_PROP_FPS) or 30;prev=None;vals=[];t=0;i=0;first=None;dist=[]
    while True:
        ok,f=cap.read()
        if not ok:break
        g=cv2.cvtColor(cv2.resize(f,(320,180)),cv2.COLOR_BGR2GRAY).astype(np.float32)
        if first is None:first=g
        if prev is not None:vals.append((i/fps,float(np.mean(np.abs(g-prev)))));dist.append((i/fps,float(np.sqrt(np.mean((g-first)**2)))))
        prev=g;i+=1
    cap.release()
    if len(vals)<10:return {'error':'too short'}
    tt=np.array([v[0] for v in vals]);v=np.array([x[1] for x in vals]);k=max(3,int(fps*0.25));sm=np.convolve(v,np.ones(k)/k,mode='same');dur=float(tt[-1])
    best=None
    for s in np.arange(0,max(0.01,dur-window),0.05):
        m=sm[(tt>=s)&(tt<s+window)].mean() if ((tt>=s)&(tt<s+window)).any() else 0
        if best is None or m>best[0]:best=(m,float(s))
    tail=sm[tt>dur-0.8].mean() if (tt>dur-0.8).any() else 0;mid=sm[(tt>1)&(tt<dur-1)].mean() if ((tt>1)&(tt<dur-1)).any() else sm.mean()
    d=np.array([x[1] for x in dist]);dd=np.diff(d);neg_run=0;max_neg=0
    for x in dd:
        neg_run=neg_run+1 if x<-0.05 else 0;max_neg=max(max_neg,neg_run)
    return {'duration':round(dur,2),'mean_motion':round(float(sm.mean()),2),'best_window':[round(best[1],2),round(best[1]+window,2)],'best_window_motion':round(float(best[0]),2),
            'frozen':bool(sm.mean()<1.0),'dying_tail':bool(tail<0.5*mid),'reversal_frames':int(max_neg),'reverses':bool(max_neg>=int(fps*0.5))}
def trim(clip,start,end,out):
    import platform
    codec=['-c:v','h264_videotoolbox','-b:v','12M'] if platform.system()=='Darwin' else ['-c:v','libx264','-crf','16','-preset','veryfast']
    subprocess.run(['ffmpeg','-y','-v','error','-ss',f'{start:.2f}','-to',f'{end:.2f}','-i',str(clip),'-an',*codec,'-pix_fmt','yuv420p',str(out)],check=True);return out
def plan(scenes,facts,resolution='1080p'):
    """Author a shot per scene; returns list of dicts with move+prompt and the total credit estimate."""
    out=[]
    for i,s in enumerate(scenes):
        mv=choose_move(s.get('room'),i,len(scenes));out.append({'index':i,'room':s.get('room'),'move':mv,'prompt':author(s.get('room'),mv,facts,s.get('caption') or s.get('title') or '')})
    return {'shots':out,'resolution':resolution,'model':MODEL}
