"""
Reeltour - 房源视频生成器（网页版）
=================================================
给她用的界面：上传素材 → （如果是一镜到底，AI自动分段）→ 填讲解文案 → 点生成 →
看成片+下载文案

本地运行（开发/测试用）：
  pip install -r requirements.txt
  export ANTHROPIC_API_KEY=你的key
  streamlit run app.py
"""

import os
import re
import json
import glob
import base64
import shutil
import tempfile
import subprocess
import xml.sax.saxutils as saxutils
from pathlib import Path

import requests
import streamlit as st
import anthropic

VOICE_OPTIONS = {
    "晓晓 - 温暖女声（默认）": "zh-CN-XiaoxiaoNeural",
    "晓伊 - 活泼女声": "zh-CN-XiaoyiNeural",
    "云希 - 自然男声": "zh-CN-YunxiNeural",
    "晓墨 - 成熟女声": "zh-CN-XiaomoNeural",
}

FRAME_INTERVAL = 4.0  # 自动分段时，每隔几秒抽一帧画面给AI判断房间类型

st.set_page_config(page_title="Reeltour", page_icon="🎬", layout="centered")


# ---------- 核心逻辑 ----------

def get_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("没有配置 ANTHROPIC_API_KEY，请联系开发者设置")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


def extract_text(resp) -> str:
    """从API响应里取出真正的文字内容。较新的模型可能会先返回一个思考过程的block，
    不一定第一个block就是文字答案，所以要遍历找类型是text的那个，不能直接取 content[0]"""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError(
        f"API响应里没有找到文字内容（stop_reason={resp.stop_reason}，"
        f"可能是max_tokens不够、内容在思考过程里被截断了，试试调大max_tokens）"
    )


def generate_script(property_info: dict, room_names: list) -> dict:
    client = get_client()
    prompt = f"""你是一个小红书房产博主，风格是那种跟朋友唠嗑一样自然、有点小兴奋的语气，
不是地产中介的官方话术。请根据以下房源信息，为一条房源walkthrough视频写分房间的口播讲解词，
以及一段小红书笔记文案。

房源信息：
{json.dumps(property_info, ensure_ascii=False, indent=2)}

视频镜头顺序（按房间）：{room_names}

讲解词的要求（很重要，照着做）：
1. 就当你自己拿着手机边走边跟朋友说话，用短句，可以有语气词（"你看"、"我跟你说"、"绝了"这种），
   不要用"该房间"、"本户型"、"总体而言"这类书面语/中介腔
2. 每个房间1句话就够，最多2句，别堆砌形容词，挑1个最有记忆点的细节说
3. 反例（不要这样写）："本厨房配备大理石岛台，动线合理，采光充足"
   正例（要这样写）："厨房这个岛台是真的大，一家人围着做饭聊天完全没问题"
4. 每句话控制在能5-8秒读完的长度（大概15-25个字）

最后单独写小红书发布文案：标题(吸引点击，可带emoji，别太夸张) + 正文(3-5句，呼应视频内容，
口语化，结尾可以带一句行动号召比如"想看详细信息评论区戳我") + 5个相关话题标签

严格按以下JSON格式输出，不要有任何其他文字或markdown代码块标记：
{{
  "segments": {{"房间名1": "讲解词1", "房间名2": "讲解词2"}},
  "post_title": "...",
  "post_body": "...",
  "hashtags": ["#...", "#...", "#...", "#...", "#..."]
}}
"""
    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = extract_text(resp).strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text)
    return json.loads(text)


# ---------- AI自动分段（一镜到底模式用） ----------

def extract_frames(video_path: str, workdir: str, interval: float = FRAME_INTERVAL):
    frames_dir = os.path.join(workdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps=1/{interval}", "-q:v", "3",
        os.path.join(frames_dir, "f_%04d.jpg")
    ], check=True, capture_output=True)
    files = sorted(glob.glob(os.path.join(frames_dir, "f_*.jpg")))
    return [(i * interval, path) for i, path in enumerate(files)]


def detect_room_segments(video_path: str, workdir: str) -> list:
    """用AI看抽出来的画面帧，自动判断每一段是哪个房间，返回
    [{"room": "厨房", "start": 0.0, "end": 12.0}, ...]"""
    client = get_client()
    frames = extract_frames(video_path, workdir)
    if not frames:
        return []
    # 免费实例资源有限，帧数太多容易超时/爆内存，限制一下最多分析的帧数
    frames = frames[:60]

    content = [{
        "type": "text",
        "text": (
            f"以下是一段房源walkthrough视频按固定间隔抽取的{len(frames)}张画面截图，"
            "按时间顺序排列，第一张对应第0秒。请判断每一张截图所在的空间类型（例如："
            "玄关、客厅、厨房、卧室、浴室、走廊、储藏室、后院、其他），相邻画面如果明显"
            "是同一个空间应该标同一个标签，不要因为镜头轻微晃动/角度变化就换标签，尽量"
            f"减少不必要的切换。\n\n"
            f"必须严格按顺序返回一个长度为{len(frames)}的JSON数组，"
            "第i个元素对应第i张图（从0开始数），不要输出其他文字，格式：\n"
            '[{"room": "客厅"}, {"room": "客厅"}, {"room": "厨房"}]'
        )
    }]
    for i, (t, path) in enumerate(frames):
        img_b64 = base64.b64encode(open(path, "rb").read()).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
        })

    resp = client.messages.create(
        model="claude-sonnet-5", max_tokens=6000,
        messages=[{"role": "user", "content": content}],
    )
    text = extract_text(resp).strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text)
    labels = json.loads(text)

    # 不用AI返回的"index"字段去反查frames（AI偶尔会数错张数导致越界），
    # 而是直接按labels列表本身的顺序对应frames列表的顺序——两者理论上是一一对应的
    segments = []
    for i, item in enumerate(labels):
        if i >= len(frames):
            break
        room = item.get("room", "未知空间")
        t = frames[i][0]
        if segments and segments[-1]["room"] == room:
            segments[-1]["end"] = round(t + FRAME_INTERVAL, 1)
        else:
            segments.append({"room": room, "start": round(t, 1), "end": round(t + FRAME_INTERVAL, 1)})
    return segments


def cut_clip(video_path: str, start: float, end: float, out_path: str):
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-ss", str(start), "-to", str(end),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-an", out_path
    ], check=True, capture_output=True)


# ---------- TTS：改用Azure官方语音合成服务（跟edge-tts是同一批声音，但走官方稳定接口） ----------

def get_azure_config():
    key = os.environ.get("AZURE_SPEECH_KEY")
    region = os.environ.get("AZURE_SPEECH_REGION")
    if not key or not region:
        st.error("没有配置 AZURE_SPEECH_KEY / AZURE_SPEECH_REGION，请联系开发者设置")
        st.stop()
    return key, region


def tts_segment_sync(text: str, out_path: str, voice: str):
    key, region = get_azure_config()
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "reeltour",
    }
    ssml = (
        "<speak version='1.0' xml:lang='zh-CN'>"
        f"<voice xml:lang='zh-CN' name='{voice}'>{saxutils.escape(text)}</voice>"
        "</speak>"
    )
    resp = requests.post(url, headers=headers, data=ssml.encode("utf-8"), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Azure TTS请求失败（状态码 {resp.status_code}）：{resp.text[:300]}")
    with open(out_path, "wb") as f:
        f.write(resp.content)


def get_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(out.stdout.strip())


def merge_segment(video_path: str, audio_path: str, srt_path: str, out_path: str):
    """一次编码完成：调速对齐配音时长 + 缩放裁剪9:16 + 烧录中文字幕"""
    orig = get_duration(video_path)
    target = get_duration(audio_path)
    speed = max(0.5, min(orig / target, 2.0))

    vf = (
        f"setpts={1/speed}*PTS,"
        "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:1920,"
        f"subtitles={srt_path}:force_style='FontName=WenQuanYi Zen Hei,FontSize=20,"
        f"PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2,"
        f"Alignment=2,MarginV=80'"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac",
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest", out_path
    ], check=True, capture_output=True)


def ms_to_srt_time(ms: int) -> str:
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_srt(text: str, duration: float, out_path: str):
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("1\n")
        f.write(f"00:00:00,000 --> {ms_to_srt_time(int(duration * 1000))}\n")
        f.write(text + "\n")


def concat_segments(segment_paths: list, out_path: str, workdir: str):
    list_file = os.path.join(workdir, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", out_path
    ], check=True, capture_output=True)


def run_pipeline(clip_paths, room_names, script, voice, workdir, progress_cb):
    """clip_paths 和 room_names 一一对应，clip_paths 是已经切好的单房间无声视频文件路径
    （不管是手动按房间上传的，还是自动分段切出来的，走到这里都是一样的）"""
    segment_outputs = []
    skipped_rooms = []
    n = len(clip_paths)
    for i, (video_path, room) in enumerate(zip(clip_paths, room_names)):
        text = (script["segments"].get(room, "") or "").strip()
        if not text:
            skipped_rooms.append(room)
            continue

        base = os.path.join(workdir, f"seg{i}_{re.sub(r'[^0-9a-zA-Z_一-龥]', '_', room)}")
        audio_path = base + ".mp3"
        srt_path = base + ".srt"
        seg_out_path = base + "_final.mp4"

        progress_cb(f"正在处理「{room}」...", 0.1 + 0.7 * (i / n))

        try:
            tts_segment_sync(text, audio_path, voice)
        except Exception:
            # edge-tts偶尔会因为网络问题临时失败，重试一次再放弃
            try:
                tts_segment_sync(text, audio_path, voice)
            except Exception as e:
                raise RuntimeError(f"「{room}」配音生成失败（文案：{text!r}）：{e}") from e

        duration = get_duration(audio_path)
        make_srt(text, duration, srt_path)
        merge_segment(video_path, audio_path, srt_path, seg_out_path)
        segment_outputs.append(seg_out_path)

    if skipped_rooms:
        st.warning(f"这几个房间没有讲解词，已跳过：{', '.join(skipped_rooms)}")

    if not segment_outputs:
        raise RuntimeError("所有房间都没有有效的讲解词，没有内容可以生成")

    progress_cb("正在拼接成片...", 0.85)
    final_path = os.path.join(workdir, "final.mp4")
    concat_segments(segment_outputs, final_path, workdir)

    caption_text = (
        script.get("post_title", "") + "\n\n" +
        script.get("post_body", "") + "\n\n" +
        " ".join(script.get("hashtags", []))
    )

    progress_cb("完成！", 1.0)
    return final_path, caption_text


# ---------- 网页界面 ----------

st.title("🎬 Reeltour")
st.caption("上传素材，AI帮你生成带配音+字幕的成片和小红书文案")

st.subheader("① 上传视频素材")
upload_mode = st.radio(
    "拍摄方式",
    ["已经按房间分开拍摄（每段一个文件）", "一镜到底拍完整套房子，AI自动分段（测试中）"],
)

def dedupe_names(names: list) -> list:
    """房间名可能重复（比如AI分段识别出两段都叫"走廊"，或者手动模式两个文件都手打了同样的名字）。
    重复的话给后面的自动加个编号，避免：1) Streamlit组件key冲突报错 2) 用房间名当字典key时后面的
    悄悄覆盖前面的数据"""
    seen = {}
    result = []
    for n in names:
        n = n or "未命名"
        if n not in seen:
            seen[n] = 1
            result.append(n)
        else:
            seen[n] += 1
            result.append(f"{n}({seen[n]})")
    return result


room_names = []
manual_clip_paths = {}  # 手动模式：{房间名: 上传的文件对象}

if upload_mode.startswith("已经"):
    uploaded_files = st.file_uploader(
        "选择视频文件（可多选）", type=["mov", "mp4"], accept_multiple_files=True
    )
    if uploaded_files:
        st.write("给每段素材标一下房间/区域名称：")
        raw_names = []
        for i, f in enumerate(uploaded_files):
            default_guess = re.sub(r"^\d+[-_]", "", Path(f.name).stem)
            room = st.text_input(f"素材 {i+1}（{f.name}）", value=default_guess, key=f"room_{i}")
            raw_names.append(room)
        room_names = dedupe_names(raw_names)
        for room, f in zip(room_names, uploaded_files):
            manual_clip_paths[room] = f
else:
    uploaded_files = None
    single_file = st.file_uploader("上传完整walkthrough视频（无声）", type=["mov", "mp4"])
    if single_file:
        if st.session_state.get("auto_file_name") != single_file.name:
            # 新文件，重置session state
            st.session_state.auto_workdir = tempfile.mkdtemp()
            video_path = os.path.join(st.session_state.auto_workdir, "full" + Path(single_file.name).suffix)
            with open(video_path, "wb") as f:
                f.write(single_file.getbuffer())
            st.session_state.auto_video_path = video_path
            st.session_state.auto_file_name = single_file.name
            st.session_state.auto_segments = None

        if st.button("🔍 AI 识别房间分段"):
            with st.spinner("正在抽取画面帧、识别房间中，可能要一会儿..."):
                try:
                    segments = detect_room_segments(
                        st.session_state.auto_video_path, st.session_state.auto_workdir
                    )
                    st.session_state.auto_segments = segments
                except Exception as e:
                    st.error(f"识别失败：{e}")

        if st.session_state.get("auto_segments"):
            st.write("识别结果，检查一下，不对的话可以直接改：")
            edited = st.data_editor(
                st.session_state.auto_segments,
                num_rows="dynamic",
                column_config={
                    "room": st.column_config.TextColumn("房间"),
                    "start": st.column_config.NumberColumn("开始(秒)"),
                    "end": st.column_config.NumberColumn("结束(秒)"),
                },
                key="segments_editor",
            )
            st.session_state.auto_segments = edited
            # 不在这里过滤掉空房间名的行，否则room_names的长度会跟后面切片段用的
            # auto_segments对不上，统一交给dedupe_names处理空值
            room_names = dedupe_names([seg.get("room", "") for seg in edited])

mode = st.radio(
    "② 讲解文案怎么来",
    ["手动输入（免费，测试用）", "AI自动生成（需要 API key，正式使用推荐）"],
    help="测试阶段建议先用手动输入，不需要配置任何API key",
)
use_ai = mode.startswith("AI")

property_info = {}
manual_segments = {}

if use_ai:
    st.subheader("③ 房源信息")
    property_info = {
        "地址": st.text_input("地址/小区名"),
        "户型": st.text_input("户型（例如：3室2卫）"),
        "面积": st.text_input("面积"),
        "价格区间": st.text_input("价格区间"),
        "亮点": [h.strip() for h in st.text_area(
            "亮点（每行一个，例如：厨房岛台大 / 采光好 / 近学校）").split("\n") if h.strip()],
        "目标客群": st.text_input("目标客群（可选）"),
    }
else:
    st.subheader("③ 每个房间自己写一句讲解词")
    if room_names:
        for room in room_names:
            manual_segments[room] = st.text_area(f"「{room}」的讲解词", key=f"seg_{room}")
    st.subheader("小红书文案（可选，留空也行）")
    manual_title = st.text_input("标题")
    manual_body = st.text_area("正文")
    manual_hashtags = st.text_input("话题标签（空格分隔，例如：#卡尔加里买房 #首次购房）")

st.subheader("④ 配音音色")
voice_label = st.selectbox("选一个试试，音质有差异，多试几个", list(VOICE_OPTIONS.keys()))
selected_voice = VOICE_OPTIONS[voice_label]
st.caption("这几个是Azure官方语音合成的音色（每月有50万字符免费额度，超出后按量计费，很便宜），比免费hack方案稳定；想要完全自然、像真人的声音，需要声音克隆（额外付费），可以后面再升级")

st.subheader("⑤ 生成")
can_generate = bool(room_names)
if st.button("🚀 生成视频和文案", type="primary", disabled=not can_generate):
    workdir = tempfile.mkdtemp()
    progress_bar = st.progress(0.0)
    status = st.empty()

    def progress_cb(msg, pct):
        status.write(msg)
        progress_bar.progress(pct)

    try:
        # 准备好每个房间对应的无声视频片段路径
        clip_paths = []
        if upload_mode.startswith("已经"):
            for room in room_names:
                f = manual_clip_paths[room]
                path = os.path.join(workdir, f"{room}{Path(f.name).suffix}")
                with open(path, "wb") as out:
                    out.write(f.getbuffer())
                clip_paths.append(path)
        else:
            progress_cb("正在按识别结果切分视频...", 0.05)
            for i, seg in enumerate(st.session_state.auto_segments):
                clip_path = os.path.join(workdir, f"clip{i}.mp4")
                cut_clip(st.session_state.auto_video_path, seg["start"], seg["end"], clip_path)
                clip_paths.append(clip_path)

        if use_ai:
            progress_cb("AI 正在生成讲解文案...", 0.08)
            script = generate_script(property_info, room_names)
        else:
            script = {
                "segments": manual_segments,
                "post_title": manual_title,
                "post_body": manual_body,
                "hashtags": manual_hashtags.split() if manual_hashtags else [],
            }

        final_path, caption_text = run_pipeline(
            clip_paths, room_names, script, selected_voice, workdir, progress_cb
        )

        st.success("生成完成！Review一下，满意的话就去小红书发布")
        st.video(final_path)

        with open(final_path, "rb") as f:
            st.download_button("⬇️ 下载视频", f, file_name="final.mp4", mime="video/mp4")

        st.subheader("小红书文案")
        st.text_area("可以直接复制", caption_text, height=200)

    except Exception as e:
        st.error(f"处理过程中出错了：{e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
