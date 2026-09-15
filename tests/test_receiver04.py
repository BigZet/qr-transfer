from __future__ import annotations
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from qr_transfer.storage import DurableSession
from qr_transfer.protocol import ProtocolError, encode, decode
from qr_transfer.transfer import packets, prepare_object
from qr_transfer.capture import receive_frames
from qr_transfer.pipeline import LatestFrames, region, Decoded


class Receiver04Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'data'; self.source.write_bytes(bytes(range(256))*5)
        self.transfer=prepare_object(self.source,chunk_size=200,container='7z-aes256')
        self.sequence=list(packets(self.transfer,metadata_every=100))

    def test_odd_layout_padding_budget(self):
        from qr_transfer.player import estimate
        small=self.root/'odd'; small.write_bytes(b'x'*4000)
        descriptor=prepare_object(small).descriptor
        result=estimate(descriptor,slots=2)
        self.assertEqual(result['unique_frames'],3)
        self.assertEqual(result['cycle_frames'],4)
        self.assertEqual(result['cycle_seconds'],1.2)

    def test_resume_early_middle_late_and_complete(self):
        for stage in (1,4,len(self.sequence)-1,len(self.sequence)):
            path=self.root/f'state-{stage}'
            with DurableSession(path) as state:
                for raw in self.sequence[:stage]: state.receiver.feed(raw)
            # Damaged caches are repaired from committed bytes, not silently trusted.
            for p in (path/'chunks').glob('*'): p.write_bytes(b'bad cache')
            with DurableSession(path,resume=True) as state:
                self.assertEqual(state.committed,stage-1)
                for raw in self.sequence: state.receiver.feed(raw)
                self.assertEqual(state.snapshot()['committed_chunks'],self.transfer.descriptor.total)
                self.assertEqual(state.snapshot()['missing_ranges'],[])
            self.assertEqual((path/'object.bin').read_bytes(),self.source.read_bytes())

    def test_second_writer_and_crash_releases_lock_rolls_back_transaction(self):
        path=self.root/'crash'
        with DurableSession(path) as state:
            for raw in self.sequence[:3]: state.receiver.feed(raw)
            with self.assertRaises(FileExistsError): DurableSession(path,resume=True)
        code='''import sys,os
from pathlib import Path
from qr_transfer.storage import DurableSession
s=DurableSession(Path(sys.argv[1]),resume=True)
s.db.execute("INSERT INTO packets(kind,idx,raw) VALUES(1,999,?)", (b'uncommitted',))
print('ready',flush=True)
input()
os._exit(77)
'''
        proc=subprocess.Popen([sys.executable,'-c',code,str(path)],stdout=subprocess.PIPE,stdin=subprocess.PIPE,text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(),'ready')
            proc.communicate("crash\n",timeout=5)
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close()
        with DurableSession(path,resume=True) as state:
            self.assertEqual(state.committed,2)
            for raw in self.sequence: state.receiver.feed(raw)
            self.assertTrue(state.snapshot()['content_verified'])

    def test_corrupt_schema_and_packet_fail_without_lock_leak(self):
        for corruption in ('schema','packet'):
            path=self.root/corruption
            with DurableSession(path) as state: state.receiver.feed(self.sequence[0])
            with contextlib.closing(sqlite3.connect(path/'packets.sqlite3')) as db:
                if corruption=='schema': db.execute('PRAGMA user_version=999')
                else: db.execute('UPDATE packets SET raw=?',(b'bad',))
                db.commit()
            for _ in range(2):
                with self.assertRaises(ProtocolError): DurableSession(path,resume=True)

    def test_disk_exhaustion_does_not_commit_data(self):
        path=self.root/'disk'
        with DurableSession(path) as state:
            state.receiver.feed(self.sequence[0])
            with patch('qr_transfer.storage.shutil.disk_usage') as space:
                space.return_value.free=0
                with self.assertRaisesRegex(ProtocolError,"insufficient_disk"): state.receiver.feed(self.sequence[1])
            self.assertEqual(state.committed,0)
        with DurableSession(path,resume=True) as state:
            self.assertEqual(len(state.receiver.received),0)
            for raw in self.sequence: state.receiver.feed(raw)
            self.assertTrue(state.snapshot()['content_verified'])

    def test_duplicate_storage_and_foreign_session(self):
        path=self.root/'duplicates'
        with DurableSession(path) as state:
            for raw in self.sequence[:2]: state.receiver.feed(raw)
            before=(path/'packets.sqlite3').stat().st_size
            for _ in range(100): state.receiver.feed(self.sequence[1])
            self.assertEqual((path/'packets.sqlite3').stat().st_size,before)
            other=encode(replace(decode(self.sequence[1]),transfer_id=b'x'*16))
            self.assertEqual(state.receiver.feed(other).reason,'foreign_session')
            self.assertEqual(state.snapshot()['missing_ranges'],[[1,6]])

    def test_multi_capture_independent_slots_resume_and_conflict(self):
        path=self.root/'capture'
        source=iter([[Decoded(self.sequence[0],0),Decoded(self.sequence[1],1)]])
        def grab():
            try: return next(source)
            except StopIteration: raise KeyboardInterrupt
        first=receive_frames(path,grab,lambda x:x)
        self.assertEqual(first['exit_code'],130)
        source=iter([[b'bad',self.sequence[2]]]+[[p] for p in self.sequence[3:]])
        result=receive_frames(path,lambda:next(source),lambda x:x,resume=True)
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(result['initial_bytes'],200)
        # No grab or decoder needed once committed object is complete.
        result=receive_frames(path,lambda: self.fail('unexpected grab'),lambda x:x,resume=True)
        self.assertEqual(result['frames_captured'],0)
        conflict=encode(replace(decode(self.sequence[1]),payload=b'z'*200))
        source=iter([[self.sequence[0],self.sequence[1],conflict]])
        bad=receive_frames(self.root/'conflict',lambda:next(source),lambda x:x)
        self.assertEqual(bad['termination'],'packet_conflict')

    def test_decoder_periodic_search_and_geometry_change(self):
        try:
            from PIL import Image
            import zxingcpp
        except ImportError:
            self.skipTest('Install receiver extras')
        from qr_transfer.player import matrix
        from qr_transfer.pipeline import MultiDecoder
        def image(raw):
            tile=Image.new('RGB',(185,185),'white')
            rows=matrix(raw)
            for y,row in enumerate(rows):
                for x,value in enumerate(row):
                    if value: tile.putpixel((x+4,y+4),(0,0,0))
            return tile.resize((740,740),Image.Resampling.NEAREST)
        picture=Image.new('RGB',(1600,900),'white')
        picture.paste(image(self.sequence[0]),(20,80)); picture.paste(image(self.sequence[1]),(820,80))
        decoder=MultiDecoder()
        with patch('qr_transfer.pipeline.time.monotonic',return_value=0):
            self.assertEqual(len(decoder(picture)),2)
            self.assertEqual(len(decoder(picture)),2)
        with patch('qr_transfer.pipeline.time.monotonic',return_value=2):
            self.assertEqual(len(decoder(picture)),2)
        self.assertEqual(decoder.stats['full_search_frames'],2)
        smaller=picture.crop((0,0,800,900))
        self.assertEqual(len(decoder(smaller)),1)
        self.assertEqual(decoder.stats['full_search_frames'],3)

    def test_monitor_relative_roi_handles_negative_origin(self):
        monitor=dict(left=-1920,top=589,width=1920,height=1080)
        self.assertEqual(region(monitor,(20,30,1000,800)),dict(left=-1900,top=619,width=1000,height=800))
        for roi in ((-1,0,10,10),(0,0,2000,100),(0,0,1,0)):
            with self.assertRaises(ValueError): region(monitor,roi)

    def test_bounded_queue_keeps_fresh_frames(self):
        count=[0]
        @contextlib.contextmanager
        def factory():
            def grab(): count[0]+=1; return count[0]
            yield grab
        source=LatestFrames(factory,fps=200,capacity=2)
        try:
            first=source.grab(); time.sleep(.08); second=source.grab()
            self.assertGreater(second,first+1)
            self.assertLessEqual(source.stats['queue_peak'],2)
            self.assertGreater(source.stats['dropped_old'],0)
        finally: source.close()
        self.assertFalse(source.thread.is_alive())

if __name__=='__main__': unittest.main()
