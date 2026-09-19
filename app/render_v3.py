#!/usr/bin/env python3
"""render_v3 — tutorial-style walkthrough (ref: youtube ReASV_e1mwc): bright, full-bleed 9:16, continuous forward camera,
zoom-through transitions that carry the camera into the next photo, kinetic centred captions with one accent word.
No letterbox, no dark gradient bars, no cards. Optional Higgsfield/Kling/Seedance clip per scene replaces the synthetic move.
usage: render_v3.py manifest.json out.mp4 [--workers 6]
manifest: {aspect:'9:16'|'16:9', depth_dir, scene_seconds, scenes:[{image,caption,accent?,clip?}], overlays:{title,subtitle,trust,review,cta,by}}"""
import json,sys,os,math,subprocess,wave,struct,argparse,glob,concurrent.futures as cf
import numpy as np,cv2,platform
def VCODEC(bitrate):
    return ['-c:v','h264_videotoolbox','-b:v',bitrate] if platform.system()=='Darwin' else ['-c:v','libx264','-preset','veryfast','-crf','20']
from PIL import Image,ImageDraw,ImageFont,ImageFilter
ap=argparse.ArgumentParser();ap.add_argument('manifest');ap.add_argument('output');ap.add_argument('--workers',type=int,default=6);A=ap.parse_args()
M=json.load(open(A.manifest));OUT=os.path.abspath(A.output);WORK=os.path.splitext(OUT)[0]+'-render';os.makedirs(WORK,exist_ok=True)
W,H=(1080,1920) if M.get('aspect','9:16')=='9:16' else (1920,1080);FPS=30;SD=float(M.get('scene_seconds',4.5));TR=float(M.get('transition_seconds',0.9));DEPTH=M.get('depth_dir','depth')
WHITE=(255,255,255);ACC=tuple(M.get('accent_rgb',[255,232,88]))   # tutorial-style yellow accent word
def _find_font(pats):
    for pat in pats:
        for d in [os.path.join(os.path.dirname(os.path.abspath(__file__)),'fonts'),os.path.expanduser('~/Library/Fonts'),'/Library/Fonts','/System/Library/Fonts/Supplemental','/usr/share/fonts/truetype/dejavu']:
            h=sorted(glob.glob(os.path.join(d,pat)))
            if h:return h[0]
F_BOLD=_find_font(['CanvaSans-Bold.*','Inter-Bold.ttf','Montserrat-Bold.ttf']) or '/System/Library/Fonts/Supplemental/Arial Bold.ttf'
F_MED=_find_font(['CanvaSans-Medium.*','Inter-Medium.ttf','Montserrat-Medium.ttf']) or '/System/Library/Fonts/Supplemental/Arial.ttf'
def font(p,s):
    try:return ImageFont.truetype(p,s)
    except Exception:
        try:return ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',s)
        except Exception:return ImageFont.load_default(s)
def ease(t):t=min(max(t,0),1);return t*t*(3-2*t)
def ease_out(t):t=min(max(t,0),1);return 1-(1-t)**3
def clamp(t):return min(max(t,0),1)
# ---------- plates ----------
class Plate:
    def __init__(s,img):
        im=cv2.imread(img);h,w=im.shape[:2];ov=1.5;tw,th=int(W*ov),int(H*ov)   # big margin: we zoom to 1.45 in transitions
        sc=max(tw/w,th/h);im=cv2.resize(im,(int(w*sc)+1,int(h*sc)+1),interpolation=cv2.INTER_AREA);y0=(im.shape[0]-th)//2;x0=(im.shape[1]-tw)//2;im=im[y0:y0+th,x0:x0+tw]
        dp=os.path.join(DEPTH,os.path.splitext(os.path.basename(img))[0]+'.png');d=cv2.imread(dp,cv2.IMREAD_UNCHANGED)
        if d is None:d=np.full((h,w),32768,np.uint16)
        d=cv2.resize(d,(int(w*sc)+1,int(h*sc)+1))[y0:y0+th,x0:x0+tw].astype(np.float32)/65535.0;d=cv2.GaussianBlur(d,(0,0),8)
        s.im=im;s.tw,s.th=tw,th;ys,xs=np.mgrid[0:H,0:W].astype(np.float32);s.gx=xs-W/2;s.gy=ys-H/2;s.dd=cv2.resize(d,(W,H))-0.5
    def frame(s,z,ax=0,ay=0,px=0,py=0):
        mx=s.tw/2+s.gx/z+s.dd*ax+px;my=s.th/2+s.gy/z+s.dd*ay+py
        return cv2.remap(s.im,mx,my,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT)
class ClipPlate:
    def __init__(s,path):s.cap=cv2.VideoCapture(path);s.last=None
    def frame(s,*a):
        ok,f=s.cap.read()
        if not ok:return s.last if s.last is not None else np.zeros((H,W,3),np.uint8)
        h,w=f.shape[:2];sc=max(W/w,H/h);f=cv2.resize(f,(int(w*sc)+1,int(h*sc)+1));y0=(f.shape[0]-H)//2;x0=(f.shape[1]-W)//2;s.last=f[y0:y0+H,x0:x0+W].copy();return s.last
def walk(i,t,n):
    """Continuous forward walk: slow dolly-in with parallax, gentle handheld sway, alternating slight turn left/right."""
    p=t/max(n-1,1);turn=(1 if i%2==0 else -1)
    z=1.0+0.10*p;ax=turn*(10+22*p);ay=6*p;px=turn*(-12+24*ease(p))+3*math.sin(t*0.9);py=2*math.sin(t*0.7)
    return z,ax,ay,px,py
def grade(fr):
    f=fr.astype(np.float32);f=(f-128)*1.05+128;hsv=cv2.cvtColor(np.clip(f,0,255).astype(np.uint8),cv2.COLOR_BGR2HSV).astype(np.float32);hsv[...,1]*=1.08;hsv[...,2]=np.clip(hsv[...,2]*1.03,0,255)
    return cv2.cvtColor(np.clip(hsv,0,255).astype(np.uint8),cv2.COLOR_HSV2BGR)
def zoomblur(fr,amt):
    """cheap radial zoom blur: average a few scaled copies"""
    if amt<=0:return fr
    acc=fr.astype(np.float32);k=4
    for j in range(1,k+1):
        z=1+amt*j/k;m=cv2.getRotationMatrix2D((W/2,H/2),0,z);acc+=cv2.warpAffine(fr,m,(W,H),borderMode=cv2.BORDER_REFLECT).astype(np.float32)
    return (acc/(k+1)).astype(np.uint8)
# ---------- text ----------
def comp(fr,L,alpha=1.0,dy=0,scale=1.0):
    if alpha<=0:return fr
    im=L
    if scale!=1.0:
        im=L.resize((max(1,int(W*scale)),max(1,int(H*scale))),Image.LANCZOS);c=Image.new('RGBA',(W,H),(0,0,0,0));c.paste(im,((W-im.width)//2,(H-im.height)//2));im=c
    a=np.asarray(im,dtype=np.float32)
    if dy:a=np.roll(a,int(dy),axis=0)
    al=(a[...,3:4]/255.0)*alpha;rgb=a[...,:3][...,::-1];return (fr.astype(np.float32)*(1-al)+rgb*al).astype(np.uint8)
def caption_layers(text,accent=None,size=None,y=None,maxw=None):
    """Bold centred lines; the accent word (or last word) in yellow. Returns list of per-word RGBA layers (with soft shadow) for staggered pops."""
    size=size or int(W*0.085);f=font(F_BOLD,size);maxw=maxw or int(W*0.86);words=text.split();lines=[];cur=[]
    d=ImageDraw.Draw(Image.new('RGBA',(1,1)))
    for w in words:
        if d.textlength(' '.join(cur+[w]),font=f)>maxw and cur:lines.append(cur);cur=[w]
        else:cur.append(w)
    if cur:lines.append(cur)
    lh=int(size*1.18);y0=(y if y is not None else H*0.5-len(lines)*lh/2);layers=[];acc=(accent or words[-1]).lower().strip('.,!')
    for li,ln in enumerate(lines):
        x=(W-d.textlength(' '.join(ln),font=f))/2
        for w in ln:
            L=Image.new('RGBA',(W,H),(0,0,0,0));sh=Image.new('RGBA',(W,H),(0,0,0,0));ds=ImageDraw.Draw(sh);ds.text((x+3,y0+li*lh+5),w,font=f,fill=(0,0,0,170));sh=sh.filter(ImageFilter.GaussianBlur(6))
            dl=ImageDraw.Draw(L);dl.text((x,y0+li*lh),w,font=f,fill=(ACC if w.lower().strip('.,!')==acc else WHITE)+(255,))
            layers.append(Image.alpha_composite(sh,L));x+=d.textlength(w+' ',font=f)
    return layers
def small_layer(text,size=None,y=None,color=WHITE,spaced=True):
    size=size or int(W*0.03);f=font(F_MED,size);t=('  '.join(text.upper()) if spaced and len(text)<26 else text.upper());L=Image.new('RGBA',(W,H),(0,0,0,0));d=ImageDraw.Draw(L)
    w=d.textlength(t,font=f);x=(W-w)/2;yy=y if y is not None else H*0.5+int(W*0.06)
    sh=Image.new('RGBA',(W,H),(0,0,0,0));ImageDraw.Draw(sh).text((x+2,yy+3),t,font=f,fill=(0,0,0,160));sh=sh.filter(ImageFilter.GaussianBlur(5));d.text((x,yy),t,font=f,fill=color+(255,));return Image.alpha_composite(sh,L)
def pill_layer(text,y):
    f=font(F_BOLD,int(W*0.032));L=Image.new('RGBA',(W,H),(0,0,0,0));d=ImageDraw.Draw(L);w=d.textlength(text,font=f)+int(W*0.08);x=(W-w)/2;h=int(W*0.075)
    d.rounded_rectangle((x,y,x+w,y+h),radius=h//2,fill=ACC+(255,));d.text((x+int(W*0.04),y+int(h*0.22)),text,font=f,fill=(20,20,20,255));return L
def overlay_plan(i,n):
    """Which overlay plays on scene i (kinetic captions, tutorial-style). All optional."""
    O=M.get('overlays',{});plan=[]
    if i==0 and O.get('title'):plan.append(('title',O['title'],O.get('subtitle')))
    elif i==min(2,n-1) and O.get('trust'):plan.append(('trust',O['trust'],None))
    elif i==max(1,int(n*0.65)) and O.get('review'):plan.append(('review',O['review'],O.get('review_by')))
    elif i==n-1 and O.get('cta'):plan.append(('cta',O['cta'],O.get('by')))
    return plan
def draw_overlay(fr,kind,a,b,t,dur):
    fo=1.0 if t<dur-0.5 else clamp((dur-t)/0.5)
    if kind=='title':
        for k,L in enumerate(caption_layers(a,size=int(W*0.095),y=H*0.40)):
            p=ease_out(clamp((t-0.25-0.12*k)/0.45));fr=comp(fr,L,p*fo,dy=(1-p)*40)
        if b:p=ease(clamp((t-1.2)/0.6));fr=comp(fr,small_layer(b,y=H*0.40+int(W*0.095*1.18*(1+a.count(' ')//3))+int(W*0.03)),p*fo,dy=(1-p)*14)
    elif kind=='trust':
        for k,L in enumerate(caption_layers(a,accent=a.split()[0],size=int(W*0.075),y=H*0.44)):
            p=ease_out(clamp((t-0.2-0.1*k)/0.4));fr=comp(fr,L,p*fo,dy=(1-p)*30,scale=1.0)
    elif kind=='review':
        for k,L in enumerate(caption_layers('“'+a+'”',accent='__none__',size=int(W*0.058),y=H*0.40)):
            p=ease_out(clamp((t-0.15-0.05*k)/0.35));fr=comp(fr,L,p*fo,dy=(1-p)*20)
        if b:p=ease(clamp((t-1.6)/0.5));fr=comp(fr,small_layer('— '+b,spaced=False,y=H*0.40+int(W*0.058*1.18*3.2)),p*fo)
    elif kind=='cta':
        for k,L in enumerate(caption_layers(a,size=int(W*0.09),y=H*0.40)):
            p=ease_out(clamp((t-0.2-0.12*k)/0.45));fr=comp(fr,L,p*fo,dy=(1-p)*40)
        p=ease_out(clamp((t-1.1)/0.5));fr=comp(fr,pill_layer(M.get('overlays',{}).get('cta_pill','BOOK ON AIRBNB'),H*0.40+int(W*0.09*1.18*2)+int(W*0.02)),p*fo,dy=(1-p)*16)
        if b:p=ease(clamp((t-1.8)/0.6));fr=comp(fr,small_layer(b,spaced=False,size=int(W*0.036),y=H*0.86,color=ACC),p*fo)
    return fr
def scene_caption(fr,cap,accent,t,dur):
    """Short room caption: pops in at 0.3 s, holds, leaves before the transition."""
    if not cap:return fr
    tr=TR if TR>0 else 0.0;fo=1.0 if t<dur-tr-0.3 else clamp((dur-tr-t)/0.3)
    for k,L in enumerate(caption_layers(cap,accent=accent,size=int(W*0.07),y=H*0.72)):
        p=ease_out(clamp((t-0.3-0.1*k)/0.4));fr=comp(fr,L,p*fo,dy=(1-p)*26)
    return fr
# ---------- scene = hold + zoom-through into next ----------
def seg_scene(i):
    S=M['scenes'];s=S[i];nxt=S[(i+1)] if i+1<len(S) else None;sd=float(s.get('seconds') or SD);n=int(sd*FPS);hold=n-int(TR*FPS) if (nxt and TR>0) else n
    A=ClipPlate(s['clip']) if s.get('clip') and os.path.exists(s['clip']) else Plate(s['image']);B=Plate(nxt['image']) if nxt else None
    ov=overlay_plan(i,len(S));frames=[]
    for f in range(n):
        t=f/FPS
        if f<hold or not nxt or TR<=0:
            z,ax,ay,px,py=walk(i,t,hold);fr=A.frame(z,ax,ay,px,py)
        else:
            p=(f-hold)/max(n-hold-1,1);e=ease(p)
            za,ax,ay,px,py=walk(i,hold/FPS,hold);fa=A.frame(za*(1+0.45*e),ax,ay,px,py);fa=zoomblur(fa,0.10*e)
            fb=B.frame(1.45-0.45*e,0,0,0,0);fb=zoomblur(fb,0.10*(1-e))
            mix=ease(clamp((p-0.3)/0.4));fr=(fa.astype(np.float32)*(1-mix)+fb.astype(np.float32)*mix).astype(np.uint8)
        fr=grade(fr)
        if f<hold and not ov:fr=scene_caption(fr,s.get('caption'),s.get('accent'),t,sd)
        for kind,a,b in ov:fr=draw_overlay(fr,kind,a,b,t,sd-(TR if (nxt and TR>0) else 0))
        if i==0 and t<0.5:fr=(fr*(t/0.5)).astype(np.uint8)
        if not nxt and t>sd-0.8:fr=(fr*clamp((sd-t)/0.8)).astype(np.uint8)
        frames.append(fr)
    path=os.path.join(WORK,f's{i:02d}.mp4');p=subprocess.Popen(['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s',f'{W}x{H}','-r',str(FPS),'-i','-',*VCODEC('16M'),'-pix_fmt','yuv420p',path],stdin=subprocess.PIPE)
    for fr in frames:p.stdin.write(fr.tobytes())
    p.stdin.close();p.wait();return i
if __name__=='__main__':
    n=len(M['scenes'])
    with cf.ProcessPoolExecutor(A.workers) as ex:
        for i in ex.map(seg_scene,range(n)):print('scene',i,flush=True)
    (lambda f:f.write(''.join(f"file 's{i:02d}.mp4'\n" for i in range(n))))(open(os.path.join(WORK,'list.txt'),'w'))
    total=sum(float(x.get('seconds') or SD) for x in M['scenes']);rate=24000;chords=[(130.81,164.81,196),(146.83,174.61,220),(110,130.81,164.81),(98,123.47,146.83)]
    with wave.open(os.path.join(WORK,'score.wav'),'w') as w:   # original, bright, mid-tempo pad + soft pulse
        w.setparams((1,2,rate,0,'NONE','not compressed'));buf=bytearray()
        for k in range(int(total*rate)):
            t=k/rate;ch=chords[int(t/max(SD,1))%4];fade=min(1,t/1.5,(total-t)/2);v=sum(math.sin(2*math.pi*f*t)*0.05 for f in ch)+sum(math.sin(2*math.pi*f*2*t)*0.02 for f in ch)
            beat=t%0.5;v+=math.sin(2*math.pi*60*t)*math.exp(-beat*10)*0.09;arp=t%0.25;v+=math.sin(2*math.pi*ch[int(t*4)%3]*4*t)*math.exp(-arp*12)*0.04
            buf.extend(struct.pack('<h',int(max(-1,min(1,v*fade))*32767)))
        w.writeframes(buf)
    subprocess.run(['ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',os.path.join(WORK,'list.txt'),'-i',os.path.join(WORK,'score.wav'),'-c:v','copy','-c:a','aac','-b:a','160k','-shortest','-movflags','+faststart',OUT],check=True)
    if W<H:   # tutorial-style 16:9 presentation: portrait clip over its own blurred fill
        wide=os.path.splitext(OUT)[0]+'-16x9.mp4'
        subprocess.run(['ffmpeg','-y','-v','error','-i',OUT,'-filter_complex',"[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=30,eq=brightness=-0.05[bg];[0:v]scale=-2:1080[fg];[bg][fg]overlay=(W-w)/2:0,format=yuv420p[v]",'-map','[v]','-map','0:a',*VCODEC('12M'),'-c:a','copy','-movflags','+faststart',wide],check=True)
    print(OUT,f'{total:.1f}s')
