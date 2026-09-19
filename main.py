#!/usr/bin/env python3
"""Higgsfield API smoke test: Seedance 2.5 text-to-video via the official SDK `subscribe` method.
Credentials: HF_KEY="key-id:key-secret" in .env.local (loaded at runtime, never printed)."""
import sys,os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent/'.env.local')
if not os.getenv('HF_KEY'):sys.exit('HF_KEY missing: add it to .env.local as key-id:key-secret')
import higgsfield_client as hf
from higgsfield_client import Failed,NSFW,Cancelled
MODEL='bytedance/seedance-2.5/text-to-video'
ARGS={'prompt':'A cinematic scene at sunset','duration':5,'resolution':'720p','aspect_ratio':'16:9'}
def main():
    terminal={'bad':None};seen=set()
    def on_enqueue(rid):print('request_id',rid,flush=True)
    def on_update(st):
        n=type(st).__name__
        if n not in seen:print('status',n,flush=True);seen.add(n)
        if isinstance(st,(Failed,NSFW,Cancelled)):terminal['bad']=n
    print('submitting',MODEL,ARGS)
    try:
        result=hf.subscribe(MODEL,arguments=ARGS,on_enqueue=on_enqueue,on_queue_update=on_update)
    except hf.HiggsfieldClientError as e:
        print(f'NOT SUCCESSFUL: {terminal["bad"] or "API error"}: {e}');return 2
    if terminal['bad']:print(f'NOT SUCCESSFUL: request ended with {terminal["bad"]}');return 2
    video=(result or {}).get('video');url=video.get('url') if isinstance(video,dict) else video
    if not url:print('completed but no video url in result:',result);return 3
    print('VIDEO_URL',url);return 0
if __name__=='__main__':sys.exit(main())
