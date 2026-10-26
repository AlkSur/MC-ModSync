import hashlib
import io
import threading
from pathlib import Path

import pytest

from mcmodsync import http_download as hd


def test_two_files_use_full_32_connection_budget(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(hd, 'download_one', lambda *args, **kwargs: seen.append(kwargs['segments']))
    tasks = [dict(path=f'mods/{i}.jar', sha256=f'{i:064x}', size=32*1024*1024) for i in (1,2)]
    hd.download_blobs(tasks, 'https://unused.invalid', str(tmp_path), total_slots=32)
    assert seen == [16,16]


def test_range_resume_keeps_bytes_and_progress(tmp_path, monkeypatch):
    requests = []
    progress = []
    class Response(io.BytesIO):
        status = 206
        def __init__(self, data, header):
            super().__init__(data)
            self.headers = {'Content-Range':header}
    def opener(_, request, timeout):
        requests.append(request.get_header('Range'))
        if len(requests) == 1:
            return Response(b'ab', 'bytes 0-5/6')
        return Response(b'cdef','bytes 2-5/6')
    monkeypatch.setattr(hd,'_open',opener)
    monkeypatch.setattr(hd.time,'sleep',lambda _:None)
    path = tmp_path/'part'
    hd._fetch_segment('https://unused.invalid',str(path),0,5,progress=progress.append)
    assert path.read_bytes() == b'abcdef'
    assert requests == ['bytes=0-5','bytes=2-5']
    assert sum(progress) == 6


def test_temporary_segment_failure_does_not_downgrade_to_single(tmp_path, monkeypatch):
    calls=[]
    def segmented(url,dst,*args,**kwargs):
        calls.append('multi')
        if len(calls)==1:
            raise hd.DownloadError(5,'temporary network failure')
        Path(dst).write_bytes(b'ok')
        return 206
    monkeypatch.setattr(hd,'_fetch_segmented',segmented)
    monkeypatch.setattr(hd,'_fetch',lambda *a,**kw:pytest.fail('must not downgrade on a temporary error'))
    monkeypatch.setattr(hd.time,'sleep',lambda _:None)
    dst=tmp_path/'out'
    hd.download_one('https://unused.invalid',str(dst),'',3*1024*1024,
                    lambda p:Path(p).read_bytes()==b'ok',retries=2)
    assert calls == ['multi','multi']


def test_chunk_queue_obeys_connection_limit_and_merges(tmp_path, monkeypatch):
    active=peak=0
    lock=threading.Lock()
    barrier=threading.Barrier(2,timeout=5)
    ranges=[]
    def segment(url,dst,start,end,timeout=60,progress=None):
        nonlocal active,peak
        with lock:
            active+=1
            peak=max(peak,active)
            ranges.append((start,end))
        barrier.wait()
        Path(dst).write_bytes(b'x'*(end-start+1))
        with lock:
            active-=1
        return 206
    monkeypatch.setattr(hd,'_fetch_segment',segment)
    dst=tmp_path/'out'
    hd._fetch_segmented('https://unused.invalid',str(dst),64,2,segment_size=8)
    assert peak==2
    assert len(ranges)==8
    assert dst.read_bytes()==b'x'*64
    assert not list(tmp_path.glob('*.p*'))
