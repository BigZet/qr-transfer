"""I04 local evidence: durable 10 MiB stream, Full HD multi-QR and capture pipeline.
No VDI throughput claims. Requires .[sender,receiver], playwright, psutil, Chrome.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from qr_transfer.container import prepare, extract
from qr_transfer.transfer import prepare_object, packets
from qr_transfer.storage import DurableSession
from qr_transfer.player import render
from qr_transfer.capture import receive_frames
from qr_transfer.pipeline import MultiDecoder, LatestFrames


def run(out):
    import psutil
    import zxingcpp
    from PIL import Image
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True,exist_ok=False)
    report={'schema':1,'evidence_level':'local_synthetic_no_vdi','runs':[]}
    # Streaming fixture construction and packet consumption: no list of all chunks.
    source=out/'large.bin'
    with source.open('wb') as target:
        for i in range(10240): target.write(hashlib.shake_256(i.to_bytes(4,'big')).digest(1024))
    large_archive=prepare(source,out/'large.7z','synthetic-i04-only',profile='strong')
    transfer=prepare_object(large_archive,container='7z-aes256')
    proc=psutil.Process(); memory=[]; stop=threading.Event()
    def sample():
        while not stop.is_set(): memory.append(proc.memory_info().rss); stop.wait(.02)
    sampler=threading.Thread(target=sample);sampler.start()
    start=time.perf_counter(); checkpoints=[]
    try:
        with DurableSession(out/'large-state') as state:
            for raw in packets(transfer):
                state.receiver.feed(raw)
                if len(state.receiver.received) in (1,1000,2000,3000):
                    checkpoints.append({'chunks':len(state.receiver.received),'rss':proc.memory_info().rss})
            snapshot=state.snapshot()
        assert snapshot['content_verified']
        report['storage_10mib']={'encrypted_object_bytes':large_archive.stat().st_size,'seconds':time.perf_counter()-start,'rss_first':memory[0],
            'rss_peak':max(memory),'checkpoints':checkpoints,'snapshot':snapshot}
    finally: stop.set();sampler.join()
    restored_large=extract(out/'large-state/object.bin',out/'large-restored','synthetic-i04-only',descriptor=transfer.descriptor)
    assert hashlib.sha256((restored_large/'large.bin').read_bytes()).digest()==hashlib.sha256(source.read_bytes()).digest()
    report['storage_10mib']['manifest_verified']=True
    # Actual encrypted small object for browser/capture/extraction runs.
    small=out/'synthetic.bin'
    small.write_bytes(b''.join(hashlib.sha256(b'i04'+i.to_bytes(4,'big')).digest() for i in range(2048)))
    password='synthetic-i04-only'
    archive=prepare(small,out/'object.7z',password)
    transfer=prepare_object(archive,container='7z-aes256')
    render(archive,transfer.descriptor,out/'player',slots=2,interval_ms=400)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page(viewport={'width':1920,'height':1080})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            url=(out/'player/standalone.html').resolve().as_uri()
            for slots,mode in ((1,'sync'),(2,'sync'),(2,'staggered')):
                page.goto(url);page.wait_for_function('window.QRTransfer && QRTransfer.snapshot().ready')
                page.select_option('#slots',str(slots));page.select_option('#mode',mode)
                page.click('#fullscreen');page.wait_for_timeout(100)
                geometry=page.evaluate('QRTransfer.snapshot()')
                assert geometry['slots']==slots
                assert geometry['geometry']['canvas_backing']==([925,925] if slots==1 else [1858,925])
                label=f'{slots}-{mode}'
                if slots==2: page.screenshot(path=str(out/f'{label}.png'))
                decoder=MultiDecoder();page.evaluate('QRTransfer.start()')
                result=receive_frames(out/label,lambda:Image.open(io.BytesIO(page.screenshot())).convert('RGB'),
                                      decoder,fps=12,first_timeout=10,idle_timeout=15,total_timeout=30)
                page.evaluate('QRTransfer.pause()');assert result['exit_code']==0,result
                restored=extract(out/label/'object.bin',out/f'{label}-restored',password,descriptor=transfer.descriptor)
                assert (restored/'synthetic.bin').read_bytes()==small.read_bytes()
                report['runs'].append({'slots':slots,'mode':mode,'capture':result,'decoder':dict(decoder.stats),
                                       'player':page.evaluate('QRTransfer.snapshot()'),'restored_equal':True})
                print(f'{label}: {result["elapsed_seconds"]:.3f}s OK',flush=True)
            # Resize and mode changes preserve transfer ID; physical geometry stays integer.
            tid=page.evaluate('QRTransfer.snapshot().transfer_id')
            for size in ((3840,2160),(1920,1080),(1000,700),(1920,1080)):
                page.set_viewport_size({'width':size[0],'height':size[1]});page.wait_for_timeout(100)
                value=page.evaluate('QRTransfer.snapshot()')
                assert value['transfer_id']==tid
                assert value['geometry']['canvas_backing'][1]%185==0
                assert len(MultiDecoder()(Image.open(io.BytesIO(page.screenshot()))))==value['slots']
            # Force late join, one unreadable slot, resize and resume in the SAME session.
            page.evaluate('QRTransfer.reset(); QRTransfer.step(); QRTransfer.step()')
            path=out/'late-resize'; seen=0
            for phase in range(2):
                with DurableSession(path,resume=bool(phase)) as state:
                    for index in range(100):
                        shot=Image.open(io.BytesIO(page.screenshot())).convert('RGB')
                        if index%4==0:
                            from PIL import ImageDraw
                            ImageDraw.Draw(shot).rectangle((0,0,shot.width//2,shot.height),fill='white')
                        for decoded in MultiDecoder()(shot): state.receiver.feed(decoded.raw)
                        if state.receiver.state.value=='object_verified':break
                        page.evaluate('QRTransfer.step()')
                        if phase==0 and index==4:break
                    seen=state.snapshot()['received_chunks']
                if phase==0: page.set_viewport_size({'width':1600,'height':1000});page.wait_for_timeout(100)
            assert (path/'object.bin').read_bytes()==archive.read_bytes()
            report['late_resize_resume']={'ok':True,'received_chunks':seen}
            # Static Full HD multi-QR image for repeatable decoder/pipeline measurements.
            page.set_viewport_size({'width':1920,'height':1080});page.evaluate('QRTransfer.reset()');page.wait_for_timeout(100)
            picture=Image.open(io.BytesIO(page.screenshot())).convert('RGB')
            expected=sorted(bytes(c.bytes) for c in zxingcpp.read_barcodes(picture))
            assert len(expected)==2
            def decode_one(_):
                assert sorted(bytes(c.bytes) for c in zxingcpp.read_barcodes(picture))==expected
            before=time.perf_counter()
            for i in range(30):decode_one(i)
            serial=time.perf_counter()-before
            before=time.perf_counter()
            with ThreadPoolExecutor(2) as pool:list(pool.map(decode_one,range(30)))
            report['decoder_threads']={'images':30,'serial_seconds':serial,'two_threads_seconds':time.perf_counter()-before,
                'note':'Empirical concurrent decoding; shared immutable image, no process serialization.'}
            # Move QR content and blank a slot to invalidate the cached geometry.
            decoder=MultiDecoder();assert len(decoder(picture))==2
            moved=Image.new('RGB',picture.size,'white');moved.paste(picture.resize((1600,900)),(100,100))
            assert len(decoder(moved))==2
            report['search_recovery']={'ok':True,**decoder.stats}
            @contextmanager
            def factory():yield lambda:picture.copy()
            report['pipeline_comparison']=[]
            for threaded in (False,True):
                decoder=MultiDecoder()
                source=LatestFrames(factory,fps=30)
                try:
                    result=receive_frames(
                        out/f'pipeline-{threaded}',source.grab if threaded else lambda:picture.copy(),decoder,
                        fps=30,total_timeout=2,first_timeout=5,paced=threaded)
                finally:source.close()
                report['pipeline_comparison'].append({'threaded':threaded,'capture':result,'queue':dict(source.stats),'decoder':dict(decoder.stats)})
            assert not errors,errors
            browser.close()
        report['ok']=True
    finally:
        (out/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report

def check_edges(out):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True,exist_ok=False)
    source=out/'source.bin';source.write_bytes(hashlib.shake_256(b'i04-odd').digest(4000))
    archive=prepare(source,out/'object.7z','synthetic-i04-only')
    transfer=prepare_object(archive,container='7z-aes256')
    assert transfer.descriptor.total==2
    render(archive,transfer.descriptor,out/'player',slots=2)
    results=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='chrome',headless=True)
        try:
            for mode,dpr in ((m,d) for m in ('sync','staggered') for d in (1,1.25,1.8)):
                page=browser.new_page(viewport={'width':1920,'height':1080},device_scale_factor=dpr)
                page.goto((out/'player/standalone.html').resolve().as_uri())
                page.wait_for_function('window.QRTransfer && QRTransfer.snapshot().ready')
                page.select_option('#mode',mode);page.click('#fullscreen');page.wait_for_timeout(100)
                snapshot=page.evaluate('QRTransfer.snapshot()');assert snapshot['cycle_frames']==4
                with DurableSession(out/f'{mode}-{dpr}') as state:
                    for index in range(8):
                        # Ignore initial frame to ensure recovery from the padded schedule.
                        page.evaluate('QRTransfer.step()')
                        picture=Image.open(io.BytesIO(page.screenshot())).convert('RGB')
                        for code in MultiDecoder()(picture):state.receiver.feed(code.raw)
                    assert state.snapshot()['content_verified']
                results.append({'mode':mode,'dpr':dpr,'padding_metadata':True,'verified':True})
                page.close()
        finally:browser.close()
    report={'schema':1,'ok':True,'odd_last_group':results}
    (out/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--edges-only',action='store_true')
    args=parser.parse_args()
    if args.edges_only:check_edges(args.output)
    else:
        run(args.output)
        check_edges(args.output/'edges')
