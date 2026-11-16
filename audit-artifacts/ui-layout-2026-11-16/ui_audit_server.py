"""Read-only UI audit host with disposable synthetic project/task data."""
from pathlib import Path
import json
import sys

SOURCE=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SOURCE))
BASE=Path(__file__).resolve().parent
DATA=BASE/'fixture-data'
DATA.mkdir(exist_ok=True)
from pipeline import config
config.ROOT=DATA
config.ENV_FILE=DATA/'.env'
config.UPLOADS_DIR=DATA/'work'/'uploads'
config.load_env_file.__defaults__=(config.ENV_FILE,)
config.ENV_FILE.write_text('\n'.join(f'{k}=' for k in ['LLM_API_KEY','JEV_API_KEY','VIDEO_API_KEY','VLM_API_KEY','TTS_API_KEY'])+'\nCOMFYUI_BASE_URL=http://127.0.0.1:1\n',encoding='utf-8')
original_resource=config.resource_dir
config.resource_dir=lambda name:BASE/'web' if name=='web' else original_resource(name)
from server.app import app
from server import history,taskstore
from PIL import Image,ImageDraw
rid='ui-audit-demo'
project=DATA/'runs'/rid
plan=project/'plan';plan.mkdir(parents=True,exist_ok=True)
shots=[]
for i in range(1,9):
    shots.append({'id':i,'act':['开场','人物','过程','收尾'][(i-1)//2],'duration':4.,'start':(i-1)*4.,
        'visual':f'镜头{i}：清晨的手工面馆，暖色灯光照亮工作台，主角整理食材并与客人交谈。',
        'camera':'中景，缓慢推近','narration':f'第{i}个镜头的示例旁白，只用于界面布局审计。',
        'prompt_image':'warm documentary scene','prompt_video':'slow push in','subject_refs':['hero'],
        'status':'approved','quality':3,'attempts':1,'review_mode':'simulated','visual_check':'not_performed',
        'caption':f'示例字幕 {i}'})
board={'project':{'title':'一碗面里的城市记忆 · 布局审计示例','style':'温暖纪实、烟火气','aspect_ratio':'16:9','total_duration':32},
       'subjects':[{'id':'hero','name':'面馆师傅','visual_lock':'围裙、短发、温暖表情'}],'shots':shots}
script={'title':board['project']['title'],'logline':'跟随一位面馆师傅，观察城市清晨的日常。','tone':'温暖纪实','target_audience':'短视频观众','hook':'一碗面从清晨开始。','acts':[{'name':n,'content':f'{n}场次示例文本。这里展示场次编辑和镜头信息。','time_range':f'{j*8}-{j*8+8}s'} for j,n in enumerate(['开场','人物','过程','收尾'])]}
meta={'revision':3,'rendered':True,'live':False,'render_profile':{'live':False,'painted':False,'painted_fps':12,'painted_quality':'medium','clean_frames':False,'palette':'warm'},'edited':{'script':True,'shots':[2,4]},'stale_shots':[4],
      'last_preview_at':'2026-11-16 10:30:00','last_preview_revision':3,'last_shot_preview':{'shot':2,'at':'2026-11-16 10:32:00','revision':3},
      'audio_edit':{'voice_gain':1,'music_gain':.28,'subtitle_offset':0,'transition':'none','text_anim':False,'voice_duck':False},'subtitle_mode':'burn',
      'subtitle_lines':[{'start':i*4.,'end':i*4+3.5,'text':f'这是用于界面布局审计的第{i+1}行示例字幕。'} for i in range(8)]}
for name,payload in [('brief.json',{'theme':'城市清晨的面馆','total_duration':32,'aspect_ratio':'16:9','style':'温暖纪实'}),('script.json',script),('storyboard.json',board),('meta.json',meta)]:
    (plan/name).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
versions=plan/'versions'/'rev-000003';versions.mkdir(parents=True,exist_ok=True)
for p in plan.glob('*.json'):(versions/p.name).write_bytes(p.read_bytes())
output=project/'output';output.mkdir(exist_ok=True)
(output/'storyboard.json').write_text(json.dumps(board,ensure_ascii=False),encoding='utf-8')
(output/'subtitles.srt').write_text('1\n00:00:00,000 --> 00:00:03,500\n用于界面审计的示例字幕\n',encoding='utf-8-sig')
frames=project/'work'/'frames';frames.mkdir(parents=True,exist_ok=True)
for i in range(1,9):
    im=Image.new('RGB',(640,360),(44+10*i,58,90));draw=ImageDraw.Draw(im)
    draw.rectangle((60,100,400,310),fill=(170,110+5*i,70));draw.ellipse((320,110,560,330),fill=(235,190,100))
    im.save(frames/f'shot{i:02d}_clean.png')
# Project variants allow list/card density inspection, not actual generated works.
for k in range(2,7):
    pp=DATA/'runs'/f'ui-audit-project-{k}'/'plan';pp.mkdir(parents=True,exist_ok=True)
    for name in ['brief.json','script.json','storyboard.json','meta.json']:
        payload=json.loads((plan/name).read_text(encoding='utf-8'))
        if name=='meta.json':payload['rendered']=False
        (pp/name).write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
seen=set()
@app.middleware('http')
async def seed_tasks(request,call_next):
    guest=request.headers.get('x-guest-id','')
    if guest and guest not in seen:
        seen.add(guest)
        for k,status in enumerate(['done','failed','interrupted']):
            jid=f'ui-audit-task-{len(seen)}-{k}'
            taskstore.create_task(jid,f'g:{guest}','布局审计示例任务 '+str(k+1),kind='plan',run_id=rid,options={'live':False,'painted':False})
            taskstore.update_task(jid,status=status,error='' if status=='done' else '示例：用于检视错误状态和任务列表布局')
    return await call_next(request)

if __name__=='__main__':
    import subprocess,shutil
    master=output/'final.mp4'
    if not master.is_file():
        subprocess.run([str(SOURCE/'bin'/'ffmpeg.exe'),'-y','-loglevel','error','-loop','1','-i',str(frames/'shot01_clean.png'),'-t','4','-r','12','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(master)],check=True,timeout=30)
    for k in (2,3):
        variant=DATA/'runs'/f'ui-audit-project-{k}'
        (variant/'output').mkdir(exist_ok=True)
        shutil.copyfile(master,variant/'output'/'final.mp4')
        shutil.copyfile(output/'storyboard.json',variant/'output'/'storyboard.json')
        vm=json.loads((variant/'plan'/'meta.json').read_text(encoding='utf-8'))
        vm['rendered']=True
        (variant/'plan'/'meta.json').write_text(json.dumps(vm,ensure_ascii=False),encoding='utf-8')
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8879,access_log=False,log_level='warning')

