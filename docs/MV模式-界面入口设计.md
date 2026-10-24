# MV 模式：界面入口设计

> **状态**：**已落地**（4.4.0）。§九 的 P1-P4 全部完成，自检 `[16] test_mv_entry` 23 项断言在跑，端到端验过一条带真音轨的 MV。
> **范围**：只做"让 Web 界面能出 MV / 配乐"这一件事——上传音轨、填歌词、把参数送到引擎。不碰节拍算法、不碰歌词对齐算法、不碰时间轴。
> **前提**：引擎侧一行代码都不用改（见 §一），缺的全在接口和界面。

---

## 一、现在的问题

引擎侧这条路是**通的**，而且一直有自检盯着：

- `pipeline/models.py:172` 的 `MusicInput` 定义了整套语义——`role="song"` 时以音频为总时长权威、不生成配音；`role="bgm"` 时以解说为权威、音乐压低垫底；`caption_source="lyrics"` 时歌词上屏。
- `pipeline/pipeline.py:458` 的 `_check_music_source()` 做输入校验，`:472` 的 `_prepare_music()` 做时长探测、节拍估计、日志汇报。
- `pipeline/pipeline.py:284` 的 `render_from_plan()` 也接了 `music` 参数——先拆后拍这条路径同样支持 MV。
- `app/engine.py:476` 只要 `opts.audio` 非空就构造 `MusicInput`，两条入口（`run()` / `render_from_plan()`）都吃。
- `tests/test_pipeline.py` 的 `[4c]` `[4d]` `[7]` `[8]` 四条一直在跑，覆盖时长权威、混音、节拍、歌词解析。

**断在三处，全在传输层和界面层：**

1. **`RunRequest` 里没有这几个字段。** `server/schemas.py:17` 的 `RunRequest` 只有 theme/duration/aspect/…，没有 `audio` / `lyrics` / `music_role`。所以 `server/app.py:107` 和 `server/plan.py:156` 只能写死 `audio=""`。前端就算想传也传不进去——请求模型会直接丢掉未知字段。
2. **没有上传通路。** 服务端 23 条路径里没有任何接收文件的接口（`server/media.py` 只做视频的 Range 响应）。`audio` 就算加了字段，也只能收一个服务器本地路径——浏览器拿不到本地文件路径，这条路对 Web 界面等于不存在。
3. **界面上没有入口。** `web/src/pages/CreatePage.tsx:26` 的 `DEFAULT_FORM` 里没有 mode、没有音频、没有歌词；整个页面只有主题/时长/画幅/镜长/风格/画质/帧率/代码绘制/真实模型/三分线。

净效果：**删除旧 Tkinter 界面之后，MV 与配乐模式从界面上消失了，只剩 `yiyan.py --audio --lyrics`。** 这是那次删除带出来的唯一实质能力回退。

## 二、目标

一句话：**在创作台上多一个"模式"开关，选了 MV 就出现音轨和歌词，选完就能出片。**

```
模式：解说短片 │ 音乐 MV │ 配乐模式
                          ↓ 选中后展开
      音轨  [拖入音频，或点这里选择]   → 上传 → 显示 名字 / 时长 / 大小
      歌词  [textarea]  读 .lrc 文件   → 行数
      节拍  [自动估计 ●]  BPM [  ] 首拍 [  ]
```

四条硬要求：

- **时长权威不能被界面骗。** MV 模式下总时长只能来自音频探测值，界面把「时长」那一行改成只读。用户在界面上填的 15 秒不能把一首 3 分 42 秒的歌压成 15 秒。
- **音轨要跟着拆解走。** 用户点了「先拆解」，音轨信息必须落进 `plan/meta.json`；否则进了拆解页点「开始拍摄」，拍出来是无声的。这是最容易漏的一环——`web/src/App.tsx:127` 的 `onRender` 只传 run_id，不带任何参数。
- **上传的不是路径，是内容。** 接口收文件、算哈希、返回 token；后续请求只认 token。**不能让客户端传任意路径**，否则 `audio="C:\\Windows\\System32\\..."` 会被 ffprobe 当输入打开，既泄露文件是否存在，也把 ffmpeg 变成了任意文件解析器。
- **没有音轨就不许进 MV。** 缺音轨时返回 422，不静默退回解说模式。静默退回的产物是「一条没有音乐的 MV」，用户会以为成功了。

## 三、数据契约的改动

### 3.1 `RunRequest` 补齐音乐字段

```python
class RunRequest(BaseModel):
    ...
    #: 音轨 token（由 POST /api/media/audio 返回），空串表示没有音轨。
    #: **不是路径**——服务端只把它当 uploads 目录里的文件名解析。
    audio: str = ""
    lyrics: str = ""
    music_role: Literal["song", "bgm"] = "song"
    bpm: float = Field(default=0.0, ge=0.0, le=400.0)
    beat_offset: float = Field(default=0.0, ge=0.0, le=10.0)
    auto_beats: bool = True
```

`music_role` 用 `Literal` 而不是 `str`，是因为 `MusicInput.is_bgm` 只认 `"bgm"` 这一个值，写错一个字母就静默变成 MV——这种错必须在进管线之前被 422 拦住。

`RenderRequest`（`server/schemas.py:126`）已经有这六个字段，不用动。

### 3.2 上传的返回体

```python
class AudioUpload(BaseModel):
    token: str            # 形如 "3f2a9c1d8e4b7a06.mp3"
    name: str             # 原始文件名，只用于显示
    size: int             # 字节
    duration: float       # 秒，ffprobe 探测
    format: str           # ffprobe 的 format_name，如 "mp3"
```

### 3.3 `plan/meta.json` 多一个 `music` 段

拆解时把音轨信息一起写进去：

```json
{
  "music": {
    "token": "3f2a9c1d8e4b7a06.mp3",
    "name": "海阔天空.mp3",
    "duration": 222.4,
    "role": "song",
    "lyrics": "[00:12.34]今天我\n[00:16.02]寒夜里看雪飘过",
    "bpm": 0.0,
    "beat_offset": 0.0,
    "auto_beats": true
  }
}
```

`GET /api/plan/{run_id}` 原样带出来，拆解页据此显示「音轨：海阔天空.mp3 · 222.4s · MV」这行徽章。

歌词正文存在 meta 里而不是单独一个文件：它只有几 KB，多一个文件就要多一处同步，不值得。

## 四、接口

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/media/audio` | multipart 上传音轨，返回 `AudioUpload` |
| `POST` | `/api/plan` | **新增** `audio` / `lyrics` / `music_role` / `bpm` / `beat_offset` / `auto_beats`；带 token 时用探测时长覆盖 `duration`，并把 `music` 写进 `meta.json` |
| `POST` | `/api/plan/{run_id}/render` | 已支持。`audio` 为空时**回落到 `meta.json.music`** |
| `GET` | `/api/plan/{run_id}` | 返回值新增 `music` |
| `POST` | `/api/runs` | **新增**同名字段，透传给 `RunOptions` |

### 4.1 上传接口

```
POST /api/media/audio
Content-Type: multipart/form-data
field: file
```

服务端流程：

1. 分块读，累计超过 `MAX_AUDIO_BYTES = 60 MB` 立即 413，不落盘。（一首 5 分钟的 FLAC 约 30 MB，10 分钟的 44.1k 立体声 WAV 约 100 MB——上限卡在 60 MB 是为了挡住 WAV 长片，同时容得下任何常见压缩格式。）
2. 写进临时文件，用 `pipeline.video.probe_duration()` 探测。探测不到或时长 ≤ 0 → 422「这个文件不是可解码的音频」。**扩展名白名单只是第一道**，真正的判据是 ffprobe 能不能解出时长——把 `.txt` 改名成 `.mp3` 必须在这一步被拒。
3. 算 `sha256` 前 16 位十六进制，作为文件名，扩展名取自探测结果（探测失败才回落到原始扩展名）。**同名同内容不重复落盘**——用户改一句歌词反复上传同一首歌是常态。
4. 落到 `ROOT / "work" / "uploads" / <token>`，返回 `AudioUpload`。
5. 顺手清理：每次上传时删掉 uploads 目录里 **mtime 超过 7 天**的文件。有 §4.3 的复制兜底，删掉不影响已经跑过的运行。

### 4.2 token 解析（唯一的安全边界）

```python
TOKEN_RE = re.compile(r"^[0-9a-f]{16}\.[a-z0-9]{2,5}$")

def resolve_audio(token: str) -> Path:
    """token → uploads 目录里的绝对路径。**不接受任何带路径分隔符的输入。**"""
```

两道检查，缺一不可：

- 正则先挡。`../` `..\\` `C:` 这类输入连格式都不对，第一道就拒。
- 再 `resolve()` 后检查前缀落在 uploads 目录内。正则挡不住的只有「符号链接指向外面」这一种，前缀检查兜住。

拿不到就 422「音轨不存在或已被清理，请重新上传」。**不要 404**——对前端来说这是参数错误，不是资源不存在。

### 4.3 音轨复制进运行目录

`_prepare_music()` 探测之前加一步：如果来源在 uploads 目录内，复制到 `runs/<run_id>/work/music.<ext>`，并把 `MusicInput.path` 改成副本路径。

理由：拆解页的「开始拍摄」可以在几天后点。如果那时 uploads 已经被清理，这份拆解就永远拍不出来了——而它明明是一次有效的工作成果。复制之后，一次运行自带音轨，uploads 可以放心删。

**复制失败不致命**：保留原路径，往 `ctx.notes` 里记一句「音轨未能复制进运行目录，原文件被清理后这份记录将无法重拍」。复制是个优化，不该让它把整次运行搞挂。

命令行（`yiyan.py --audio D:\...`）不走 uploads，自然不触发复制——本地路径本来就长期有效，没必要每次多占一份空间。

## 五、界面

### 5.1 创作台：模式行

放在「内容」面板之下、「时长」之上——它决定了下面几行的语义，位置必须在它们之前。

`Segmented` 三项：

| 值 | 标签 | 下面出现什么 |
|---|---|---|
| `narration` | 解说短片 | 什么都不变（默认） |
| `song` | 音乐 MV | 音轨 + 歌词 + 节拍；「时长」变只读 |
| `bgm` | 配乐模式 | 音轨 + 歌词（可空）+ 节拍；「时长」仍可改 |

### 5.2 音轨行

空态是一块虚线区，`onDragOver` / `onDrop` 都接，点一下也弹文件选择：

```
┌────────────────────────────────────────┐
│  拖入音频，或点这里选择                  │
│  mp3 / wav / m4a / flac / ogg · 最大 60 MB │
└────────────────────────────────────────┘
```

有音轨时换成一行信息 + 两个文字按钮：

```
海阔天空.mp3   222.4 秒 · 8.7 MB        [替换] [移除]
```

上传期间显示「上传中…」并禁用两个按钮——重复上传同一首会打两次请求，虽然服务端去重，但界面会闪。

**时长那一行同步变成只读**，显示 `222.4 秒`，副标题从「总秒数」改成「以音频为准」。这是整个改动里最重要的一处提示：用户必须一眼看出自己填的 15 秒不管用了。

### 5.3 歌词行

一个 `textarea`（6 行，等宽字体），下面一行小字显示「12 行 · 支持 LRC 时间戳」，右边一个「读 .lrc 文件」按钮。

**文件读取在浏览器里做**（`FileReader.readAsText`），填进 textarea，不发请求。理由：歌词是纯文本，几 KB，走一趟服务端除了多一个接口没有任何收益；而且读进来之后用户还能改——上传就没法改了。

`song` 模式下歌词为空时给一条提醒（不是报错）：「没有歌词时，上屏文字会退回解说词」——这正是 `MusicInput.caption_source` 的兜底行为，说清楚比让用户猜好。

### 5.4 节拍行

只在有音轨时出现。

```
节拍     [自动估计 ●]                    默认从音频里找 BPM
```

关掉「自动估计」之后才露出两个数字框：BPM、首拍（秒）。这两个字段对应 `MusicInput.bpm` / `offset`，`pipeline/pipeline.py:500` 的逻辑是「手工给了 BPM 就采信手工值，否则自动估计」。

为什么非留这个口子：自动估计有置信度门槛（`BEAT_MIN_CONFIDENCE`），不够可靠时会**放弃吸附并说明**。这时用户手里如果确知速度（比如素材本身就是 120 BPM 的鼓点），得有地方填进去。没有这个入口，他只能看着「本次不按拍切镜」无能为力。

### 5.5 长音频的提醒

MV 模式下列出 `duration / shot_len` 的镜数估计。超过 40 镜时补一句：

> 222 秒会拆成约 45 镜。代码绘制按 24fps 约需 62 分钟。

`EtaRow` 已经有耗时估算（`estimatePaintedSeconds`），这里只是把它和镜数摆在一起。不加这句的话，用户上传一首完整的歌、勾上代码绘制、点开始，然后等一个小时——他不会觉得是自己选的，只会觉得程序卡死了。

### 5.6 拆解页：音轨徽章

`GET /api/plan/{run_id}` 带回的 `music` 非空时，在标题行右侧加一枚 `Badge`：

```
音轨 海阔天空.mp3 · 222.4s · MV
```

`role === 'bgm'` 时文案换成「配乐」。用户从创作台跳到拆解页之后，这是唯一能确认「我刚才选的 MV 还在」的地方。

## 六、校验与不变量

1. **token 不可穿越。** 正则 + 前缀检查两道（§4.2）。任何带 `/` `\` `:` 的输入都进不到文件系统调用。
2. **上传必须是可解码音频。** ffprobe 探测出 `duration > 0` 才算通过；扩展名不参与判断。
3. **MV 模式必须有音轨。** `music_role in ("song","bgm")` 且 `audio` 为空 → 422。
4. **时长权威唯一。** `song` 模式下 `RunOptions.duration` 被探测值覆盖（`max(4, round(probed))`），界面只读，两者一致。
5. **拆解与拍摄用同一份音轨。** `POST render` 的 `audio` 为空时从 `meta.json.music.token` 取，不回落到「无音轨」。
6. **解说模式零变化。** `audio=""` 且 `music_role` 默认时，`app/engine.py:476` 的 `music` 是 `None`，管线走与现在完全一致的分支。

## 七、验收标准

- 上传一首 3:42 的 mp3：界面显示 `222.4 秒`，「时长」行变只读；不点任何按钮，`work/uploads/` 下多一个 16 位十六进制命名的文件。
- 点「先拆解」：`plan/meta.json` 里出现 `music` 段，`storyboard.json` 的总时长 ≈ 222 秒（不是 15 秒）。
- 从拆解页点「开始拍摄」：成片 `ffprobe` 时长 222.x 秒，**有 audio stream**，`volumedetect` 的平均音量不是 `-91 dB`（静音兜底轨的特征值）。
- 切回「解说短片」：音轨字段清空，出片与改动前完全一致（回归）。
- 把一个 `.txt` 改名成 `.mp3` 上传：422，提示「这个文件不是可解码的音频」。
- `POST /api/runs` 带 `audio="../yiyan.db"`：422，且不产生任何文件读取。
- 上传 70 MB 的文件：413，磁盘上不留临时文件。

## 八、改动清单

| 文件 | 改动 |
|---|---|
| `server/media.py` | 新增 `resolve_audio()` / `save_audio()` / `POST /api/media/audio`（现在这个文件只有 `video_response`） |
| `server/schemas.py` | `RunRequest` 补 6 个字段；新增 `AudioUpload`；`MAX_AUDIO_BYTES` / `AUDIO_EXTS` 常量 |
| `server/app.py` | `create_run` 里 `audio=""` → `req.audio` 等（现在写死在 `:107`） |
| `server/plan.py` | `_opts()` 透传音乐字段；`create_plan` 解析 token、覆盖 duration、写 `meta.music`；`render_plan` 回落 meta；`read_plan` 返回 `music` |
| `pipeline/pipeline.py` | `_prepare_music()` 前加一步「uploads 来源复制进 `work/music.*`」，失败降级为 note |
| `web/src/lib/api.ts` | `FormState` 补 9 个字段；`RenderOptions` 补 4 个；新增 `uploadAudio()` |
| `web/src/pages/CreatePage.tsx` | 模式行、音轨面板、歌词框、节拍行、时长只读态、长音频提醒 |
| `web/src/pages/PlanPage.tsx` | 音轨徽章 |
| `tests/test_pipeline.py` | 新增 `[16]`：token 穿越被拒、`RunRequest` 字段齐全、`meta.music` 落盘与回读 |
| `README.md` | 能力表第 51/52 行改回真话（见 §十）；「三种输入模式」补一句界面入口在哪 |
| `docs/使用手册.md` | 二节加「出 MV 的步骤」 |

## 九、分期

**P1 后端通路**：`AudioUpload` + 上传接口 + `resolve_audio()` + `RunRequest` 六个字段 + `plan.py` 的 meta 落盘与回落。这一期做完，用 `curl` 就能出 MV，界面还没动。风险最低，先让整条链路成立。

**P2 创作台界面**：模式行、音轨面板、歌词框、节拍行、时长只读。这一期纯前端，不碰后端契约。

**P3 拆解页与复制**：`meta.music` 的显示、`_prepare_music` 的复制兜底。复制放最后，因为它只影响「很久之后重拍」这一个场景。

**P4 文档与自检**：`[16]` 测试、README 与手册。

### 实际落地记录

四期全部完成，代码入口与上面 §八 的清单一致。执行中有三处与原计划的偏差，都记在下面，因为它们是"照计划写会出错"的地方：

1. **`_shot_bounds` 与 `_check_shots` 要一起改。** 计划里只提到"服务端校验"和"管线校验"两处 floor，实际先改 `server/plan.py:_shot_bounds` 后，`render_plan` 过了接口层、进管线又被 `pipeline/pipeline.py:_check_shots` 拦下，报「拆解文件校验未通过」。两处必须用同一个公式 `round(min(MIN_SHOT_SECONDS, 总时长 / 镜数), 3)`。
2. **floor 要按落盘精度取整。** 镜头时长存 3 位小数（1.333），拿未取整的 1.33333 去比，会把自己刚写出来的合法值判成越界。少了 `round(..., 3)` 第一次修完仍然失败。
3. **`_adopt_music_source` 只对 uploads 来的文件生效。** 命令行 `--audio D:\music.mp3` 给的是本机长期路径，每次运行多复制一份纯属浪费空间。判据是 `resolve()` 后能不能 `relative_to(UPLOADS_DIR)`。

验收（§七 的七条）实际跑出来的结果：

| 验收项 | 结果 |
|---|---|
| 上传 3 秒 wav | `{"token":"4194aa4787c98434.wav","name":"测试音轨.wav","size":529278,"duration":3.0,"format":"wav"}` |
| `audio="../yiyan.db"` | 422「音轨 token 格式不对（应形如 3f2a9c1d8e4b7a06.mp3）」 |
| `.txt` 改名成 `.mp3` | 422「这个文件不是可解码的音频」 |
| 填歌词不传音轨 | 422「填了歌词但没有音轨，请先上传音频」 |
| `plan/meta.json` | `music` 段完整（token / name / duration 3.0 / role song / lyrics / bpm / beat_offset / auto_beats） |
| `GET /api/plan/{id}` | `music` 原样回读，`total_duration` 被音频覆盖成 4 秒（3 秒 → `max(4, round(3))`） |
| 拆解页拍摄（不带参数） | 成片 `h264 4.000s` + `aac 3.994s`，`volumedetect` 报 `mean_volume: -18.8 dB` / `max_volume: -14.3 dB`（不是静音兜底的 -91 dB） |
| `runs/<id>/work/music.wav` | 存在，516.9 KB，证明复制生效 |
| 前端 | `tsc --noEmit` 零错误，`npm run build` 通过（295 KB JS / 51.5 KB CSS） |
| 自检 | 424 项通过 / 0 失败（原 401 + 新增 23） |

## 十、明确不做

- **不做音频裁剪。** 想截取 30 秒高潮段，属于时间轴编辑那一期。
- **不做波形图。** 好看，但一个 canvas 波形组件换不来任何功能；用户要的是「这首对不对」，名字加时长够了。
- **不做多音轨。** 一条成片一条音轨，混音逻辑（`compose.mux_audio`）也只吃一条。
- **不做上传管理页。** uploads 目录按 7 天自动清理，没有需要用户管的东西。
- **不做云端 TTS 与歌词对齐。** 歌词上屏走 `parse_lyrics` 的均分 + 节拍吸附，这是现状，不在这期改。
- **不改 `pipeline/beats.py` 的算法。** 自动估计不准是已知问题，这期只提供手工填 BPM 的出口。

## 十一、风险

| 风险 | 说明 | 应对 |
|---|---|---|
| 长音频把出片时间放大十几倍 | 3 分 42 秒 = 45 镜，代码绘制约 62 分钟 | §5.5 在点开始之前就把镜数和耗时摆出来 |
| 用户以为 MV 还要填时长 | 界面上「时长」那一行还在 | 选中 MV 后立即变只读 + 副标题改「以音频为准」 |
| uploads 目录无限增长 | 每次上传都落一个文件 | 内容哈希去重 + 每次上传顺手清 7 天前的 + 运行时复制进 run 目录 |
| 拆解页拍出来没声音 | `onRender` 只传 run_id，不带参数 | `meta.music` 落盘 + `render` 回落（§3.3、§4.3） |
| 节拍估计不准导致镜头乱切 | 置信度不够时管线已放弃吸附 | 已有行为，这期只补手工 BPM 的出口 |
| 前端传了 `music_role: "mv"` 之类的值 | 拼错一个字母就静默变成默认 MV | `Literal["song","bgm"]`，pydantic 直接 422 |

## 十二、待确认

三件都已定，落地时按下面执行：

1. **上传上限 60 MB** —— 按「容得下任何压缩格式、挡掉长 WAV」定的。一首 5 分钟 FLAC 约 30 MB，10 分钟 44.1kHz 立体声 WAV 要 100 MB 以上。后者没有理由走上传：让 ffmpeg 转一道再传，体积小一个数量级。超限直接 413，不把 70 MB 收完再判断。
2. **7 天清理** —— 可接受。运行时会复制副本，清理不影响已出的片；只影响「7 天后拿旧拆解重拍」，那种情况重传一次即可。清理是顺手做的（每次上传扫一遍），没有独立定时器。
3. **模式控件用 `Segmented`** —— 创作台的 `SettingRow` 右侧空间有限，三段横排刚好；`Radio` 卡片要占三行，会把音轨面板推到首屏之外。标签取两字（`解说` / `MV` / `配乐`），四个字的「解说短片」在 1/3 宽度里会换行。
