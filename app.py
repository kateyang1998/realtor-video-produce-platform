"""
小红书房源视频自动化工具 - 网页版 (Streamlit)
=================================================
给她用的界面：上传素材 → 填房源信息 → 点生成 → 看成片+下载文案

本地运行（开发/测试用）：
  pip install streamlit anthropic edge-tts
  export ANTHROPIC_API_KEY=你的key
  streamlit run app.py

真正给她用，需要部署到服务器（见 README 里的部署说明），
部署后她只需要打开一个网址，不需要装任何东西。
"""

import os
import re
import json
import glob
import shutil
import asyncio
import tempfile
import subprocess
from pathlib import Path

import streamlit as st
import anthropic
import edge_tts

VOICE_OPTIONS = {
    "晓晓 - 温暖女声（默认）": "zh-CN-XiaoxiaoNeural",
    "晓伊 - 活泼女声": "zh-CN-XiaoyiNeural",
    "云希 - 自然男声": "zh-CN-YunxiNeural",
    "晓墨 - 成熟女声": "zh-CN-XiaomoNeural",
}

st.set_page_config(page_title="房源视频生成器", page_icon="🎬", layout="centered")


# ---------- 核心逻辑（跟 pipeline.py 一致，改成函数式方便网页调用） ----------

def get_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("没有配置 ANTHROPIC_API_KEY，请联系开发者设置")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


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
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text)
    return json.loads(text)


async def _tts_segment(text: str, out_path: str, voice: str):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def tts_segment_sync(text: str, out_path: str, voice: str):
    asyncio.run(_tts_segment(text, out_path, voice))


def get_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(out.stdout.strip())


def merge_segment(video_path: str, audio_path: str, srt_path: str, out_path: str):
    """一次编码完成：调速对齐配音时长 + 缩放裁剪9:16 + 烧录中文字幕
    （之前是调速一次编码、烧字幕再编码一次，两次压缩会明显损失画质，合并成一次）"""
    orig = get_duration(video_path)
    target = get_duration(audio_path)
    speed = max(0.5, min(orig / target, 2.0))  # 限制调速幅度，避免看起来过快/过慢

    vf = (
        f"setpts={1/speed}*PTS,"
        "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:1920,"
        f"subtitles={srt_path}:force_style='FontName=Noto Sans CJK SC,FontSize=20,"
        f"PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2,"
        f"Alignment=2,MarginV=80'"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
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


def run_pipeline(uploaded_files, room_names, script, voice, workdir, progress_cb):
    """script 是一个已经准备好的 dict：{"segments": {房间: 文案}, "post_title": ..., "post_body": ..., "hashtags": [...]}
    可以来自 generate_script()（AI生成），也可以是手动填写拼出来的——这个函数不关心来源"""
    video_files = []
    for f, room in zip(uploaded_files, room_names):
        path = os.path.join(workdir, f"{room}{Path(f.name).suffix}")
        with open(path, "wb") as out:
            out.write(f.getbuffer())
        video_files.append(path)

    segment_outputs = []
    n = len(video_files)
    for i, (video_path, room) in enumerate(zip(video_files, room_names)):
        text = script["segments"].get(room, "")
        if not text:
            continue

        base = os.path.join(workdir, room)
        audio_path = base + ".mp3"
        srt_path = base + ".srt"
        seg_out_path = base + "_final.mp4"

        progress_cb(f"正在处理「{room}」...", 0.1 + 0.7 * (i / n))
        tts_segment_sync(text, audio_path, voice)
        duration = get_duration(audio_path)
        make_srt(text, duration, srt_path)
        merge_segment(video_path, audio_path, srt_path, seg_out_path)
        segment_outputs.append(seg_out_path)

    progress_cb("正在拼接成片...", 0.85)
    final_path = os.path.join(workdir, "final.mp4")
    concat_segments(segment_outputs, final_path, workdir)

    caption_text = (
        script["post_title"] + "\n\n" +
        script["post_body"] + "\n\n" +
        " ".join(script["hashtags"])
    )

    progress_cb("完成！", 1.0)
    return final_path, caption_text


# ---------- 网页界面 ----------

st.title("🎬 房源视频生成器")
st.caption("上传素材，填一下房源信息，AI帮你生成带配音+字幕的成片和小红书文案")

st.subheader("① 上传视频素材")
st.write("按拍摄顺序上传（比如先厨房再客厅），下面会让你给每段标注房间名")
uploaded_files = st.file_uploader(
    "选择视频文件（可多选）", type=["mov", "mp4"], accept_multiple_files=True
)

room_names = []
if uploaded_files:
    st.write("给每段素材标一下房间/区域名称：")
    cols = st.columns(len(uploaded_files)) if len(uploaded_files) <= 4 else None
    for i, f in enumerate(uploaded_files):
        default_guess = re.sub(r"^\d+[-_]", "", Path(f.name).stem)
        room = st.text_input(f"素材 {i+1}（{f.name}）", value=default_guess, key=f"room_{i}")
        room_names.append(room)

mode = st.radio(
    "② 讲解文案怎么来",
    ["手动输入（免费，测试用）", "AI自动生成（需要 API key，正式使用推荐）"],
    help="测试阶段建议先用手动输入，不需要配置任何API key，先验证配音+剪辑效果",
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

st.subheader("③ 配音音色")
voice_label = st.selectbox("选一个试试，音质有差异，多试几个", list(VOICE_OPTIONS.keys()))
selected_voice = VOICE_OPTIONS[voice_label]
st.caption("这几个都是免费的通用AI音色，会有一定机器感，如果想要完全自然、像真人的声音，需要用声音克隆（额外付费），这个可以后面再升级")

st.subheader("④ 生成")
if st.button("🚀 生成视频和文案", type="primary", disabled=not uploaded_files):
    workdir = tempfile.mkdtemp()
    progress_bar = st.progress(0.0)
    status = st.empty()

    def progress_cb(msg, pct):
        status.write(msg)
        progress_bar.progress(pct)

    try:
        if use_ai:
            progress_cb("AI 正在生成讲解文案...", 0.05)
            script = generate_script(property_info, room_names)
        else:
            script = {
                "segments": manual_segments,
                "post_title": manual_title,
                "post_body": manual_body,
                "hashtags": manual_hashtags.split() if manual_hashtags else [],
            }

        final_path, caption_text = run_pipeline(
            uploaded_files, room_names, script, selected_voice, workdir, progress_cb
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
