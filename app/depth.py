#!/usr/bin/env python3
"""Make 16-bit depth maps for every image in a folder using Depth-Anything-V2-Small (HF transformers).
usage: depth.py <images_dir> <out_dir>   -> out_dir/<stem>.png (near = bright)"""
import sys,glob,os,numpy as np,torch
from PIL import Image
from transformers import pipeline
src,dst=sys.argv[1],sys.argv[2];os.makedirs(dst,exist_ok=True)
dev='mps' if torch.backends.mps.is_available() else 'cpu'
pipe=pipeline('depth-estimation',model='depth-anything/Depth-Anything-V2-Small-hf',device=dev)
fs=sorted(f for f in glob.glob(src+'/*') if f.lower().endswith(('.jpg','.jpeg','.png')))
for f in fs:
    im=Image.open(f).convert('RGB');r=pipe(im)['predicted_depth'];d=r.squeeze().float().cpu().numpy()
    d=(d-d.min())/(d.max()-d.min()+1e-6)  # Depth-Anything outputs relative inverse depth: larger = nearer
    Image.fromarray((d*65535).astype(np.uint16)).resize(im.size,Image.BILINEAR).save(os.path.join(dst,os.path.splitext(os.path.basename(f))[0]+'.png'))
    print('depth',f,d.shape,flush=True)
