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

VOICE = "zh-CN-XiaoxiaoNeural"

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
    prompt = f"""你是一个小红书房产博主的文案助理。请根据以下房源信息，
为一条房源walkthrough视频生成分房间的口播讲解文案，以及一段小红书笔记文案。

房源信息：
{json.dumps(property_info, ensure_ascii=False, indent=2)}

视频镜头顺序（按房间）：{room_names}

要求：
1. 给每个房间写1-2句口播讲解词，口语化、有画面感、像真人在带看，不要说明书式堆参数
2. 语气符合小红书"种草"风格，不要广告腔
3. 每句话长度适合正常语速朗读（大约5-8秒能读完）
4. 最后单独写一段小红书发布文案：标题(吸引点击，可带emoji) + 正文(3-5句，呼应视频内容+行动号召) + 5个相关话题标签

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


async def _tts_segment(text: str, out_path: str, voice: str = VOICE):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def tts_segment_sync(text: str, out_path: str):
    asyncio.run(_tts_segment(text, out_path))


def get_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(out.stdout.strip())


def adjust_video_to_duration(video_path: str, target_duration: float, out_path: str):
    orig = get_duration(video_path)
    speed = orig / target_duration
    speed = max(0.5, min(speed, 2.0))
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-filter:v", f"setpts={1/speed}*PTS",
        "-an", out_path
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


def merge_segment(video_path: str, audio_path: str, srt_path: str, out_path: str):
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"subtitles={srt_path}:force_style='FontSize=20,PrimaryColour=&HFFFFFF&,"
        f"OutlineColour=&H000000&,BorderStyle=1,Outline=2,Alignment=2,MarginV=80'"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
        "-vf", vf,
        "-c:v", "libx264", "-c:a", "aac",
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest", out_path
    ], check=True, capture_output=True)


def concat_segments(segment_paths: list, out_path: str, workdir: str):
    list_file = os.path.join(workdir, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", out_path
    ], check=True, capture_output=True)


def run_pipeline(uploaded_files, room_names, property_info, workdir, progress_cb):
    video_files = []
    for f, room in zip(uploaded_files, room_names):
        path = os.path.join(workdir, f"{room}{Path(f.name).suffix}")
        with open(path, "wb") as out:
            out.write(f.getbuffer())
        video_files.append(path)

    progress_cb("AI 正在生成讲解文案...", 0.1)
    script = generate_script(property_info, room_names)

    segment_outputs = []
    n = len(video_files)
    for i, (video_path, room) in enumerate(zip(video_files, room_names)):
        text = script["segments"].get(room, "")
        if not text:
            continue

        base = os.path.join(workdir, room)
        audio_path = base + ".mp3"
        srt_path = base + ".srt"
        adj_video_path = base + "_adj.mp4"
        seg_out_path = base + "_final.mp4"

        progress_cb(f"正在处理「{room}」...", 0.1 + 0.7 * (i / n))
        tts_segment_sync(text, audio_path)
        duration = get_duration(audio_path)
        make_srt(text, duration, srt_path)
        adjust_video_to_duration(video_path, duration, adj_video_path)
        merge_segment(adj_video_path, audio_path, srt_path, seg_out_path)
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

st.subheader("② 房源信息")
address = st.text_input("地址/小区名")
layout = st.text_input("户型（例如：3室2卫）")
size = st.text_input("面积")
price = st.text_input("价格区间")
highlights = st.text_area("亮点（每行一个，例如：厨房岛台大 / 采光好 / 近学校）")
audience = st.text_input("目标客群（可选，例如：首次购房年轻家庭）")

st.subheader("③ 生成")
if st.button("🚀 生成视频和文案", type="primary", disabled=not uploaded_files):
    property_info = {
        "地址": address,
        "户型": layout,
        "面积": size,
        "价格区间": price,
        "亮点": [h.strip() for h in highlights.split("\n") if h.strip()],
        "目标客群": audience,
    }

    workdir = tempfile.mkdtemp()
    progress_bar = st.progress(0.0)
    status = st.empty()

    def progress_cb(msg, pct):
        status.write(msg)
        progress_bar.progress(pct)

    try:
        final_path, caption_text = run_pipeline(
            uploaded_files, room_names, property_info, workdir, progress_cb
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
