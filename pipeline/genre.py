"""一言片场 · 离线创意引擎的题材词库。

作用：在没有配置语言大模型时，管线仍能跑出**像样的中文解说词与画面描述**，
而不是占位符乱码。设计上把「题材 → 词库」抽出来，接入真实大模型后本模块自动退居备用。

词库只提供词汇与句式骨架，不改变提示词库里的任何提示词文字。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .common import stable_hash


@dataclass
class Genre:
    key: str
    label: str
    tone: str
    keywords: tuple[str, ...]
    palette: tuple[tuple[str, str], ...] = ()  # 由深到浅的主色
    accents: tuple[str, ...] = ()
    decor: str = "bokeh"
    # 解说词素材
    subjects: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    emotions: tuple[str, ...] = ()
    hook: str = ""
    peak: str = ""
    close: str = ""
    look: tuple[str, ...] = ()  # 画面描述用


GENRES: tuple[Genre, ...] = (
    Genre(
        key="food",
        label="烟火美食",
        tone="烟火气、暖色调、纪实风",
        keywords=("烩面", "面", "吃", "美食", "菜", "汤", "小吃", "火锅", "烧烤", "早餐", "夜宵", "厨", "味", "食"),
        palette=(("#2A1608", "#5A2E10"), ("#8A4318", "#C97430"), ("#E8A24A", "#F6D08A")),
        accents=("#FFD9A0", "#FFB060", "#FFF3DC"),
        decor="steam",
        subjects=("一碗热汤面", "老街的小馆", "案板上的面团", "翻滚的汤锅", "掌勺的老师傅", "刚出锅的那一勺"),
        details=("羊骨吊足八小时的高汤", "手腕一抖扯出的宽面", "碗沿那圈辣油", "门帘上抖落的水汽", "葱花落在汤面的那一下"),
        actions=("掀帘进门", "甩开面团", "浇上热汤", "端碗上桌", "低头喝下第一口"),
        values=("一口热汤的踏实", "被认真对待的日常", "街坊之间不用多说的熟络"),
        emotions=("暖", "踏实", "熨帖", "念想"),
        hook="门帘一掀，热气先冲出来。",
        peak="面扯开的那一瞬，整条街的清晨都醒了。",
        close="一碗下肚，人才算真正醒了。",
        look=("steam rising through warm light", "hand-pulled noodles mid-air", "broth surface shimmering", "warm tungsten glow", "shallow depth of field"),
    ),
    Genre(
        key="city",
        label="城市宣传",
        tone="开阔、清晨到夜幕的时间流、电影感",
        keywords=("城市", "家乡", "街", "夜景", "地标", "桥", "都市", "宣传片", "文旅", "县城", "镇"),
        palette=(("#08131F", "#123049"), ("#1E5A78", "#3E93B0"), ("#8FD0DC", "#F2C879")),
        accents=("#FFD79A", "#7FD4E8", "#FFF6E2"),
        decor="skyline",
        subjects=("城市天际线", "跨江大桥", "清晨的立交", "亮灯的窗", "河面上的倒影", "老城巷口"),
        details=("第一班公交亮起的车灯", "桥索上滚过的晨雾", "玻璃幕墙反射的那片云", "夜里十点还亮着的那扇窗"),
        actions=("灯火次第亮起", "车流汇成光带", "云影掠过楼顶", "行人穿过斑马线"),
        values=("不断生长的劲头", "把日子过成风景"),
        emotions=("开阔", "笃定", "热烈"),
        hook="第一缕光落在楼顶的时候，城市醒了。",
        peak="灯亮起来的那一秒，所有忙碌都有了答案。",
        close="这就是它每天的样子。",
        look=("city skyline at golden hour", "aerial view of traffic light trails", "long exposure skyline", "glass towers reflecting clouds", "cinematic wide shot"),
    ),
    Genre(
        key="person",
        label="人物故事",
        tone="克制、自然光、35mm 胶片质感",
        keywords=("故事", "人物", "匠", "老人", "手艺人", "传承", "坚守", "人生", "父亲", "母亲", "老师"),
        palette=(("#1A1712", "#3A3226"), ("#6B5A44", "#A08A6A"), ("#D8C39E", "#F2E7D0")),
        accents=("#F0D9A8", "#C9B48C", "#FFF8EA"),
        decor="dust",
        subjects=("布满老茧的手", "工作台前的侧脸", "墙上的旧照片", "窗边的一束光", "堆满工具的木架"),
        details=("虎口上那道旧疤", "台灯下压着的图纸", "喝了一半的搪瓷缸", "袖口磨出的毛边"),
        actions=("低头专注", "抬手擦汗", "把工具归位", "望向窗外"),
        values=("一辈子只做一件事", "把时间熬成手艺"),
        emotions=("安静", "笃定", "温柔"),
        hook="他的手停了一下，又继续。",
        peak="光和尘一起落下来，时间在这里走得很慢。",
        close="日子就这么过，手艺也就这么留下了。",
        look=("35mm film grain portrait", "natural window light", "weathered hands working", "intimate close-up", "muted earthy palette"),
    ),
    Genre(
        key="product",
        label="产品商业",
        tone="干净、棚拍光、商业质感",
        keywords=("产品", "发布", "手机", "车", "科技", "智能", "设备", "品牌", "新品"),
        palette=(("#0B0D12", "#1B2230"), ("#2E3A4E", "#4E6A8A"), ("#AFC6DC", "#F0F4F8")),
        accents=("#FFFFFF", "#8FC6FF", "#E8F2FF"),
        decor="grid",
        subjects=("产品本体", "金属边框的特写", "屏幕亮起的一刻", "包装盒打开的角度", "桌面上的倒影"),
        details=("边框上那道 45 度切角", "屏幕点亮时的第一帧", "按键回弹的那点阻尼"),
        actions=("缓缓转过来", "点亮", "合上", "推入画面"),
        values=("把复杂收进简洁", "细节值得被看见"),
        emotions=("利落", "可靠", "高级"),
        hook="它转过来的那一刻，细节说话了。",
        peak="光沿着边框走了一圈。",
        close="剩下的，交给上手的那一刻。",
        look=("studio product hero shot", "clean seamless background", "softbox reflection on metal", "macro detail", "commercial lighting"),
    ),
    Genre(
        key="culture",
        label="国风古建",
        tone="留白、水墨质感、低饱和",
        keywords=("古", "国风", "非遗", "寺", "庙", "园林", "汉服", "书法", "茶", "瓷", "古镇", "文化"),
        palette=(("#141310", "#2C2A24"), ("#5A5548", "#8E8776"), ("#C9C2AE", "#EFE9D8")),
        accents=("#E8DCC0", "#B9A882", "#FBF6E8"),
        decor="ink",
        subjects=("飞檐的一角", "青石板路", "窗棂后的光", "案上的茶盏", "廊下的石阶"),
        details=("瓦当上未完的纹样", "檐角挂着的铜铃", "茶面上那层薄雾", "砚台里未干的墨"),
        actions=("风过檐角", "水痕漫开", "灯芯轻晃", "衣袖拂过石栏"),
        values=("把时间留成样子", "不争朝夕的耐性"),
        emotions=("静", "古意", "绵长"),
        hook="风过檐角，铜铃响了一声。",
        peak="一笔落下，墨在宣纸上慢慢走开。",
        close="老东西不说话，但它一直在。",
        look=("ink wash texture", "traditional chinese eaves", "muted palette", "oriental aesthetics", "soft diffused light"),
    ),
)

GENERIC = Genre(
    key="generic",
    label="通用叙事",
    tone="克制、纪实、有呼吸感",
    keywords=(),
    palette=(("#101418", "#1E2830"), ("#33424E", "#5A7284"), ("#A8BCC8", "#E6EEF2")),
    accents=("#F0E6D2", "#BFD4DE", "#FFFFFF"),
    decor="bokeh",
    subjects=("主体本身", "所处的环境", "一个具体的手部动作", "与环境的关系"),
    details=("一个只属于它的细节", "被反复使用留下的痕迹", "光线落在表面的一瞬"),
    actions=("慢慢展开", "被拿起", "被放下", "转向光"),
    values=("被认真对待的日常", "不喧哗的坚持"),
    emotions=("笃定", "安静", "明亮"),
    hook="先看见细节，再看见它。",
    peak="光和动作在同一个瞬间对上。",
    close="故事到这里，刚好。",
    look=("documentary photography", "natural light", "shallow depth of field", "observational framing"),
)


def pick_genre(text: str) -> Genre:
    """按关键词命中数选题材，命中相同取更靠前者。"""
    best_key = "generic"
    best_score = 0
    for genre in GENRES:
        score = sum(1 for kw in genre.keywords if kw in text)
        if score > best_score:
            best_score = score
            best_key = genre.key
    if best_score == 0:
        return GENERIC
    return next(g for g in GENRES if g.key == best_key)


def stable_choice(options: tuple[str, ...], seed: str, offset: int = 0) -> str:
    if not options:
        return ""
    idx = (stable_hash(f"{seed}#{offset}") + offset) % len(options)
    return options[idx]


DURATION_PATTERNS = (
    r"\d+\s*秒(钟)?", r"\d+\s*s(ec(onds?)?)?\b", r"\d+\s*分钟", r"\d+\s*min(utes?)?\b",
    r"\d+\s*分\d*\s*秒?", r"半分钟", r"一分钟",
)
# 指令性词块：去掉后剩下的才是创作对象
INSTRUCTION_CHUNKS = (
    r"给(我|我们|咱)", r"帮(我|我们|咱)", r"请", r"麻烦", r"来一?[条个支]", r"做一?[条个支]",
    r"生成一?[条个支]?", r"拍一?[条个支]", r"搞一?[条个支]",
    r"(宣传|推广|介绍|广告|纪录)片?", r"短?视频", r"样片", r"文案",
    r"主题是?", r"内容(是|为)?", r"要求", r"关于",
)
# 叙述动词：只在段首去掉，避免误删正文里的"故事"
NARRATION_VERBS = re.compile(r"^(讲|讲述|讲一讲|说|说说|记录|展现|呈现|聚焦)")


def sanitize_input(text: str) -> str:
    """把用户的口语指令洗成干净的创作主题，再交给模型。

    做法：先全局去掉指令性词块，再按标点切段、丢掉空洞片段。

    例："给我的家乡方城烩面做一条 15 秒宣传片" → "家乡方城烩面"
        "帮我生成一个 30 秒的视频，主题是城市夜景" → "城市夜景"
        "做条 60s 短片，讲一位老木匠的故事"    → "一位老木匠的故事"
    """
    raw = (text or "").strip()
    # **长输入不清洗**。清洗是为"给我做一条 15 秒方城烩面宣传片"这类**口语指令**
    # 设计的：删时长词、删数字、按标点切段、丢掉短片段、无空格拼接。
    # 但用户如果直接给的是**画面描述**（"暖色厨房，手挑面条，蒸汽弥漫，木纹桌面"），
    # 这一套会把标点和细节全删掉、词还粘在一起——信息损失远大于收益。
    # 判据两条，命中任一就原样返回：
    #   ① 长度 > 40 字（长句多半是描述）
    #   ② 逗号 ≥ 2 个（"暖色调的厨房，手挑面条，蒸汽弥漫" 这种列举式描述）
    # 只看长度不够：34 字的描述照样会被洗成粘连词串（实测踩过）。
    if len(raw) > 40 or raw.count("，") + raw.count(",") >= 2:
        return raw
    t = raw
    for pat in DURATION_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE)
    for pat in INSTRUCTION_CHUNKS:
        t = re.sub(pat, " ", t)
    t = re.sub(r"[，。！？；：、,.!?;:]+", " ", t)
    t = re.sub(r"\d+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    kept: list[str] = []
    for seg in t.split(" "):
        # 去掉首尾的体裁词/结构助词，但保留词内部的内容（如"非遗"）
        seg = re.sub(r"^(短?视频|短片|片|的)+", "", seg)
        seg = re.sub(r"(短?视频|短片|文案|样片|的)+$", "", seg)
        seg = NARRATION_VERBS.sub("", seg.strip(" 的了")).strip(" 的了")
        if len(seg) >= 2:
            kept.append(seg)
    out = "".join(kept).strip(" 的了")
    return out or raw


def clean_theme(text: str) -> str:
    """把一句话压成短主题词，去掉客套与标点。"""
    t = sanitize_input(text)
    for junk in ("我的", "我们的", "一下", "一点"):
        t = t.replace(junk, "")
    return t.strip(" 的了") or (text or "").strip()[:12] or "未命名"


META_SUFFIX = ("故事", "传说", "纪录", "记录", "片子", "宣传", "介绍", "之旅", "一天")
# 不能单独当"主体"的词（量词/称谓/泛指）
BAD_SUBJECTS = ("一位", "一个", "我的", "家乡", "故事", "他们", "我们", "这个", "那个", "一份", "一段")
# 地名标记：命中才认为"地点"槽位真的表示地点
PLACE_MARKERS = (
    "省", "市", "县", "区", "镇", "村", "乡", "街", "路", "巷", "城", "州", "府", "关",
    "山", "河", "湖", "江", "海", "岛", "湾", "港", "原", "坊", "庄", "屯", "堡", "集", "口", "桥",
)
PERSON_SUFFIX = ("匠", "人", "者", "师", "家", "员", "手", "工", "婆", "公")


def place_like(word: str) -> bool:
    """判断某个词能不能当"地点"拼进句式。

    "方城""南京""古镇" → True；"老木""一位" → False（它们是主体的修饰语，
    拼进"X人"会得到"老木人"这种句子，或与主体重复成"老木老木匠"）。
    """
    w = (word or "").strip()
    if len(w) < 2 or w in BAD_SUBJECTS:
        return False
    if any(m in w for m in PLACE_MARKERS):
        return True
    if w.endswith("家乡"):
        return True
    return not any(w.endswith(s) for s in PERSON_SUFFIX)


def usable_place(place: str, subject: str) -> bool:
    """地点槽位只有在**与主体不重叠**时才能用。

    "方城" vs "烩面" → 可用；"老木" vs "老木匠" → 不可用（会拼出"老木的老木匠"）。
    判据是字符重叠，比单纯的地名判断更稳：主体是从主题里切出来的，
    只要切出来的"地点"和它有共同字，就说明这次切分不可靠。
    """
    p, s = (place or "").strip(), (subject or "").strip()
    if not place_like(p) or not p or not s:
        return False
    return not (set(p) & set(s))


def theme_tokens(theme: str) -> dict[str, str]:
    """从主题里抠出可以填进句式的槽位：地点 + 主体。

    "方城烩面"        → 地点=方城，主体=烩面
    "一位老木匠的故事" → 地点=""（不是地名），主体=老木匠
    "城市夜景"        → 地点=城市，主体=夜景
    """
    t = clean_theme(theme)
    # 带"的"时：取"的"前的名词短语当主体（"一位老木匠的故事" → "老木匠"）
    if "的" in t:
        head = t.split("的")[0]
        head = re.sub(r"^[一二三四五六七八九十百]+", "", head)
        head = re.sub(r"^(这|那)?(位|个|名|家|座|条|只|群|对|份|段)", "", head).strip()
        if len(head) >= 2:
            subject = _trim_meta(head)
            candidate = head[:2]
            return {"theme": t, "place": candidate if usable_place(candidate, subject) else "", "subject": subject}

    body = t
    for suffix in META_SUFFIX:
        if body.endswith(suffix) and len(body) > len(suffix) + 1:
            body = body[: -len(suffix)]
            break
    # 去掉开头的数词 + 量词（"一处古建筑" → "古建筑"）。
    # 不去的话会切出 place="一处古"，拼出"一处古的建筑"这种病句。
    body = re.sub(r"^[一二三四五六七八九十百]+(处|位|个|名|家|座|条|只|群|对|份|段|片|道|句)?", "", body) or t
    if len(body) <= 2:
        return {"theme": t, "place": body, "subject": body}
    if len(body) <= 6:
        place, subject = body[:-2], body[-2:]
    else:
        place, subject = body[: len(body) - 4], body[len(body) - 4 :]
    if not usable_place(place, subject) or place == subject:
        # 切不出可靠地点，主体就用整段（"古建筑" 而不是只剩 "建筑"）
        place, subject = "", body
    return {"theme": t, "place": place, "subject": subject}


def _trim_meta(word: str) -> str:
    w = word
    for suffix in META_SUFFIX:
        if w.endswith(suffix) and len(w) > len(suffix) + 1:
            w = w[: -len(suffix)]
    return w.strip() or word
