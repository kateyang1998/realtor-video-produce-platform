# Reeltour

Turn a folder of silent real estate walkthrough clips into a ready-to-post
short-form video — AI-generated narration, synced captions, and a
platform-style caption (title + body + hashtags) — with a simple web UI.

Built for solo real estate agents who shoot raw footage but don't have
time (or a team) to script, voice, edit, and caption a video for every
listing.

## How it works

1. **You upload** silent video clips, one per room, named in shooting
   order (e.g. `01-kitchen.mov`, `02-living-room.mov`).
2. **You fill in** a few basic facts about the listing (address, layout,
   size, price range, highlights).
3. **The app**:
   - generates a short narration line per room + a social post caption,
     styled for short-form video (via the Claude API)
   - converts the narration to speech (via free Microsoft Edge TTS by
     default)
   - stretches/compresses each silent clip to match its narration length
   - burns in synced captions
   - concatenates everything into a single 9:16 vertical video
4. **You review** the result in the browser and download it. Publishing
   is a manual, deliberate step — this tool does not auto-post anywhere.

## Why a human stays in the loop

This is intentionally *not* a fully hands-off, auto-publish pipeline.
Two reasons:
- Real estate advertising is subject to fair-housing / advertising
  regulations in most jurisdictions — an unreviewed AI caption going
  straight to a public account is a real risk.
- Most social platforms (especially outside the US) don't offer a safe,
  official API for individual creators to auto-publish. Unofficial
  workarounds risk account suspension.

So the app stops at "here's your finished video and caption, ready for
you to post" — the actual publish click is yours.

## Project structure

```
app.py                Streamlit web UI (what you actually run/deploy)
pipeline.py            Same logic as a CLI script, useful for local testing
config_example.json    Example listing-info input
requirements.txt        Python dependencies
packages.txt            System dependency (ffmpeg) for Streamlit Cloud
```

## Local setup

```bash
pip install -r requirements.txt
brew install ffmpeg        # macOS
# sudo apt install ffmpeg  # Debian/Ubuntu

export ANTHROPIC_API_KEY=your-key-here

streamlit run app.py
```

This opens a local URL in your browser where you can test the full flow
before deploying it anywhere.

## Deploying so anyone can use it from a browser

The easiest free option is [Streamlit Community Cloud](https://streamlit.io/cloud):

1. Push this repo to GitHub (public or private both work — see note below).
2. On Streamlit Community Cloud, create a new app, point it at this repo,
   and set the main file to `app.py`.
3. In "Advanced settings" → Secrets, add:
   ```
   ANTHROPIC_API_KEY = "your-key-here"
   ```
4. Deploy. You'll get a `your-chosen-name.streamlit.app` URL you can share.

**Note on the API key and public repos:** the key is never committed to
this repository — it's injected at deploy time through Streamlit's
Secrets manager. That means it's safe to keep this repo public even
though the app itself uses a paid API key behind the scenes.

## Cost

- Claude API: roughly a few cents per generated video (one short
  generation call per listing).
- Edge TTS: free.
- Hosting on Streamlit Community Cloud: free.

Total cost scales with usage, not a flat subscription.

## Known limitations / things to improve

- Captions currently display as one block of text per room rather than
  word-by-word — word-level timing would need parsing TTS timestamp
  output.
- Room-to-clip matching relies on filename convention, not visual
  scene detection.
- The default voice is a generic TTS voice, not a cloned voice — voice
  cloning (e.g. via ElevenLabs) is possible but requires a paid API and
  a voice sample.
- Currently processes one listing at a time.
