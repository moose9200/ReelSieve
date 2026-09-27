#!/usr/bin/env python3
"""property-to-generator v2: 2.5D depth-parallax cinematic listing video with animated intro, trust/ratings card,
lower-third scenes, animated review cards and outro. Requires depth maps from depth.py (same stem, .png, near=bright).
usage: render_v2.py manifest.json out.mp4 [--workers 6] [--threads <CPU budget>]
manifest: {brand, intro{eyebrow,title,subtitle,image}, trust{rating,count,five_star_pct,badges[],categories{}}, scenes[{image,title,subtitle}],
           reviews{bg,items[{stars,date,text}]}, outro{eyebrow,title,subtitle,cta,image}, depth_dir, scene_seconds}
The review card never shows a guest's name, even if an older manifest carries one."""
import json,sys,os,math,subprocess,wave,argparse,concurrent.futures as cf
import numpy as np,cv2,platform
def VCODEC(bitrate,threads):   # explicit x264 threads: its auto count follows the host's 48 cores, not the container's quota
    return ['-c:v','h264_videotoolbox','-b:v',bitrate] if platform.system()=='Darwin' else ['-c:v','libx264','-preset','veryfast','-crf','20','-threads',str(threads)]
from PIL import Image,ImageDraw,ImageFont,ImageFilter
ap=argparse.ArgumentParser();ap.add_argument('manifest');ap.add_argument('output');ap.add_argument('--workers',type=int,default=6);ap.add_argument('--threads',type=int,default=os.cpu_count());ap.add_argument('--reuse',action='store_true');ap.add_argument('--force',default='');A=ap.parse_args()
M=json.load(open(A.manifest));OUT=os.path.abspath(A.output);WORK=os.path.splitext(OUT)[0]+'-render';os.makedirs(WORK,exist_ok=True)
W,H=1920,1080;FPS=30;BAR=96;SD=float(M.get('scene_seconds',4.5));XF=0.6;DEPTH=M.get('depth_dir','depth')
GOLD=(217,185,135);WHITE=(255,255,255);INK=(10,16,20)
import glob as _glob
def _find_font(patterns):
    for pat in patterns:
        for d in [os.path.join(os.path.dirname(os.path.abspath(__file__)),'fonts'),os.path.expanduser('~/Library/Fonts'),'/Library/Fonts','/System/Library/Fonts','/System/Library/Fonts/Supplemental','/usr/share/fonts/truetype/dejavu']:
            hits=sorted(_glob.glob(os.path.join(d,pat)))
            if hits:return hits[0]
    return None
# Canva Sans if installed (proprietary), else Inter (free, OFL — closest match), else Montserrat, else Arial.
F_BOLD=_find_font(['CanvaSans-Bold.*','Canva Sans Bold.*','CanvaSans*Bold*','Inter-SemiBold.ttf','Montserrat-SemiBold.ttf','Montserrat-Bold.ttf']) or '/System/Library/Fonts/Supplemental/Arial Bold.ttf'
F_REG=_find_font(['CanvaSans-Regular.*','Canva Sans Regular.*','CanvaSans*Regular*','CanvaSans-Medium.*','Inter-Medium.ttf','Montserrat-Medium.ttf','Montserrat-Regular.ttf']) or '/System/Library/Fonts/Supplemental/Arial.ttf'
F_ARIAL='/System/Library/Fonts/Supplemental/Arial.ttf'
def font(path,size,index=0):
    try:return ImageFont.truetype(path,size,index=index)
    except Exception:
        try:return ImageFont.truetype(F_ARIAL,size)
        except Exception:return ImageFont.load_default(size)
def serif(s):return font(F_BOLD,int(s*0.86))   # titles: Canva Sans Bold (was Didot); slightly smaller for the wider face
def sans(s,bold=False):return font(F_BOLD if bold else F_REG,s)
def ease(t):t=min(max(t,0),1);return t*t*(3-2*t)
def ease_out(t):t=min(max(t,0),1);return 1-(1-t)**3
def clamp(t):return min(max(t,0),1)
# ---------- parallax ----------
class Plate:
    def __init__(s,img,blur=0,dark=0.0):
        im=cv2.imread(img);h,w=im.shape[:2];ov=1.16;tw,th=int(W*ov),int(H*ov)
        sc=max(tw/w,th/h);im=cv2.resize(im,(int(w*sc)+1,int(h*sc)+1),interpolation=cv2.INTER_AREA)
        y0=(im.shape[0]-th)//2;x0=(im.shape[1]-tw)//2;im=im[y0:y0+th,x0:x0+tw]
        dp=os.path.join(DEPTH,os.path.splitext(os.path.basename(img))[0]+'.png');d=cv2.imread(dp,cv2.IMREAD_UNCHANGED)
        if d is None:d=np.full((h,w),32768,np.uint16)
        d=cv2.resize(d,(int(w*sc)+1,int(h*sc)+1))[y0:y0+th,x0:x0+tw].astype(np.float32)/65535.0
        d=cv2.GaussianBlur(d,(0,0),6)
        if blur:im=cv2.GaussianBlur(im,(0,0),blur)
        if dark:im=(im.astype(np.float32)*(1-dark)).astype(np.uint8)
        s.im=im;s.d=d-0.5;s.tw,s.th=tw,th
        ys,xs=np.mgrid[0:H,0:W].astype(np.float32);s.gx=xs-W/2;s.gy=ys-H/2
        # destination-space depth approximation (centre crop of plate depth)
        s.dd=cv2.resize(s.d,(W,H))
    def frame(s,z,ax,ay,px=0,py=0):
        """z: zoom>=1, ax/ay: parallax amplitude px (near moves +), px/py: pan px"""
        mx=s.tw/2+s.gx/z+s.dd*ax+px;my=s.th/2+s.gy/z+s.dd*ay+py
        return cv2.remap(s.im,mx,my,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT)
MOVES=[lambda t:(1.0+0.09*ease(t),-6+34*ease(t),4-8*ease(t),0,0),          # dolly in
       lambda t:(1.06,-30+60*ease(t),6-10*ease(t),-40+80*ease(t),0),         # truck right
       lambda t:(1.10-0.09*ease(t),18-30*ease(t),-8+12*ease(t),0,-10+20*ease(t)),  # pull back
       lambda t:(1.03+0.03*math.sin(math.pi*t),28-56*ease(t),-8+16*ease(t),30-60*ease(t),0)]  # orbit
# ---------- grade ----------
_vig=None
def grade(fr,grain=True):
    global _vig
    if _vig is None:
        ys,xs=np.mgrid[0:H,0:W].astype(np.float32);r=np.sqrt(((xs-W/2)/(W/2))**2+((ys-H/2)/(H/2))**2);_vig=np.clip(1-0.42*np.clip(r-0.35,0,1)**1.7,0,1)[...,None]
    f=fr.astype(np.float32)
    f=(f-128)*1.06+128;f[...,2]*=1.03;f[...,0]*=0.985   # BGR: warm lift
    f*=_vig
    if grain:f+=_RNG.standard_normal((H,W,1),dtype=np.float32)*2.4   # same N(0,2.4) grain; the float32 sampler is several times faster
    return np.clip(f,0,255).astype(np.uint8)
_RNG=np.random.default_rng()
def letterbox(fr):fr[:BAR]=0;fr[H-BAR:]=0;return fr
def comp(fr,layer,alpha=1.0,dy=0):
    """composite RGBA PIL layer onto BGR frame. Blends only the box where the layer has any alpha (text is a small
    part of the frame); outside it alpha is 0, so the pixels are exactly what a full-frame blend gives."""
    if alpha<=0:return fr
    L=np.asarray(layer)
    if dy:L=np.roll(L,int(dy),axis=0)
    out=fr.copy();rows=np.flatnonzero(L[...,3].any(1))
    if not len(rows):return out
    y0,y1=rows[0],rows[-1]+1;cols=np.flatnonzero(L[y0:y1,:,3].any(0));x0,x1=cols[0],cols[-1]+1
    L=L[y0:y1,x0:x1].astype(np.float32);a=(L[...,3:4]/255.0)*alpha;rgb=L[...,:3][...,::-1]
    out[y0:y1,x0:x1]=(fr[y0:y1,x0:x1].astype(np.float32)*(1-a)+rgb*a).astype(np.uint8);return out
def layer():return Image.new('RGBA',(W,H),(0,0,0,0))
def shadow_text(d,xy,txt,f,fill,sh=(0,0,0,140)):
    x,y=xy;d.text((x+2,y+3),txt,font=f,fill=sh);d.text((x,y),txt,font=f,fill=fill)
def wrap(d,txt,f,maxw):
    words=txt.split();lines=[];cur=''
    for w in words:
        t=(cur+' '+w).strip()
        if d.textlength(t,font=f)<=maxw:cur=t
        else:lines.append(cur);cur=w
    if cur:lines.append(cur)
    return lines
def brandmark(d):
    if M.get('brand'):d.text((120,BAR+34),M['brand'],font=sans(20),fill=WHITE+(200,))
def stars_img(n,size,filled=5):
    im=Image.new('RGBA',(int(size*1.2*n),int(size*1.1)),(0,0,0,0));d=ImageDraw.Draw(im)
    for i in range(n):
        cx=i*size*1.2+size/2;cy=size*0.55;pts=[]
        for k in range(10):
            r=size/2 if k%2==0 else size/5;a=-math.pi/2+k*math.pi/5;pts.append((cx+r*math.cos(a),cy+r*math.sin(a)))
        d.polygon(pts,fill=GOLD+(255,) if i<filled else (255,255,255,70))
    return im
def encode(path,frames_iter,n):
    p=subprocess.Popen(['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s',f'{W}x{H}','-r',str(FPS),'-i','-',*VCODEC('14M',max(1,A.threads//A.workers)),'-pix_fmt','yuv420p',path],stdin=subprocess.PIPE)
    for fr in frames_iter:p.stdin.write(fr.tobytes())
    p.stdin.close();p.wait();return path
# ---------- segments ----------
def seg_intro(spec,dur):
    P=Plate(spec['image'],dark=0.35);n=int(dur*FPS)
    title=spec['title'];words=title.split();ft=serif(120);fs=sans(28);fe=sans(22,True)
    base=layer();d=ImageDraw.Draw(base);tw=d.textlength(title,font=ft);x0=(W-tw)/2;y0=H/2-118
    eyeb=layer();d=ImageDraw.Draw(eyeb);t=spec.get('eyebrow','');w=d.textlength(t,font=fe);d.text(((W-w)/2,y0-58),t,font=fe,fill=GOLD+(255,))
    wl=[];x=x0
    for wd in words:
        L=layer();dd=ImageDraw.Draw(L);shadow_text(dd,(x,y0),wd,ft,WHITE+(255,));wl.append(L);x+=d.textlength(wd+' ',font=ft)
    sub=layer();d=ImageDraw.Draw(sub);t='    '.join(spec['subtitle'].upper());w=d.textlength(t,font=fs);d.text(((W-w)/2,y0+178),t,font=fs,fill=WHITE+(230,))
    for i in range(n):
        t=i/FPS;z,ax,ay,px,py=MOVES[0](i/(n-1));fr=P.frame(z,ax,ay,px,py)
        fr=comp(fr,eyeb,ease(t/0.6))
        for k,L in enumerate(wl):
            a=ease((t-0.35-0.22*k)/0.6);fr=comp(fr,L,a,dy=(1-a)*40)
        rl=clamp((t-1.3)/0.7);rw=int(180*ease_out(rl))
        if rw>0:cv2.rectangle(fr,(W//2-rw//2,int(y0+158)),(W//2+rw//2,int(y0+161)),GOLD[::-1],-1)
        fr=comp(fr,sub,ease((t-1.7)/0.8),dy=(1-ease((t-1.7)/0.8))*16)
        fr=grade(fr);fr=letterbox(fr)
        fo=min(1,(dur-t)/0.5) if t>dur-0.5 else 1
        if fo<1:fr=(fr*fo).astype(np.uint8)
        yield fr
def seg_trust(spec,dur,bgimg):
    P=Plate(bgimg,blur=3,dark=0.55);n=int(dur*FPS);rating=spec['rating'];cats=list(spec['categories'].items())
    fbig=serif(210);fmid=sans(30);fsm=sans(24);fcat=sans(26);fnum=sans(26,True)
    static=layer();d=ImageDraw.Draw(static);brandmark(d)
    d.text((150,H/2-190),'GUEST RATING',font=sans(22,True),fill=GOLD+(255,))
    d.text((150,H/2+130),f"{spec['count']} reviews"+(f"  ·  {spec['five_star_pct']}% five-star" if spec.get('five_star_pct') else ''),font=fmid,fill=WHITE+(230,))
    d.text((1060,H/2-190),'RATED BY GUESTS FOR',font=sans(22,True),fill=GOLD+(255,))
    badges=spec.get('badges',[])
    for i in range(n):
        t=i/FPS;z,ax,ay,px,py=MOVES[3](i/(n-1));fr=P.frame(z,ax,ay,px,py);fr=comp(fr,static,ease(t/0.5))
        L=layer();d=ImageDraw.Draw(L)
        # counter
        v=rating*ease_out(clamp((t-0.2)/1.6));s=f'{v:.2f}';shadow_text(d,(140,H/2-150),s,fbig,WHITE+(255,))
        sw=d.textlength(s,font=fbig);L.alpha_composite(stars_img(1,72),(int(150+sw+18),int(H/2-40)))
        # badges pop
        bx=150
        for k,b in enumerate(badges):
            a=ease_out(clamp((t-1.6-0.25*k)/0.5))
            if a<=0:continue
            bw=d.textlength(b,font=fsm)+44;bh=48;sc=0.85+0.15*a;y=H/2+190
            box=(bx,y,bx+bw,y+bh);d.rounded_rectangle(box,radius=24,fill=(255,255,255,int(28*a)),outline=GOLD+(int(220*a),),width=2)
            d.text((bx+22,y+11),b,font=fsm,fill=WHITE+(int(255*a),));bx+=bw+16
        # category bars
        for k,(name,val) in enumerate(cats):
            a=ease_out(clamp((t-0.6-0.18*k)/0.9));y=H/2-130+k*72
            d.text((1060,y),name,font=fcat,fill=WHITE+(int(255*min(1,a*2)),))
            d.rounded_rectangle((1060,y+40,1760,y+46),radius=3,fill=(255,255,255,40))
            wv=700*(val/5.0)*a;d.rounded_rectangle((1060,y+40,1060+wv,y+46),radius=3,fill=GOLD+(255,))
            d.text((1780,y+22),f'{val:.1f}',font=fnum,fill=WHITE+(int(255*a),))
        fr=comp(fr,L);fr=grade(fr);fr=letterbox(fr);yield fr
class ClipPlate:
    """Frames from a generated clip (e.g. Higgsfield Seedance image-to-video), fitted to 1920x1080; holds last frame if short."""
    def __init__(s,path):
        s.cap=cv2.VideoCapture(path);s.last=None
    def frame(s,*a):
        ok,f=s.cap.read()
        if not ok:
            if s.last is None:return np.zeros((H,W,3),np.uint8)
            return s.last
        h,w=f.shape[:2];sc=max(W/w,H/h);f=cv2.resize(f,(int(w*sc)+1,int(h*sc)+1));y0=(f.shape[0]-H)//2;x0=(f.shape[1]-W)//2;s.last=f[y0:y0+H,x0:x0+W].copy();return s.last
def seg_scene(idx,spec,dur):
    P=ClipPlate(spec['clip']) if spec.get('clip') and os.path.exists(spec['clip']) else Plate(spec['image']);n=int(dur*FPS);mv=MOVES[idx%4];ft=serif(84);fs=sans(26);fn=sans(20)
    grad=layer();d=ImageDraw.Draw(grad)
    for y in range(H//2,H):d.line((0,y,W,y),fill=INK+(int(170*((y-H/2)/(H/2))**1.4),))
    brandmark(d);d.text((W-160,H-BAR-56),f'{idx+1:02d}',font=fn,fill=WHITE+(190,))
    tl=layer();d=ImageDraw.Draw(tl);shadow_text(d,(120,H-BAR-250),spec['title'],ft,WHITE+(255,))
    sl=layer();d=ImageDraw.Draw(sl);d.text((122,H-BAR-118),spec['subtitle'].upper(),font=fs,fill=WHITE+(225,))
    for i in range(n):
        t=i/FPS;z,ax,ay,px,py=mv(i/(n-1));fr=P.frame(z,ax,ay,px,py);fr=comp(fr,grad)
        a=ease((t-0.25)/0.7);fr=comp(fr,tl,a,dy=(1-a)*34)
        rw=int(96*ease_out(clamp((t-0.15)/0.6)))
        if rw>0:cv2.rectangle(fr,(120,H-BAR-274),(120+rw,H-BAR-270),GOLD[::-1],-1)
        b=ease((t-0.7)/0.7);fr=comp(fr,sl,b,dy=(1-b)*18)
        fr=grade(fr);fr=letterbox(fr);yield fr
def seg_review(k,rv,dur,bgimg):
    P=Plate(bgimg,blur=2,dark=0.5);n=int(dur*FPS);fq=serif(46);fn=sans(28,True);fd=sans(22);fh=sans(22,True)
    cw,ch=1240,470;cx,cy=(W-cw)//2,(H-ch)//2+10
    static=layer();d=ImageDraw.Draw(static);brandmark(d);d.text((cx,cy-58),'WHAT GUESTS SAY',font=fh,fill=GOLD+(255,))
    d.text((W-cx-d.textlength(f'{k+1} / {M["reviews"]["n"]}',font=fd),cy-56),f'{k+1} / {M["reviews"]["n"]}',font=fd,fill=WHITE+(160,))
    lines=wrap(d,rv['text'],fq,cw-160);full=' '.join(lines);total=len(full)
    for i in range(n):
        t=i/FPS;z,ax,ay,px,py=MOVES[1 if k%2 else 2](i/(n-1));fr=P.frame(z,ax,ay,px,py);fr=comp(fr,static,ease(t/0.5))
        L=layer();d=ImageDraw.Draw(L);a=ease_out(clamp(t/0.7));dy=(1-a)*80
        d.rounded_rectangle((cx,cy+dy,cx+cw,cy+ch+dy),radius=22,fill=(12,20,26,int(215*a)),outline=(255,255,255,int(35*a)),width=1)
        d.text((cx+56,cy+dy+12),'“',font=serif(150),fill=GOLD+(int(255*a),))
        # stars pop
        for s in range(rv['stars']):
            sa=ease_out(clamp((t-0.5-0.12*s)/0.35))
            if sa<=0:continue
            sz=int(34*(0.6+0.4*sa));st=stars_img(1,sz);L.alpha_composite(st,(int(cx+150+s*44+(34-sz)/2),int(cy+dy+64+(34-sz)/2)))
        # typewriter
        shown=int(total*clamp((t-1.0)/2.2));acc=0
        for li,ln in enumerate(lines):
            seg=ln[:max(0,min(len(ln),shown-acc))];acc+=len(ln)+1
            if seg:d.text((cx+80,cy+dy+130+li*60),seg,font=fq,fill=WHITE+(int(255*a),))
        # caret
        if shown<total and t>1.0 and int(t*3)%2==0:
            li=0;acc=0
            for li,ln in enumerate(lines):
                if shown-acc<=len(ln):break
                acc+=len(ln)+1
            cw_=d.textlength(lines[li][:shown-acc],font=fq);d.rectangle((cx+82+cw_,cy+dy+136+li*60,cx+85+cw_,cy+dy+176+li*60),fill=GOLD+(255,))
        na=ease(clamp((t-3.3)/0.6));d.text((cx+80,cy+ch+dy-92),'Guest review',font=fn,fill=WHITE+(int(255*na),))
        if rv.get('date'):d.text((cx+80+d.textlength('Guest review',font=fn)+16,cy+ch+dy-86),f"·  {rv['date']}",font=fd,fill=GOLD+(int(255*na),))
        fr=comp(fr,L);fr=grade(fr);fr=letterbox(fr);yield fr
def seg_outro(spec,dur):
    P=Plate(spec['image'],dark=0.4);n=int(dur*FPS);ft=serif(110);fs=sans(28);fe=sans(22,True);fb=serif(60);fc=sans(26,True)
    y0=H/2-150
    def centred(txt,f,y,fill):
        L=layer();d=ImageDraw.Draw(L);w=d.textlength(txt,font=f);shadow_text(d,((W-w)/2,y),txt,f,fill);return L
    E=centred(spec.get('eyebrow',''),fe,y0-50,GOLD+(255,));T=centred(spec['title'],ft,y0,WHITE+(255,))
    S=centred('    '.join(spec['subtitle'].upper()),fs,y0+186,WHITE+(230,))
    C=layer();d=ImageDraw.Draw(C);cta=spec.get('cta','');w=d.textlength(cta,font=fc)+72;bx=(W-w)/2;by=y0+262
    d.rounded_rectangle((bx,by,bx+w,by+60),radius=30,fill=GOLD+(255,));d.text((bx+36,by+15),cta,font=fc,fill=INK+(255,))
    B=centred(spec.get('by','by Braivex.com'),fb,H-BAR-150,GOLD+(255,))
    Bm=layer();d=ImageDraw.Draw(Bm);brandmark(d)
    for i in range(n):
        t=i/FPS;z,ax,ay,px,py=MOVES[0](i/(n-1));fr=P.frame(z,ax,ay,px,py);fr=comp(fr,Bm)
        fr=comp(fr,E,ease(t/0.6));a=ease((t-0.3)/0.8);fr=comp(fr,T,a,dy=(1-a)*40)
        rw=int(180*ease_out(clamp((t-0.9)/0.6)))
        if rw>0:cv2.rectangle(fr,(W//2-rw//2,int(y0+160)),(W//2+rw//2,int(y0+163)),GOLD[::-1],-1)
        b=ease((t-1.2)/0.7);fr=comp(fr,S,b,dy=(1-b)*16)
        c=ease_out(clamp((t-1.8)/0.5));fr=comp(fr,C,c,dy=(1-c)*20)
        bb=ease((t-2.6)/0.8);fr=comp(fr,B,bb,dy=(1-bb)*24)
        fr=grade(fr);fr=letterbox(fr)
        fo=min(1,(dur-t)/1.0) if t>dur-1.0 else 1
        if fo<1:fr=(fr*fo).astype(np.uint8)
        yield fr
# ---------- build ----------
ID=float(M.get('intro_seconds',5.0));OD=float(M.get('outro_seconds',6.5));segs=[('intro',lambda:seg_intro(M['intro'],ID),ID)]
if 'trust' in M:segs.append(('trust',lambda:seg_trust(M['trust'],M['trust'].get('seconds',6.5),M['trust']['image']),M['trust'].get('seconds',6.5)))
for i,s in enumerate(M['scenes']):segs.append((f's{i}',(lambda i=i,s=s:seg_scene(i,s,SD)),SD))
if 'reviews' in M:
    M['reviews']['n']=len(M['reviews']['items'])
    for k,rv in enumerate(M['reviews']['items']):segs.append((f'r{k}',(lambda k=k,rv=rv:seg_review(k,rv,M['reviews'].get('seconds',5.5),M['reviews']['bg'][k%len(M['reviews']['bg'])])),M['reviews'].get('seconds',5.5)))
segs.append(('outro',lambda:seg_outro(M['outro'],OD),OD))
def build(ix):
    name,gen,dur=segs[ix];path=os.path.join(WORK,name+'.mp4')
    if A.reuse and os.path.exists(path) and name not in A.force.split(','):return name
    cv2.setNumThreads(max(1,A.threads//A.workers))   # the workers already fill the CPU budget
    encode(path,gen(),int(dur*FPS));return name
if __name__=='__main__':
    with cf.ProcessPoolExecutor(A.workers) as ex:
        for nme in ex.map(build,range(len(segs))):print('seg',nme,flush=True)
    # xfade chain
    names=[s[0] for s in segs];durs=[s[2] for s in segs];trans=['fade','dissolve','fade','smoothup']
    inputs=[];fc='';prev='[0:v]';off=0
    for i,nm in enumerate(names):inputs+=['-i',os.path.join(WORK,nm+'.mp4')]
    for i in range(1,len(names)):
        off+=durs[i-1]-XF;tr='fade' if names[i-1]=='intro' or names[i]=='outro' else trans[i%len(trans)]
        outl=f'[v{i}]' if i<len(names)-1 else '[v]';fc+=f'{prev}[{i}:v]xfade=transition={tr}:duration={XF}:offset={off:.3f}{outl};';prev=outl
    total=sum(durs)-XF*(len(names)-1)
    # score: warm pad + soft pulse (original, synthesised)
    rate=24000;chords=[(130.81,164.81,196),(110,130.81,164.81),(87.31,110,130.81),(98,123.47,146.83)]
    with wave.open(os.path.join(WORK,'score.wav'),'w') as w:
        w.setparams((1,2,rate,0,'NONE','not compressed'));N=int(total*rate)   # vectorised: same samples as the old per-sample loop, ~15x faster
        t=np.arange(N)/rate;ch=np.array(chords)[(t/8).astype(int)%4];fade=np.minimum(1,np.minimum(t/2.5,(total-t)/3))
        v=sum(np.sin(2*math.pi*ch[:,k]*t)*(0.05+0.02*np.sin(t*0.7+k)) for k in range(3))
        v+=np.sin(2*math.pi*55*t)*np.exp(-(t%0.75)*9)*0.10;v+=np.sin(2*math.pi*ch[np.arange(N),(t*8/3).astype(int)%3]*4*t)*np.exp(-(t%0.375)*10)*0.045
        w.writeframes((np.clip(v*fade,-1,1)*32767).astype('<i2').tobytes())
    subprocess.run(['ffmpeg','-y','-v','error',*inputs,'-i',os.path.join(WORK,'score.wav'),'-filter_complex',fc.rstrip(';'),'-map','[v]','-map',f'{len(names)}:a',*VCODEC('14M',A.threads),'-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-shortest','-movflags','+faststart',OUT],check=True)
    print(OUT,f'{total:.1f}s')
