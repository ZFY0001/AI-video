"""Isolated audit probes. No external services or application user data are used.

NOTE: this script reproduces the **pre-fix** behavior recorded in
`backend-probe-results.json` (2026-11-15). The P1 fixes changed the ownership API
(`Job(principal=...)`, `_can_view(run_id, Principal)`), so re-running it against the
fixed code is expected to fail at those call sites. The post-fix guarantees are
covered by `tests/test_pipeline.py::test_access_and_versions`.
"""
from __future__ import annotations
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
DATA = Path(tempfile.mkdtemp(prefix='probe-', dir=Path(__file__).parent))
from server import auth
auth.DB_PATH = DATA / 'audit.db'
auth.SECRET_PATH = DATA / 'unused.key'
from server import app as api, history, plan, media
from server.jobs import Job
from server.schemas import RunRequest, RenderRequest, ShotPatch
from app.engine import RunOptions

results = []
def record(name, **evidence):
    results.append({'probe': name, **evidence})
    print('PROBE', name, evidence, flush=True)

async def request(method, path, payload=None):
    raw = b'' if payload is None else json.dumps(payload).encode()
    headers = [(b'host', b'127.0.0.1')]
    if payload is not None:
        headers += [(b'content-type', b'application/json'), (b'content-length', str(len(raw)).encode())]
    sent, messages = False, []
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': raw, 'more_body': False}
        await asyncio.Future()
    async def send(message):
        messages.append(message)
    scope = {'type':'http', 'asgi':{'version':'3.0','spec_version':'2.4'},
             'http_version':'1.1','method':method,'scheme':'http','path':path,
             'raw_path':path.encode(),'query_string':b'', 'headers':headers,
             'client':('127.0.0.1',12345), 'server':('127.0.0.1',8000)}
    await asyncio.wait_for(api.app(scope, receive, send), timeout=5)
    status = next(m['status'] for m in messages if m['type']=='http.response.start')
    body = b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
    return status, body

async def api_probes():
    job = Job('audit-private-theme', RunOptions(theme='audit-private-theme'), owner_id=9001)
    job.result = {'project_dir': str(DATA / 'private-run')}
    job.finished = True
    registry = api.REGISTRY
    with patch.dict(registry._jobs, {job.job_id:job}), patch.object(registry, '_order', [job.job_id]):
        s, b = await request('GET', '/api/runs')
        record('anonymous_job_list', status=s, exposes_private_theme=b'audit-private-theme' in b)
        s, b = await request('GET', f'/api/runs/{job.job_id}')
        record('anonymous_job_snapshot', status=s, exposes_private_result=b'private-run' in b)
        s, b = await request('GET', f'/api/runs/{job.job_id}/events')
        record('anonymous_job_sse', status=s)
        job.finished = False
        s, b = await request('POST', f'/api/runs/{job.job_id}/cancel', {})
        record('anonymous_job_cancel', status=s, cancel_requested=job.cancel_requested)
        job.finished = True
    with patch.object(history, 'ENV_PATH', DATA / '.env'):
        s, b = await request('POST', '/api/env', {'AUDIT_UNLISTED_KEY':'audit-value'})
        record('anonymous_arbitrary_env_key', status=s,
               accepted=(DATA / '.env').read_text().strip() == 'AUDIT_UNLISTED_KEY=audit-value')

asyncio.run(api_probes())
@contextlib.contextmanager
def unavailable_db():
    raise OSError('simulated DB unavailable')
    yield
with patch.object(history, 'db', unavailable_db):
    record('ownership_lookup_failure', anonymous_can_view=history._can_view('owned-run', None))

plan_dir = DATA / 'example' / 'plan'
plan_dir.mkdir(parents=True)
board = {'project':{'total_duration':8}, 'subjects':[], 'shots':[
    {'id':1,'act':'a','duration':4,'start':0,'visual':'old-1','camera':'fixed','narration':''},
    {'id':2,'act':'a','duration':4,'start':4,'visual':'old-2','camera':'fixed','narration':''}]}
def seed(meta=None):
    for name, value in {'storyboard.json':board, 'meta.json':meta or {'live':False},
                       'brief.json':{'theme':'audit','total_duration':8}}.items():
        (plan_dir / name).write_text(json.dumps(value), encoding='utf-8')
seed()
original_read, original_write = plan._read, plan._write
barrier, write_lock = threading.Barrier(2), threading.Lock()
def simultaneous_read(path):
    value = original_read(path)
    if path.name == 'storyboard.json':
        barrier.wait(timeout=5)
    return value
def serialized_write(path, value):
    # Only serialize individual atomic writes, to isolate missing transaction protection.
    with write_lock:
        return original_write(path, value)
errors = []
def worker(shot):
    try:
        plan.patch_shot('example', shot, ShotPatch(visual=f'new-{shot}'), None)
    except Exception as exc:
        errors.append(type(exc).__name__)
with patch.object(plan, '_plan_dir', return_value=plan_dir), patch.object(plan,'_read',simultaneous_read), patch.object(plan,'_write',serialized_write):
    threads = [threading.Thread(target=worker, args=(x,)) for x in (1,2)]
    for t in threads: t.start()
    for t in threads: t.join()
final = original_read(plan_dir / 'storyboard.json')
record('concurrent_shot_patch', errors=errors,
       both_requests_succeeded=not errors,
       saved_visuals=[s['visual'] for s in final['shots']],
       updates_kept=sum(s['visual'].startswith('new-') for s in final['shots']))

seed()
captured = {}
def fake_create(theme, opts, owner_id=0):
    captured.update(live=opts.live, painted=opts.painted, fps=opts.painted_fps,
                    quality=opts.painted_quality)
    return type('Accepted', (), {'job_id':'audit-not-started'})()
with patch.object(plan, '_plan_dir', return_value=plan_dir), patch.object(plan.REGISTRY,'create',side_effect=fake_create):
    plan._start_render('example', RenderRequest(), None)
record('empty_render_options_for_offline_plan', saved_live=False, actual_options=captured)

seed({'music':{'token':'0123456789abcdef.wav','role':'song'}})
(plan_dir.parent / 'work').mkdir(exist_ok=True)
(plan_dir.parent / 'work' / 'music.wav').write_bytes(b'audit-existing-run-copy')
with patch.object(plan, '_plan_dir', return_value=plan_dir), patch.object(media, 'UPLOADS_DIR', DATA / 'empty-uploads'):
    try:
        plan._start_render('example', RenderRequest(), None)
        record('render_with_expired_upload_and_run_copy', unexpected_success=True)
    except Exception as exc:
        record('render_with_expired_upload_and_run_copy',
               existing_run_audio=True, status=getattr(exc,'status_code',None),
               detail=str(getattr(exc,'detail',exc)))

from pipeline.pipeline import Pipeline
from pipeline.config import Settings
from pipeline.models import Shot, ProjectBrief
pipe = object.__new__(Pipeline)
pipe.settings = Settings(values={})
pipe.ctx = type('Context', (), {'live':False})()
shot = Shot(id=1, act='a', duration=4, narration='', visual='audit scene', camera='fixed')
record('quality_description_ignores_image',
       same_description=pipe._describe(DATA/'image-does-not-exist-A.png',shot,ProjectBrief(theme='audit'),1)
                        ==pipe._describe(DATA/'image-does-not-exist-B.png',shot,ProjectBrief(theme='audit'),1),
       both_images_missing=True)

from pipeline.config import new_run_context
with patch('pipeline.config.datetime') as clock:
    clock.now.return_value.strftime.return_value='20261115-120000'
    c1=new_run_context('same-audit-theme',Settings(values={}),False,False,False)
    c2=new_run_context('same-audit-theme',Settings(values={}),False,False,False)
    record('same_second_run_id_collision', same_run_id=c1.run_id==c2.run_id,
           same_project_directory=c1.project_dir==c2.project_dir)

static = DATA / 'dist'
static.mkdir()
sibling = DATA / 'dist-backup'
sibling.mkdir()
(sibling / 'audit.txt').write_text('audit-only', encoding='utf-8')
with patch.object(api,'DIST',static):
    response = api.spa('../dist-backup/audit.txt')
    record('static_prefix_boundary', sibling_file_accepted=Path(response.path)==sibling/'audit.txt')

result_path = Path(__file__).with_name('backend-probe-results.json')
result_path.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=False,indent=2))





