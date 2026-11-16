"""Iteration-2 audit observations. Uses isolated files and fake providers only."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent
DATA = Path(tempfile.mkdtemp(prefix='isolated-', dir=OUT))
from server import auth
auth.DB_PATH = DATA / 'audit.db'
auth.SECRET_PATH = DATA / 'unused-secret.key'
auth.init_db()
from server import app as api, history, plan, branding, exports, jobs
from server.jobs import Job, BusyError
from server.schemas import RenderRequest, BrandRequest, ExportRequest
from app.engine import RunOptions
from pipeline import pipeline as pm, clipcache, compose as cm
from pipeline.config import Settings
from pipeline.models import ProjectBrief, Shot, Storyboard, Subject
from pipeline.genre import pick_genre
from pipeline.images import inspect_image
from PIL import Image, ImageDraw

history.RUNS = DATA / 'runs'
plan.ROOT = DATA
api.ROOT = DATA
history.RUNS.mkdir()
observations = []
def record(name, **data):
    observations.append({'probe':name, **data})
    print(name, json.dumps(data, ensure_ascii=False), flush=True)
def run(name, fn):
    try: fn()
    except Exception as e:
        record(name, probe_error=f'{type(e).__name__}: {e}')
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def image(path, color):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    im=Image.new('RGB',(128,96),color)
    ImageDraw.Draw(im).rectangle((0,0,40,80),fill='white')
    im.save(path)
    return path

def seed(run_id='audit-project'):
    p=history.RUNS/run_id/'plan'; p.mkdir(parents=True,exist_ok=True)
    data={'brief.json':{'theme':'audit','total_duration':4,'aspect_ratio':'16:9'},
          'script.json':{'title':'audit','acts':[]},
          'storyboard.json':{'project':{'title':'audit','total_duration':4},'subjects':[],
              'shots':[{'id':1,'act':'a','duration':4,'start':0,'visual':'scene','camera':'fixed','narration':'old text'}]},
          'meta.json':{'revision':0,'render_profile':{'live':False,'painted':False,'painted_fps':24,'painted_quality':'medium','clean_frames':False,'palette':'auto'}}}
    for name, payload in data.items(): (p/name).write_text(json.dumps(payload),encoding='utf-8')
    return p

class Log:
    def event(self,*a,**kw): pass
    def error(self,*a,**kw): pass
class Ken:
    name='kenburns'; provider_id=''
    def __init__(self): self.inputs=[]
    def available(self): return True,''
    def make_clip(self, shot, still, target, duration, **kw):
        self.inputs.append(str(still)); Path(target).parent.mkdir(parents=True,exist_ok=True)
        Path(target).write_bytes(b'fake-encoded:'+Path(still).read_bytes())
        return Path(target)

def fake_pipe(folder, deep=False):
    p=object.__new__(pm.Pipeline)
    p.ctx=NS(work_dir=folder/'work', project_dir=folder, notes=[], budget_ok=True, cost_used_cny=0,live=False)
    p.ctx.work_dir.mkdir(parents=True,exist_ok=True)
    p.settings=Settings(values={}); p.log=Log(); p.genre=pick_genre('audit')
    p.preview=False; p.work_scale=1; p.still_scale=1.; p.fixture_scale=1.; p.guides=False
    p.only_shot=0; p.no_cache=False; p.palette_id='auto'; p._video_size=(128,96)
    p.progress=lambda *a:None; p._stop_requested=lambda *a:False
    p._model_candidates=lambda:[{'id':'test-image','description':'test'}]
    p.decider=NS(**{n:lambda *a,**k:{} for n in ['route_task','model_route','review_quality','pick_best','retry_strategy']})
    def decide(name,*a,**kw):
        if name=='route_task': return {'route':'deep_review' if deep else 'proceed_fast'}
        if name=='model_route': return {'decision':'test-image'}
        if name=='review_quality': return {'quality':4,'verdict':'accept'}
        if name=='pick_best': return {'best':'1'}
        return {}
    p._decide=decide
    p.gen_count=0
    def generate(shot,brief,board,attempt,seed):
        p.gen_count+=1
        return image(folder/f'generated-{p.gen_count}.png',(20+30*p.gen_count,30,140))
    p._generate_still=generate
    p._describe=lambda path,*a:json.dumps({'technical':inspect_image(path),'review_mode':'simulated','visual_check':'not_performed'})
    p.video_chain=[Ken()]
    return p

def draw_stub(shot,brief,genre,path,**kw): return image(path,(0,180,0))

def ownership_failure():
    owner=auth.Principal('guest','a'*32)
    job=Job('audit',RunOptions(theme='audit'),principal=owner)
    project=history.RUNS/'new-owner-write-failed'; project.mkdir()
    with patch.object(auth,'record_owner',side_effect=auth.OwnershipUnavailable('injected')):
        job._on_start(project.name)
    record('new_project_owner_write_failure', callback_returned=True,
           persisted_owner=auth.run_owner_key(project.name),
           anonymous_can_view_after_db_recovers=history._can_view(project.name,None),
           warning_logged=any('归属登记失败' in str(x) for x in job.buffer))

def chosen_image_and_cache():
    folder=DATA/'pipeline-probe'; p=fake_pipe(folder)
    brief=ProjectBrief(theme='audit',total_duration=4)
    shot=Shot(1,'a',4,'old text','scene','fixed')
    board=Storyboard(project={},shots=[shot])
    with patch.object(pm,'render_still',side_effect=draw_stub),patch.object(pm,'render_overlay',side_effect=draw_stub):
        p._process_shot(shot,brief,board)
        selected=Path(shot.frame_path); actual=Path(p.video_chain[0].inputs[-1])
        record('selected_frame_not_used_by_kenburns',selected=selected.name,
               encoded_input=actual.name,same_pixels=sha(selected)==sha(actual))
        first_gen=p.gen_count; first_video=len(p.video_chain[0].inputs)
        p._process_shot(shot,brief,board)
        record('cache_lookup_after_image_generation',
               additional_image_generations=p.gen_count-first_gen,
               additional_video_encodes=len(p.video_chain[0].inputs)-first_video,
               selected_current_frame_matches_encoded_input=sha(Path(shot.frame_path))==sha(actual))

def candidate_evidence():
    folder=DATA/'candidate-probe'; p=fake_pipe(folder,True)
    p._make_clip=lambda *a:folder/'fake.mp4'
    shot=Shot(1,'a',4,'text','scene','fixed')
    p._process_shot(shot,ProjectBrief(theme='audit'),Storyboard(project={},shots=[shot]))
    record('selected_candidate_evidence', selected=Path(shot.frame_path).name,
           selected_hash=sha(shot.frame_path), recorded_hash=shot.image_sha256,
           hashes_match=sha(shot.frame_path)==shot.image_sha256, candidates_generated=p.gen_count)

def fingerprint_inputs():
    folder=DATA/'fingerprint-probe'; p=fake_pipe(folder)
    shot=Shot(1,'a',4,'text','scene','fixed',subject_refs=['hero'],prompt_image='portrait',workflow_id='wf')
    hero=Subject('hero','Hero',visual_lock='red coat')
    board=Storyboard(project={},subjects=[hero],shots=[shot]); brief=ProjectBrief(theme='audit')
    old_key=p._clip_key(shot,brief,board); old_prompt=p._locked_prompt(shot,board)[0].prompt_image
    hero.visual_lock='blue coat'
    new_prompt=p._locked_prompt(shot,board)[0].prompt_image
    new_key=p._clip_key(shot,brief,board)
    record('cache_omits_subject_visual_lock', effective_prompt_changed=old_prompt!=new_prompt,
           cache_key_changed=old_key!=new_key)
    key_before=p._clip_key(shot,brief,board)
    p.settings=Settings(values={'COMFYUI_BASE_URL':'http://different-local-endpoint'})
    record('cache_omits_image_provider_config',cache_key_changed=key_before!=p._clip_key(shot,brief,board))

def mutable_snapshot():
    pd=seed('snapshot-project'); owner=auth.Principal('guest','b'*32)
    auth.record_owner('snapshot-project',owner)
    captured={}
    def accept(theme,opts,**kw): captured['path']=Path(opts.plan_dir);return NS(job_id='accepted-test')
    with patch.object(plan.REGISTRY,'create',side_effect=accept):
        plan._start_render('snapshot-project',RenderRequest(),owner)
    snap=captured['path']; before=sha(snap/'render_profile.json')
    with patch.object(plan.REGISTRY,'create',side_effect=BusyError('injected busy')):
        try: plan._start_render('snapshot-project',RenderRequest(painted=True),owner)
        except Exception as exc: status=getattr(exc,'status_code',None)
    record('rejected_render_overwrites_active_snapshot',rejected_status=status,
           same_snapshot_path=True,active_profile_changed=before!=sha(snap/'render_profile.json'),
           active_snapshot_painted=json.loads((snap/'render_profile.json').read_text())['painted'])

def painted_output_source():
    folder=history.RUNS/'painted-project';(folder/'output').mkdir(parents=True)
    original=folder/'output'/'final.mp4';original.write_bytes(b'placeholder')
    painted=folder/'painted'/'final.mp4';painted.parent.mkdir();painted.write_bytes(b'painted')
    result={'project_dir':str(folder),'storyboard':{'shots':[{'id':1,'duration':4,'visual':'x'}]},
            'brief':{'aspect_ratio':'16:9'},'compose':{'video':str(original)},'music':{},'lyrics':[]}
    from pipeline import painted as pb
    with patch.object(pb,'render_shots',return_value={'ok':True,'video':str(painted),'has_audio':False}):
        actual=pb.apply_painted(result)
    record('painted_master_vs_history_export_source',task_result=Path(actual['compose']['video']).relative_to(folder).as_posix(),
           export_source=exports._source(folder).relative_to(folder).as_posix(),
           same_source=Path(actual['compose']['video'])==exports._source(folder),
           history_fixed_source_exists=(folder/'output'/'final.mp4').is_file())

def subtitles_external_only():
    folder=DATA/'subtitles-probe';folder.mkdir()
    clip=folder/'clip.mp4';clip.write_bytes(b'old-burned-subtitle-video')
    board=Storyboard(project={},shots=[Shot(1,'a',4,'old text','scene','fixed')])
    def concat(clips,out,work):Path(out).write_bytes(b''.join(Path(p).read_bytes() for p in clips));return out
    def mux(source,bed,final,duration):Path(final).write_bytes(Path(source).read_bytes());return final,False
    with patch.object(cm,'concat_clips',side_effect=concat),patch.object(cm,'mux_audio',side_effect=mux),patch.object(cm,'probe_duration',return_value=4.),patch.object(cm,'probe_resolution',return_value='128x96'):
        a=cm.compose(board,[clip],folder/'a',folder,subtitle_lines=[{'start':0,'end':4,'text':'NEW'}])
        b=cm.compose(board,[clip],folder/'b',folder,subtitle_lines=[])
    record('subtitle_edit_only_affects_sidecar',same_video_bytes=sha(a.video)==sha(b.video),
           edited_sidecar_contains_new_text='NEW' in Path(a.subtitle).read_text(encoding='utf-8-sig'),
           cleared_sidecar_empty=not Path(b.subtitle).read_text(encoding='utf-8-sig').strip())

def same_aspect_brand_export():
    pd=seed('same-aspect-brand');runpath=pd.parent;(runpath/'output').mkdir()
    (runpath/'output'/'final.mp4').write_bytes(b'audit-source')
    with patch.object(exports,'_source_dims',return_value=(1080,1920)),patch.object(exports,'_brand_context',return_value=({'intro_text':'brand'},'brand-hash',1)):
        try: exports.start_export(runpath.name,ExportRequest(aspect='9:16',mode='fit'),None)
        except Exception as exc:
            record('same_aspect_brand_export',status=getattr(exc,'status_code',None),detail=str(getattr(exc,'detail',exc)))

def metadata_locks_and_access():
    pd=seed('private-route-project');owner=auth.Principal('guest','c'*32);other=auth.Principal('guest','d'*32)
    auth.record_owner(pd.parent.name,owner)
    outcomes={}
    for who,label in [(owner,'owner'),(other,'other_guest'),(None,'anonymous')]:
        try: branding.read_brand(pd.parent.name,who); outcomes[label]=200
        except Exception as e: outcomes[label]=getattr(e,'status_code',None)
    before=json.loads((pd/'meta.json').read_text())
    branding.save_brand(pd.parent.name,BrandRequest(intro_text='audit',expected_version=0),owner)
    after=json.loads((pd/'meta.json').read_text())
    record('brand_access_and_revision',route_status=outcomes,
           project_revision_before=before['revision'],project_revision_after=after['revision'],
           brand_version=after['brand']['version'],shared_lock=branding._project_lock is plan._project_lock)

def task_queue_capability():
    owner=auth.Principal('guest','e'*32)
    registry=jobs.JobRegistry()
    registry._active='active-audit'
    registry._jobs['active-audit']=NS(running=True)
    try: registry.create('next',RunOptions(theme='next'),principal=owner)
    except BusyError as exc:
        record('task_queue_capability',second_task_accepted=False,reason=str(exc),
               persistent_statuses=list(__import__('server.taskstore',fromlist=['STATUSES']).STATUSES))

(OUT/'openapi.json').write_text(json.dumps(api.app.openapi(),ensure_ascii=False,indent=2),encoding='utf-8')
for name,fn in [('queue',task_queue_capability),('ownership',ownership_failure),('generated_input',chosen_image_and_cache),
                ('candidate_evidence',candidate_evidence),('fingerprint',fingerprint_inputs),
                ('snapshot',mutable_snapshot),('painted_source',painted_output_source),
                ('subtitles',subtitles_external_only),('same_aspect_brand',same_aspect_brand_export),
                ('access',metadata_locks_and_access)]:run(name,fn)
(OUT/'probe-results.json').write_text(json.dumps(observations,ensure_ascii=False,indent=2),encoding='utf-8')
print('Observations:',len(observations),'Probe errors:',sum('probe_error' in x for x in observations),flush=True)


