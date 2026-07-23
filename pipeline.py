"""
小红书房源视频自动化流程 - 原型脚本
=================================================
输入：
  segments/ 文件夹里放按房间顺序命名的【无声】walkthrough素材
  例如: segments/01-厨房.mov, segments/02-客厅.mov, segments/03-卧室.mov
  config.json 里放房源信息（参考 config_example.json）

输出：
  output/final.mp4          带AI配音+字幕的成片（9:16竖屏，适配小红书）
  output/post_caption.txt   AI生成的小红书文案（标题+正文+话题标签）

安装依赖：
  pip install anthropic edge-tts
  brew install ffmpeg      # Mac
  # 或 sudo apt install ffmpeg   # Linux/WSL

需要设置环境变量：
  export ANTHROPIC_API_KEY=你的key

运行：
  python pipeline.py
"""

import os
import re
import json
import glob
import asyncio
import subprocess
from pathlib import Path

import anthropic
import edge_tts

# ---------- 配置 ----------
SEGMENTS_DIR = "segments"
OUTPUT_DIR = "output"
CONFIG_PATH = "config.json"

# 免费的中文女声（微软Edge TTS，效果不错，不需要额外付费）
# 想用她本人的声音克隆，把 tts_segment_sync 换成文件底部注释里的 ElevenLabs 版本
VOICE = "zh-CN-XiaoxiaoNeural"

client = anthropic.Anthropic()  # 自动读取 ANTHROPIC_API_KEY 环境变量


# ---------- 第一步：AI 生成分房间讲解文案 + 小红书发布文案 ----------

def room_name_from_filename(path: str) -> str:
    """01-厨房.mov -> 厨房"""
    stem = Path(path).stem
    return re.sub(r"^\d+[-_]", "", stem)


def generate_script(property_info: dict, room_names: list) -> dict:
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


# ---------- 第二步：文字转语音 (TTS) ----------

async def _tts_segment(text: str, out_path: str, voice: str = VOICE):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def tts_segment_sync(text: str, out_path: str):
    asyncio.run(_tts_segment(text, out_path))


# ---------- 第三步：ffmpeg 工具函数 ----------

def get_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(out.stdout.strip())


def adjust_video_to_duration(video_path: str, target_duration: float, out_path: str):
    """把无声视频片段的时长调整到跟配音时长一致（轻微加速/减速）"""
    orig = get_duration(video_path)
    speed = orig / target_duration
    speed = max(0.5, min(speed, 2.0))  # 限制幅度，避免看起来太快/太慢
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
    """简单版：整句作为一条字幕。想要逐字/分句同步，可解析 edge-tts 的 SubMaker 时间戳"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("1\n")
        f.write(f"00:00:00,000 --> {ms_to_srt_time(int(duration * 1000))}\n")
        f.write(text + "\n")


def merge_segment(video_path: str, audio_path: str, srt_path: str, out_path: str):
    """合成单片段：视频+配音+烧录字幕，统一裁成9:16竖屏"""
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


def concat_segments(segment_paths: list, out_path: str):
    list_file = os.path.join(OUTPUT_DIR, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", out_path
    ], check=True, capture_output=True)


# ---------- 主流程 ----------

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    property_info = json.load(open(CONFIG_PATH, encoding="utf-8"))
    video_files = sorted(glob.glob(os.path.join(SEGMENTS_DIR, "*")))
    if not video_files:
        print(f"⚠️ {SEGMENTS_DIR}/ 里没有找到视频文件，请先放入按房间命名的素材")
        return

    room_names = [room_name_from_filename(p) for p in video_files]

    print("→ AI 生成讲解文案 + 小红书文案...")
    script = generate_script(property_info, room_names)

    segment_outputs = []
    for video_path, room in zip(video_files, room_names):
        text = script["segments"].get(room, "")
        if not text:
            print(f"⚠️ 没有找到「{room}」对应的文案，跳过这段")
            continue

        base = os.path.join(OUTPUT_DIR, room)
        audio_path = base + ".mp3"
        srt_path = base + ".srt"
        adj_video_path = base + "_adj.mp4"
        seg_out_path = base + "_final.mp4"

        print(f"  - {room}: 生成配音...")
        tts_segment_sync(text, audio_path)
        duration = get_duration(audio_path)

        make_srt(text, duration, srt_path)
        adjust_video_to_duration(video_path, duration, adj_video_path)
        merge_segment(adj_video_path, audio_path, srt_path, seg_out_path)

        segment_outputs.append(seg_out_path)

    print("→ 拼接所有片段...")
    final_path = os.path.join(OUTPUT_DIR, "final.mp4")
    concat_segments(segment_outputs, final_path)

    caption_path = os.path.join(OUTPUT_DIR, "post_caption.txt")
    with open(caption_path, "w", encoding="utf-8") as f:
        f.write(script["post_title"] + "\n\n")
        f.write(script["post_body"] + "\n\n")
        f.write(" ".join(script["hashtags"]))

    print(f"✅ 成片：{final_path}")
    print(f"✅ 小红书文案：{caption_path}")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------
# 想用她本人声音克隆（而不是免费的通用女声），用下面这个函数替换
# tts_segment_sync，并在 main() 里改用它：
#
# def tts_segment_elevenlabs(text, out_path, voice_id, api_key):
#     import requests
#     r = requests.post(
#         f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
#         headers={"xi-api-key": api_key},
#         json={"text": text, "model_id": "eleven_multilingual_v2"},
#     )
#     with open(out_path, "wb") as f:
#         f.write(r.content)
#
# voice_id 需要先在 ElevenLabs 后台用她的一段录音样本做声音克隆得到
# ---------------------------------------------------------------
