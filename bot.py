# bot_capture_story.py
import os, io, json, time, asyncio, logging
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List, Tuple

from telegram import Update, Message
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

import cloudinary
import cloudinary.uploader

# ---------- Config ----------
PANEL_GAP_SEC = 25  # time window to pair a photo with following text from same user
OUTPUT_DIR = "out"
STORY_ID = "captured_story"
STORY_TITLE = "Captured Story"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Telegram + Cloudinary credentials (env)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
    secure=True,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("story-capture")

# ---------- Data Models ----------
@dataclass
class Panel:
    ts: float
    chat_id: int
    user_id: int
    speaker: Optional[str]
    text: str
    photo_url: Optional[str]

@dataclass
class StoryBundle:
    meta: Dict
    nodes: Dict[str, Dict]
    callbacks: Dict[str, str]

# ---------- State ----------
last_media_by_user: Dict[Tuple[int, int], Dict] = {}  # (chat_id, user_id) -> {"ts": float, "photo_file_id": str}
panels: List[Panel] = []

# ---------- Helpers ----------
def extract_speaker_and_body(text: str) -> Tuple[Optional[str], str]:
    lines = [ln for ln in (text or "").splitlines()]
    while lines and not lines.strip():
        lines.pop(0)
    if not lines:
        return None, ""
    first = lines.strip()
    if len(first) <= 32 and (first.lower() == "narration" or "slayer" in first.lower() or first.endswith(":")):
        return first.rstrip(": "), "\n".join(lines[1:]).lstrip()
    return None, text

async def download_photo_bytes(file_id: str, context: ContextTypes.DEFAULT_TYPE) -> bytes:
    # Telegram File object; then download_as_bytearray to get bytes
    tf = await context.bot.get_file(file_id)  # provides file_path for server link; downloading is robust. [6]
    ba = await tf.download_as_bytearray()
    return bytes(ba)

def cloudinary_upload_bytes(content: bytes, public_id_prefix: str = "story/panel") -> str:
    # Upload bytes; let Cloudinary assign a public_id suffix; return secure_url. [12]
    res = cloudinary.uploader.upload(
        io.BytesIO(content),
        resource_type="image",
        folder=public_id_prefix,
        overwrite=False,
        unique_filename=True,
    )
    return res["secure_url"]

def save_jsonl(panel: Panel):
    path = os.path.join(OUTPUT_DIR, "panels.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(panel), ensure_ascii=False) + "\n")

def assemble_story(panels: List[Panel]) -> StoryBundle:
    nodes, callbacks = {}, {}
    prev_id = None
    for i, p in enumerate(panels, 1):
        nid = f"node_{i:04d}"
        text = f"{p.speaker}\n\n{p.text}".strip() if p.speaker else p.text
        node = {
            "text": text,
            "type": "choice",
            "choices": [{"label": "Continue", "key": f"{STORY_ID}:goto:{i+1:04d}"}] if i < len(panels) else [],
        }
        if p.photo_url:
            node["photo"] = p.photo_url
        nodes[nid] = node
        if prev_id:
            callbacks[f"{STORY_ID}:goto:{i:04d}"] = nid
        prev_id = nid
    bundle = StoryBundle(
        meta={"title": STORY_TITLE, "intro": "node_0001" if nodes else ""},
        nodes=nodes,
        callbacks=callbacks,
    )
    # Write pretty story JSON for engine consumption
    with open(os.path.join(OUTPUT_DIR, f"{STORY_ID}.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": bundle.meta, "nodes": bundle.nodes, "callbacks": bundle.callbacks}, f, ensure_ascii=False, indent=2)
    return bundle

# ---------- Handlers ----------
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Bot alive. Send photos + text; I'll build a story JSON.")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m: Message = update.effective_message
    if not m or not m.photo:
        return
    chat_id = m.chat_id
    uid = m.from_user.id if m.from_user else 0
    # pick largest size
    file_id = m.photo[-1].file_id
    last_media_by_user[(chat_id, uid)] = {"ts": time.time(), "file_id": file_id}
    log.info(f"Photo captured for pairing chat={chat_id} uid={uid}")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m: Message = update.effective_message
    if not m or not (m.text or m.caption):
        return
    chat_id = m.chat_id
    uid = m.from_user.id if m.from_user else 0
    raw = m.text or m.caption or ""
    speaker, body = extract_speaker_and_body(raw)

    photo_url = None
    lm = last_media_by_user.get((chat_id, uid))
    if lm and time.time() - lm["ts"] <= PANEL_GAP_SEC:
        try:
            # Download from Telegram then upload to Cloudinary
            content = await download_photo_bytes(lm["file_id"], context)
            photo_url = cloudinary_upload_bytes(content, public_id_prefix=f"story/{STORY_ID}")
            log.info(f"Uploaded to Cloudinary: {photo_url}")
        except Exception as e:
            log.warning(f"Cloudinary upload failed: {e}")
        finally:
            last_media_by_user.pop((chat_id, uid), None)

    p = Panel(
        ts=time.time(),
        chat_id=chat_id,
        user_id=uid,
        speaker=speaker,
        text=body if speaker else raw,
        photo_url=photo_url,
    )
    panels.append(p)
    save_jsonl(p)
    bundle = assemble_story(panels)
    # Optional: echo a small confirmation
    await m.reply_text(f"Captured panel {len(panels)}. Nodes: {len(bundle.nodes)}")

async def periodic_dump(app: Application):
    while True:
        await asyncio.sleep(60)
        assemble_story(panels)

def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var.")
    # Validate Cloudinary config early
    if not (cloudinary.config().cloud_name and cloudinary.config().api_key and cloudinary.config().api_secret):
        log.warning("Cloudinary credentials missing; uploads will fail.")
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, on_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # Start periodic writer
    application.job_queue.run_repeating(lambda *_: assemble_story(panels), interval=60, first=60)
    log.info("Starting bot polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
