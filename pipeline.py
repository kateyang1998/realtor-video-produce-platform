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
  pip install anthropic requests
  brew install ffmpeg      # Mac
  # 或 sudo apt install ffmpeg   # Linux/WSL

需要设置环境变量：
  export ANTHROPIC_API_KEY=你的key
  export AZURE_SPEECH_KEY=你的Azure语音服务key
  export AZURE_SPEECH_REGION=你的Azure区域（例如 eastus）

运行：
  python pipeline.py
"""

import os
import re
import json
import glob
import subprocess
import xml.sax.saxutils as saxutils
from pathlib import Path

import requests
import anthropic

# ---------- 配置 ----------
SEGMENTS_DIR = "segments"
OUTPUT_DIR = "output"
CONFIG_PATH = "config.json"

# Azure官方语音合成的音色（跟edge-tts是同一批声音，但走官方稳定接口）
VOICE = "zh-CN-XiaoxiaoNeural"

client = anthropic.Anthropic()  # 自动读取 ANTHROPIC_API_KEY 环境变量


def extract_text(resp) -> str:
    """从API响应里取出真正的文字内容，不能直接假设content[0]就是文字block"""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError(
        f"API响应里没有找到文字内容（stop_reason={resp.stop_reason}，"
        f"可能是max_tokens不够，试试调大max_tokens）"
    )


# ---------- 第一步：AI 生成分房间讲解文案 + 小红书发布文案 ----------

def room_name_from_filename(path: str) -> str:
    """01-厨房.mov -> 厨房"""
    stem = Path(path).stem
    return re.sub(r"^\d+[-_]", "", stem)


def generate_script(property_info: dict, room_names: list) -> dict:
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


# ---------- 第二步：文字转语音 (Azure官方语音合成) ----------

def tts_segment_sync(text: str, out_path: str, voice: str = VOICE):
    key = os.environ.get("AZURE_SPEECH_KEY")
    region = os.environ.get("AZURE_SPEECH_REGION")
    if not key or not region:
        raise RuntimeError("没有配置 AZURE_SPEECH_KEY / AZURE_SPEECH_REGION 环境变量")

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


# ---------- 第三步：ffmpeg 工具函数 ----------

def get_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(out.stdout.strip())


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
    """一次编码完成：缩放裁剪9:16 + 烧录中文字幕，画面正常速度播放（不裁剪不加速）

    看房视频画面完整播完比"卡着配音时长"更重要，所以谁长就以谁为准：配音说完了
    画面还没放完就正常放到结束；画面放完了配音还没说完就定格最后一帧等配音说完"""
    orig = get_duration(video_path)
    target = get_duration(audio_path)
    duration = max(orig, target)

    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:1920,"
        f"tpad=stop_mode=clone:stop_duration={max(0.0, duration - orig)},"
        f"subtitles={srt_path}:original_size=1080x1920:force_style='FontName=WenQuanYi Zen Hei,FontSize=15,"
        f"PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2,"
        f"Alignment=2,MarginV=80'"
    )
    af = f"apad=pad_dur={max(0.0, duration - target)}"
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
        "-vf", vf,
        "-af", af,
        "-t", str(duration),
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac",
        "-map", "0:v:0", "-map", "1:a:0",
        out_path
    ], check=True, capture_output=True)


def concat_segments(segment_paths: list, out_path: str):
    """每段视频已经在merge_segment里统一强制了帧率，时间戳兼容，可以用省资源的
    stream copy拼接，不用整体重新编码"""
    list_file = os.path.join(OUTPUT_DIR, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        out_path
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
        seg_out_path = base + "_final.mp4"

        print(f"  - {room}: 生成配音...")
        tts_segment_sync(text, audio_path)
        duration = get_duration(audio_path)

        make_srt(text, duration, srt_path)
        merge_segment(video_path, audio_path, srt_path, seg_out_path)

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
