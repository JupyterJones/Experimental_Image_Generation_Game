import os
import sys
import time
import json
import random
import math
import requests
import traceback
import io
import datetime
import uuid
import websocket
import numpy as np
from threading import Thread, Lock
from flask import Flask, render_template_string, request, jsonify, send_file
from PIL import Image
from random import randint
from flask import (
    Flask,
    render_template_string,
    request,
    redirect,
    send_file,
    flash,
    url_for
)
import requests
from moviepy.editor import (
    ImageClip,
    concatenate_videoclips,
    AudioFileClip,
    VideoFileClip,
    vfx
)
from pydub import AudioSegment
from werkzeug.utils import secure_filename
from icecream import ic
# ============================================
# CONFIG & PATHS
# ============================================
COMFY_URL = "http://192.168.1.41:5001"
OLLAMA_URL = "http://localhost:11434"
CLIENT_ID = str(uuid.uuid4())

# Global progress state
comfy_progress = 0
comfy_max_steps = 0
DEFAULT_WIDTH = 336
DEFAULT_HEIGHT =512

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Ensure static/streamer exists
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "streamer")
STATE_FILE = os.path.join(OUTPUT_DIR, "streamer.json")
LOG_FILE_PATH = os.path.join(OUTPUT_DIR, "streamer.txt")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# GLOBAL STATE
# ============================================================
state_lock = Lock()
render_lock = Lock()

running = False
paused = False
current_frame = 0
frames_current = 500
current_seed = random.randint(111111, 999999)

model_name = "dreamshaper_8.safetensors"
vae_name = "vae-ft-mse-840000-ema-pruned.safetensors"
lora1_name = "more_details.safetensors"
lora2_name = "face_only_01.safetensors"
lora3_name = "None"

denoise_current = 0.35
teleport_image = None
active_caption = ""
caption_remaining = 0
active_caption_font = "Default"
active_caption_font_size = 20
logo_filename = "None"
logo_x = 0
logo_y = 0
logo_w = 100
logo_h = 100
logo_opacity = 1.0
feedback_color_boost = 1.03
feedback_contrast_boost = 1.01
feedback_sharpness_boost = 1.10
current_prompt = "Highly detailed Centered Science fiction image of a star-gate with semi transparent space creatures swimming in space similar to mythical sea monsters, surrounded with space, stars, planets, nebula, dust and space debris <lora:more_details:.8>"

def logit(*args):
    try:
        msg = " ".join(map(str, args))
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE_PATH, "a") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass
logit("landscape.py")    

def listen_to_comfy():
    """
    Listens to ComfyUI WebSocket for progress and completion updates.
    """
    global comfy_progress, comfy_max_steps, comfy_finished_prompts
    ws_url = COMFY_URL.replace("http://", "ws://") + f"/ws?clientId={CLIENT_ID}"
    while True:
        try:
            ws = websocket.create_connection(ws_url, timeout=10)
            # logit("Connected to ComfyUI WebSocket.")
            while True:
                result = ws.recv()
                if isinstance(result, str):
                    msg = json.loads(result)
                    m_type = msg.get('type')
                    m_data = msg.get('data', {})
                    
                    if m_type == 'progress':
                        with state_lock:
                            comfy_progress = m_data.get('value', 0)
                            comfy_max_steps = m_data.get('max', 0)
                    
                    elif m_type == 'executing':
                        # node is null means the prompt is finished
                        if m_data.get('node') is None:
                            p_id = m_data.get('prompt_id')
                            if p_id:
                                with state_lock:
                                    comfy_finished_prompts.add(p_id)
                                    # logit(f"WebSocket signal: Prompt {p_id} finished.")
                else:
                    continue 
        except Exception:
            time.sleep(5)

# Progress tracking set
comfy_finished_prompts = set()
Thread(target=listen_to_comfy, daemon=True).start()

tk = len(current_prompt.split(" "))
logit(f"Len Prompt: {tk}")
negative_prompt = "low quality, blurry, nudity, breasts, NSWF"

injection_lines = []
MAX_LINES = 5
keyframes = {}

# Motion Zoom Params
use_motion_zoom = True
use_metadata_caption = False
zoom_start = 1.0
zoom_end = 1.01
pan_start_x = 0.5
pan_end_x = 0.5
pan_start_y = 0.5
pan_end_y = 0.5

roll_mode = "none" # "none", "right", "left"

default_steps = 14
default_cfg = 4.0

# ============================================================
# MODELS LOADER
# ============================================================
try:
    MODELS = requests.get(f"{COMFY_URL}/models/checkpoints", timeout=30).json()
    LORAS = ["None"] + requests.get(f"{COMFY_URL}/models/loras", timeout=30).json()
except:
    MODELS, LORAS = [], ["None"]

# ============================================================
# NEW: SPACESHIP MOVEMENT FUNCTION
# ============================================================
def move_spaceship(img, frame_idx, w, h, spaceship_path="static/blank.png"):
    """
    Spaceship crosses right → left, loops, with subtle organic motion
    """
    if not os.path.exists(spaceship_path):
        try:
            os.makedirs(os.path.dirname(spaceship_path), exist_ok=True)
            ship_temp = Image.new("RGBA", (48, 24), (0, 0, 0, 0))
            from PIL import ImageDraw
            draw = ImageDraw.Draw(ship_temp)
            draw.polygon([(48, 12), (10, 0), (0, 12), (10, 24)], fill=(255, 60, 60, 255))
            draw.ellipse([8, 8, 20, 16], fill=(100, 200, 255, 255))
            ship_temp.save(spaceship_path)
            logit(f"Generated placeholder spaceship at {spaceship_path}")
        except Exception as e:
            logit(f"Failed to create spaceship placeholder: {e}")
            return img

    try:
        ship = Image.open(spaceship_path).convert("RGBA")
        ship_w, ship_h = ship.size

        # === horizontal movement (same as yours, but smoother float)
        speed = 2.0
        cycle_len = w + ship_w
        offset = (frame_idx * speed) % cycle_len
        ship_x = w - offset

        # === vertical drift (this is the magic)
        drift = int(20 * np.sin(frame_idx * 0.05))
        ship_y = (h // 2 - ship_h // 2) + drift

        # === subtle scale change (depth illusion)
        scale = 1.0 + 0.05 * np.sin(frame_idx * 0.03)
        new_w = int(ship_w * scale)
        new_h = int(ship_h * scale)
        ship_resized = ship.resize((new_w, new_h), Image.LANCZOS)

        # adjust position after scaling
        ship_x_adj = int(ship_x)
        ship_y_adj = int(ship_y)

        # === composite
        ship_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ship_layer.paste(ship_resized, (ship_x_adj, ship_y_adj), ship_resized)

        return Image.alpha_composite(img, ship_layer)

    except Exception as e:
        logit(f"Spaceship overlay error: {e}")
        return img

# ============================================================
# NEW: MOVIE CREATION FUNCTION
# ============================================================
def create_movie_from_frames(output_filename="production_june8.mp4"):
    """
    Joins the generated images into a movie file using ffmpeg.
    """
    logit("Joining images to create movie...")
    try:
        import subprocess
        # Search for frames and compile
        cmd = [
            "ffmpeg", "-y", "-framerate", "5", 
            "-i", os.path.join(OUTPUT_DIR, "frame_%03d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", 
            os.path.join(OUTPUT_DIR, output_filename)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            logit(f"Movie successfully created: {output_filename}")
            return True
        else:
            logit(f"FFmpeg error: {result.stderr}")
            return False
    except Exception as e:
        logit(f"Movie creation failed: {e}")
        return False

# ============================================================
# ZOOM & FINAL RENDER FUNCTION (UPDATED)
# ============================================================
def apply_pil_zoom(img, frame_idx, total_frames, overlay_png_path=None, overlay_opacity=0.10, spaceship_path="static/spaceship.png"):
    w, h = img.size
    img = img.convert("RGBA")

    # 1. Apply custom logo if configured, otherwise fallback to fullscreen overlay if requested
    if logo_filename and logo_filename != "None":
        logo_path = os.path.join(BASE_DIR, "static", "overlays", logo_filename)
        if os.path.exists(logo_path):
            try:
                logo_img = Image.open(logo_path).convert("RGBA")
                lw, lh = logo_w, logo_h
                if lw > 0 and lh > 0:
                    logo_img = logo_img.resize((lw, lh), Image.LANCZOS)
                
                if logo_opacity < 1.0:
                    alpha = logo_img.getchannel("A")
                    alpha = alpha.point(lambda p: int(p * logo_opacity))
                    logo_img.putalpha(alpha)
                
                logo_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                logo_layer.paste(logo_img, (logo_x, logo_y), logo_img)
                img = Image.alpha_composite(img, logo_layer)
            except Exception as le:
                logit(f"Custom logo composition error: {le}")
    elif overlay_png_path and os.path.exists(overlay_png_path):
        overlay = Image.open(overlay_png_path).convert("RGBA")
        if overlay.size != img.size:
            overlay = overlay.resize((w, h), Image.LANCZOS)
        alpha = overlay.getchannel("A")
        alpha = alpha.point(lambda p: int(p * overlay_opacity))
        overlay.putalpha(alpha)
        img = Image.alpha_composite(img, overlay)

    # 2. CALL THE NEW SPACESHIP FUNCTION
    img = move_spaceship(img, frame_idx, w, h, spaceship_path=spaceship_path)

    # 3. Motion Zoom Logic
    progress = frame_idx / max(total_frames - 1, 1)
    curr_zoom = zoom_start + (zoom_end - zoom_start) * progress
    curr_pan_x = pan_start_x + (pan_end_x - pan_start_x) * progress
    curr_pan_y = pan_start_y + (pan_end_y - pan_start_y) * progress

    crop_w = w / curr_zoom
    crop_h = h / curr_zoom

    left = (w * curr_pan_x) - (crop_w / 2)
    top = (h * curr_pan_y) - (crop_h / 2)

    left = max(0, min(w - crop_w, left))
    top = max(0, min(h - crop_h, top))

    right = left + crop_w
    bottom = top + crop_h

    img = img.crop((left, top, right, bottom)).resize((w, h), Image.LANCZOS)
    
    # 4. Apply Roll (Rotation)
    if roll_mode != "none":
        # Tiny increment per frame: 0.005 degrees (Slowed for cinematic weight)
        # Right roll = clockwise (negative), Left roll = counter-clockwise (positive)
        direction = -1 if roll_mode == "right" else 1
        angle = frame_idx * 0.005 * direction
        # resample=Image.BICUBIC for high quality, expand=False to keep same size
        img = img.rotate(angle, resample=Image.BICUBIC, expand=False)

    return img.convert("RGB"), (curr_zoom, curr_pan_x, curr_pan_y)

def draw_metadata_caption(img, frame_idx, total_frames, metadata, curr_zoom, curr_pan_x, curr_pan_y):
    try:
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)
        lines = [
            f"   Frame: {frame_idx} of {total_frames} - Seed: {metadata.get('seed')} - Step: {metadata.get('steps')} - CFG: {metadata.get('cfg')}",
            f"   Denoise: {metadata.get('denoise'):.2f} - Zoom: {curr_zoom:.3f} - Yaw: {curr_pan_x:.2f} - Pitch: {curr_pan_y:.2f}"
        ]
        text = "\n".join(lines)
        margin = 10
        line_height = 14
        box_h = len(lines) * line_height + 12
        box_w = 320

        draw.rectangle(
            [margin, margin, margin + box_w, margin + box_h],
            fill=(61, 81, 92, 100)
        )

        draw.text(
            (margin + 10, margin + 6),
            text,
            fill=(255, 255, 255, 255)
        )

        return img

    except Exception as e:
        logit(f"Caption error: {e}")
        return img

'''
def draw_metadata_caption(img, frame_idx, total_frames, metadata, curr_zoom, curr_pan_x, curr_pan_y):
    try:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        h = img.height
        
        # Prepare text lines
        lines = [
            f"F: {frame_idx} of {total_frames}",
            f"Seed: {metadata.get('seed')}  Step: {metadata.get('steps')}   CFG: {metadata.get('cfg')}",
            f"Den: {metadata.get('denoise'):.2f} |  Zoom: {curr_zoom:.3f}",
            f"PanX: {curr_pan_x:.2f} PanY: {curr_pan_y:.2f}"
        ]
        text = "\n".join(lines)
        
        # Optimized for 340 width: 10px margin left + 320px box + 10px margin right
        margin = 10
        line_height = 14
        box_h = len(lines) * line_height + 12
        box_w = 320 
        
        draw.rectangle([margin, h - box_h - margin, margin + box_w, h - margin], fill=(0, 0, 0, 180))
        draw.text((margin + 10, h - box_h - margin + 6), text, fill=(255, 255, 255, 255))
        return img
    except Exception as e:
        logit(f"Caption error: {e}")
        return img
'''
def apply_border(img, border_path="static/border.png"):
    """
    Overlays a frame/border on the image. This is only for local storage.
    """
    if not os.path.exists(border_path):
        return img
    try:
        border = Image.open(border_path).convert("RGBA")
        # Resize border to match image if necessary
        if border.size != img.size:
            border = border.resize(img.size, Image.LANCZOS)
        
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, border)
        return img.convert("RGB")
    except Exception as e:
        logit(f"Border overlay error: {e}")
        return img

def get_available_fonts():
    """
    Scans the fonts subdirectory recursively for TTF/OTF fonts.
    Returns a list of relative paths from the fonts directory.
    """
    fonts_dir = os.path.join(BASE_DIR, "fonts")
    font_files = []
    if os.path.exists(fonts_dir):
        for root, dirs, files in os.walk(fonts_dir):
            for file in files:
                if file.lower().endswith((".ttf", ".otf")):
                    rel_path = os.path.relpath(os.path.join(root, file), fonts_dir)
                    font_files.append(rel_path)
    font_files.sort()
    return font_files

def draw_top_caption(img, text, font_name="Default", font_size=20):
    """
    Draws a single line of text at the top on a 50% transparent black background.
    """
    if not text:
        return img
    try:
        from PIL import ImageDraw, ImageFont
        # Create an RGBA version of the image to support transparency in drawing
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        w, h = img.size
        
        # Load font if not Default
        font = None
        if font_name and font_name != "Default":
            font_path = os.path.join(BASE_DIR, "fonts", font_name)
            if os.path.exists(font_path):
                try:
                    font = ImageFont.truetype(font_path, font_size)
                except Exception as fe:
                    logit(f"Failed to load font {font_path}: {fe}")
        
        if font is None:
            font = ImageFont.load_default()
        
        # Measure text size to size the background box
        # We use draw.textbbox if available (Pillow >= 8.0.0), otherwise fallback
        text_w = w
        text_h = font_size
        box_h = font_size + 16 # default fallback height
        
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            box_h = text_h + 20
        except AttributeError:
            try:
                text_w, text_h = draw.textsize(text, font=font)
                box_h = text_h + 20
            except:
                pass
        
        # Draw 50% transparent black box (0, 0, 0, 128)
        draw.rectangle([0, 0, w, box_h], fill=(0, 0, 0, 128))
        
        # Draw white text. Center it vertically in the box, and indent slightly
        text_y = (box_h - text_h) // 2
        text_y = max(0, text_y)
        
        draw.text((20, text_y), text, font=font, fill=(255, 255, 255, 255))
        
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay)
        return img.convert("RGB")
    except Exception as e:
        logit(f"Top caption error: {e}")
        return img

# ============================================================
# STATE
# ============================================================
def save_state():
    with state_lock:
        state = {
            "current_frame": current_frame,
            "frames_total": frames_current,
            "seed": current_seed,
            "prompt": current_prompt,
            "negative_prompt": negative_prompt,
            "model": model_name,
            "lora1": lora1_name,
            "lora2": lora2_name,
            "lora3": lora3_name,
            "denoise": denoise_current,
            "keyframes": keyframes,
            "injection_lines": injection_lines,
            "use_motion_zoom": use_motion_zoom,
            "use_metadata_caption": use_metadata_caption,
            "z_s": zoom_start,
            "z_e": zoom_end,
            "px_s": pan_start_x,
            "px_e": pan_end_x,
            "py_s": pan_start_y,
            "py_e": pan_end_y,
            "roll_mode": roll_mode,
            "steps": default_steps,
            "cfg": default_cfg,
            "logo_filename": logo_filename,
            "logo_x": logo_x,
            "logo_y": logo_y,
            "logo_w": logo_w,
            "logo_h": logo_h,
            "logo_opacity": logo_opacity,
            "feedback_color_boost": feedback_color_boost,
            "feedback_contrast_boost": feedback_contrast_boost,
            "feedback_sharpness_boost": feedback_sharpness_boost
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except:
            pass

def load_state():
    global current_frame, current_seed, current_prompt, negative_prompt, model_name, denoise_current
    global keyframes, injection_lines, frames_current, lora1_name, lora2_name, lora3_name
    global use_motion_zoom, zoom_start, zoom_end, pan_start_x, pan_end_x, pan_start_y, pan_end_y
    global default_steps, default_cfg, use_metadata_caption
    global logo_filename, logo_x, logo_y, logo_w, logo_h, logo_opacity

    if not os.path.exists(STATE_FILE):
        return False

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        with state_lock:
            current_frame = state.get("current_frame", 0)
            frames_current = state.get("frames_total", 500)
            current_seed = state.get("seed", 999)
            current_prompt = state.get("prompt", "")
            negative_prompt = state.get("negative_prompt", "")
            model_name = state.get("model", "")
            lora1_name = state.get("lora1", "None")
            lora2_name = state.get("lora2", "None")
            lora3_name = state.get("lora3", "None")
            denoise_current = state.get("denoise", 0.35)

            keyframes = state.get("keyframes", {})
            injection_lines = state.get("injection_lines", [])

            use_motion_zoom = state.get("use_motion_zoom", True)
            use_metadata_caption = state.get("use_metadata_caption", False)
            zoom_start = state.get("z_s", 1.0)
            logo_filename = state.get("logo_filename", "None")
            logo_x = int(state.get("logo_x", 0))
            logo_y = int(state.get("logo_y", 0))
            logo_w = int(state.get("logo_w", 100))
            logo_h = int(state.get("logo_h", 100))
            logo_opacity = float(state.get("logo_opacity", 1.0))
            feedback_color_boost = float(state.get("feedback_color_boost", 1.03))
            feedback_contrast_boost = float(state.get("feedback_contrast_boost", 1.01))
            feedback_sharpness_boost = float(state.get("feedback_sharpness_boost", 1.10))
            zoom_end = state.get("z_e", 1.1)
            pan_start_x = state.get("px_s", 0.5)
            pan_end_x = state.get("px_e", 0.5)
            pan_start_y = state.get("py_s", 0.5)
            pan_end_y = state.get("py_e", 0.5)
            roll_mode = state.get("roll_mode", "none")

        # CRITICAL: Detect actual last frame on disk
        files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")])
        if files:
            last_file = files[-1]
            try:
                # Extract number from frame_XXX.png
                last_disk_frame = int(last_file.split("_")[1].split(".")[0])
                current_frame = last_disk_frame + 1
                logit(f"Resume: Detected frame {last_disk_frame} on disk. Starting at {current_frame}")
            except:
                pass

        if current_frame >= frames_current:
            logit(f"Session reached limit ({current_frame}/{frames_current}). Increase 'Frames' to continue.")

        return True

    except Exception as e:
        logit("Load state error:", e)
        return False

# ============================================================
# RENDER LOOP
# ============================================================
def get_workflow(active_s, active_p, neg_text, server_filename=None, frame_idx=0, active_d=0.35):
    wf = {
        "10": {"inputs": {"ckpt_name": model_name}, "class_type": "CheckpointLoaderSimple"},
        "20": {"inputs": {"vae_name": vae_name}, "class_type": "VAELoader"},
        "6": {"inputs": {"text": active_p, "clip": ["10", 1]}, "class_type": "CLIPTextEncode"},
        "7": {"inputs": {"text": neg_text, "clip": ["10", 1]}, "class_type": "CLIPTextEncode"}
    }
    lm, lc = ["10", 0], ["10", 1]
    for i, ln in enumerate([lora1_name, lora2_name, lora3_name]):
        if ln and ln != "None":
            nid = f"lora_{i}"
            wf[nid] = {"inputs": {"lora_name": ln, "strength_model": 0.8, "strength_clip": 0.8, "model": lm, "clip": lc}, "class_type": "LoraLoader"}
            lm, lc = [nid, 0], [nid, 1]
    
    wf["6"]["inputs"]["clip"] = lc; wf["7"]["inputs"]["clip"] = lc

    if server_filename:
        wf["11"] = {"inputs": {"image": server_filename}, "class_type": "LoadImage"}
        wf["12"] = {"inputs": {"pixels": ["11", 0], "vae": ["20", 0]}, "class_type": "VAEEncode"}
        lat = ["12", 0]
    else:
        wf["5"] = {"inputs": {"width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT, "batch_size": 1}, "class_type": "EmptyLatentImage"}
        lat = ["5", 0]

    wf["3"] = {"inputs": {"seed": active_s, "steps": default_steps, "cfg": default_cfg, "sampler_name": "euler", "scheduler": "normal", "denoise": active_d if server_filename else 1.0, "model": lm, "positive": ["6", 0], "negative": ["7", 0], "latent_image": lat}, "class_type": "KSampler"}
    wf["8"] = {"inputs": {"samples": ["3", 0], "vae": ["20", 0]}, "class_type": "VAEDecode"}
    wf["9"] = {"inputs": {"filename_prefix": "epoch_", "images": ["8", 0]}, "class_type": "SaveImage"}
    return wf

def render_video(resume=False):
    global running, current_frame, paused, current_seed, teleport_image
    global caption_remaining, active_caption, roll_mode
    if running: return
    logit("ENGINE STARTED: Entering render loop.")
    
    if resume:
        if not load_state():
            logit("Failed to load state, starting fresh.")
            current_frame = 0
            injection_lines.clear()
    else:
        current_frame = 0
        injection_lines.clear()
        # Cleanup existing frames from previous runs to prevent frame bleeding
        for f in os.listdir(OUTPUT_DIR):
            if (f.startswith("frame_") and f.endswith(".png")) or (f.startswith("temp_clean_") and f.endswith(".png")) or (f.startswith("clean_") and f.endswith(".png")):
                try:
                    os.remove(os.path.join(OUTPUT_DIR, f))
                except Exception as e:
                    logit(f"Failed to remove old frame {f}: {e}")
    
    running = True
    last_server_filename = None

    if current_frame > 0:
        prev_clean = os.path.join(OUTPUT_DIR, f"clean_{current_frame-1:03d}.png")
        prev_framed = os.path.join(OUTPUT_DIR, f"frame_{current_frame-1:03d}.png")
        prev = prev_clean if os.path.exists(prev_clean) else prev_framed
        if os.path.exists(prev):
            try:
                with open(prev, "rb") as f:
                    up = requests.post(f"{COMFY_URL}/upload/image", files={"image": ("init.png", f)}, timeout=2000).json()
                    last_server_filename = up.get("name")
            except Exception as e:
                logit(f"Error uploading init image: {e}")

    try:
        while current_frame < frames_current:
            if not running: break
            if paused:
                time.sleep(1)
                continue
            
            with state_lock:
                if teleport_image:
                    last_server_filename = teleport_image
                    teleport_image = None
                    logit(f"Teleporting! Using external image for frame {current_frame}")

            seed = current_seed + current_frame
            prompt = current_prompt + (", " + ", ".join(injection_lines[-MAX_LINES:]) if injection_lines else "")
            
            # Calculate active params (for keyframe support)
            active_p, active_d, active_s = prompt, denoise_current, seed
            kf = keyframes.get(str(current_frame))
            if kf:
                active_p = kf.get("prompt", active_p)
                active_d = float(kf.get("denoise", active_d))
                active_s = seed + int(kf.get("seed_offset", 0))

            wf = get_workflow(active_s, active_p, negative_prompt, last_server_filename, current_frame, active_d)
            
            try:
                resp = requests.post(f"{COMFY_URL}/prompt", json={"prompt": wf, "client_id": CLIENT_ID}, timeout=2000).json()
                pid = resp["prompt_id"]
                logit(f"Prompt sent (Frame {current_frame}). PID: {pid}")
            except Exception as e:
                logit(f"Error sending prompt: {e}")
                time.sleep(5)
                continue
            
            image_info = None
            # Wait for either WebSocket signal or poll fallback
            for i in range(3600): 
                if not running: break
                time.sleep(1)
                
                # Check WebSocket signal first (faster)
                is_finished = False
                with state_lock:
                    if pid in comfy_finished_prompts:
                        is_finished = True
                        comfy_finished_prompts.remove(pid) # Clean up
                
                if is_finished or i % 10 == 0: # Check history every 10s as fallback
                    try:
                        hist_resp = requests.get(f"{COMFY_URL}/history/{pid}", timeout=2000).json()
                        if pid in hist_resp:
                            image_info = hist_resp[pid]["outputs"]["9"]["images"][0]
                            logit(f"Frame {current_frame} finished.")
                            break
                    except:
                        continue

            if not image_info:
                logit(f"Timeout/No image for frame {current_frame}")
                break
            
            try:
                raw = requests.get(f"{COMFY_URL}/view", params=image_info, timeout=2000).content
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                
                # Metadata for caption
                meta = {
                    "seed": active_s,
                    "steps": default_steps,
                    "cfg": default_cfg,
                    "denoise": active_d if last_server_filename else 1.0
                }

                # 1. Apply zoom AND spaceship movement (This is the BASE for both server and disk)
                img, zoom_data = apply_pil_zoom(
                    img, 
                    current_frame, 
                    frames_current,
                    overlay_png_path="static/logo.png",
                    overlay_opacity=0.8,
                    spaceship_path="static/spaceship.png"
                )
                
                # 2. UPLOAD CLEAN IMAGE TO COMFYUI
                # We save a copy of the clean image for the feedback loop
                # Apply feedback loop stabilization (color, contrast, sharpness) to counteract VAE degradation
                feedback_img = img.copy()
                if feedback_color_boost != 1.0 or feedback_contrast_boost != 1.0 or feedback_sharpness_boost != 1.0:
                    from PIL import ImageEnhance
                    try:
                        if feedback_color_boost != 1.0:
                            feedback_img = ImageEnhance.Color(feedback_img).enhance(feedback_color_boost)
                        if feedback_contrast_boost != 1.0:
                            feedback_img = ImageEnhance.Contrast(feedback_img).enhance(feedback_contrast_boost)
                        if feedback_sharpness_boost != 1.0:
                            feedback_img = ImageEnhance.Sharpness(feedback_img).enhance(feedback_sharpness_boost)
                    except Exception as ee:
                        logit(f"Feedback loop stabilization error: {ee}")
                
                clean_path = os.path.join(OUTPUT_DIR, f"clean_{current_frame:03d}.png")
                feedback_img.save(clean_path)
                with open(clean_path, "rb") as f:
                    up = requests.post(f"{COMFY_URL}/upload/image", files={"image": (f"f_{current_frame}.png", f)}, timeout=2000).json()
                    last_server_filename = up.get("name")
                
                # 3. CREATE LOCAL ARCHIVE IMAGE (With Overlays)
                # We create a COPY of the image so we don't accidentally leak overlays
                local_img = img.copy()

                if use_metadata_caption:
                    local_img = draw_metadata_caption(local_img, current_frame, frames_current, meta, *zoom_data)
                
                # Apply Border
                local_img = apply_border(local_img)
                
                # Apply Temporary Top Caption
                with state_lock:
                    if caption_remaining > 0:
                        local_img = draw_top_caption(local_img, active_caption, active_caption_font, active_caption_font_size)
                        caption_remaining -= 1
                        if caption_remaining == 0:
                            logit("Temporary caption finished.")

                local_path = os.path.join(OUTPUT_DIR, f"frame_{current_frame:03d}.png")
                local_img.save(local_path)
                
                # Cleanup
                del local_img # Free memory

                current_frame += 1
                save_state()
            except Exception as e:
                logit(f"Error processing frame {current_frame}: {e}")
                break

    except Exception as e:
        logit(f"Render loop error: {e}")
    finally:
        running = False

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)
app.config["OVERLAYS_FOLDER"] = os.path.join(BASE_DIR, "static", "overlays")
os.makedirs(app.config["OVERLAYS_FOLDER"], exist_ok=True)

def get_available_logos():
    logos_dir = os.path.join(BASE_DIR, "static", "overlays")
    if not os.path.exists(logos_dir):
        os.makedirs(logos_dir, exist_ok=True)
    files = sorted([f for f in os.listdir(logos_dir) if f.lower().endswith(".png")])
    return files

@app.route("/")
def index():
    fonts = get_available_fonts()
    logos = get_available_logos()
    return render_template_string(
        HTML_UI, 
        MODELS=MODELS, 
        LORAS=LORAS, 
        FONTS=fonts, 
        LOGOS=logos, 
        CURRENT_LOGO=logo_filename,
        feedback_color_boost=feedback_color_boost,
        feedback_contrast_boost=feedback_contrast_boost,
        feedback_sharpness_boost=feedback_sharpness_boost
    )

@app.route("/upload_logo", methods=["POST"])
def upload_logo():
    if "logo" not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400
    file = request.files["logo"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "No selected file"}), 400
    if file and file.filename.lower().endswith(".png"):
        filename = secure_filename(file.filename)
        path = os.path.join(app.config["OVERLAYS_FOLDER"], filename)
        file.save(path)
        logit(f"Uploaded logo: {filename}")
        return jsonify({"status": "ok", "filename": filename})
    return jsonify({"status": "error", "message": "Only transparent PNGs allowed"}), 400

@app.route("/save_logo_position", methods=["POST"])
def save_logo_position():
    global logo_filename, logo_x, logo_y, logo_w, logo_h, logo_opacity
    d = request.json
    filename = d.get("logo_filename", "None")
    with state_lock:
        if filename == "None":
            logo_filename = "None"
        else:
            logo_filename = filename
            logo_x = int(d.get("x", 0))
            logo_y = int(d.get("y", 0))
            logo_w = int(d.get("w", 100))
            logo_h = int(d.get("h", 100))
            logo_opacity = float(d.get("opacity", 1.0))
    save_state()
    logit(f"Logo position saved: {logo_filename} at ({logo_x}, {logo_y}) {logo_w}x{logo_h} (Opacity: {logo_opacity})")
    return jsonify({"status": "ok"})

def get_ollama_models():
    """
    Queries local Ollama instance for installed models.
    """
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            return models
    except:
        pass
    return []

@app.route("/get_ollama_models")
def get_ollama_route():
    models = get_ollama_models()
    return jsonify({"models": models})

@app.route("/enhance_prompt", methods=["POST"])
def enhance_prompt():
    d = request.json
    user_prompt = d.get("prompt", "").strip()
    model = d.get("model", "")
    if not user_prompt:
        return jsonify({"error": "Prompt is empty"}), 400
    
    system_prompt = (
        "Act as an expert Stable Diffusion prompt generator. "
        "Expand the following concept into a highly descriptive visual prompt. "
        "Focus on atmospheric lighting, artistic style, camera lens details, and vivid textures. "
        "Keep the output under 60 words and return ONLY the final prompt, with no intro, outro, or conversational filler."
    )
    
    payload = {
        "model": model,
        "prompt": f"{system_prompt}\n\nConcept: {user_prompt}\nExpanded Prompt:",
        "stream": False,
        "options": {
            "temperature": 0.7,
            "max_tokens": 150
        }
    }
    
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=25)
        if r.status_code == 200:
            enhanced = r.json().get("response", "").strip()
            if enhanced.startswith('"') and enhanced.endswith('"'):
                enhanced = enhanced[1:-1]
            logit(f"Prompt enhanced by {model}: '{user_prompt}' -> '{enhanced}'")
            return jsonify({"status": "ok", "enhanced_prompt": enhanced})
        else:
            return jsonify({"error": f"Ollama error: status code {r.status_code}"}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to reach Ollama: {str(e)}"}), 500

@app.route("/compile_movie", methods=["POST"])
def compile_movie():
    success = create_movie_from_frames()
    return jsonify({"status": "success" if success else "failed"})

@app.route("/control", methods=["POST"])
def control():
    global current_prompt, paused; d = request.json; act = d.get("action")
    if act == "start":
        current_prompt = d.get("prompt", current_prompt)
        Thread(target=lambda: render_video(False), daemon=True).start()
    elif act == "resume":
        Thread(target=lambda: render_video(True), daemon=True).start()
    elif act == "pause": paused = not paused
    return jsonify({"status": "ok"})

@app.route("/update_params", methods=["POST"])
def update_params():
    global model_name, negative_prompt, lora1_name, lora2_name, current_seed, denoise_current, frames_current
    global zoom_start, zoom_end, pan_start_x, pan_end_x, pan_start_y, pan_end_y, default_steps, default_cfg, use_motion_zoom, use_metadata_caption
    global roll_mode
    global feedback_color_boost, feedback_contrast_boost, feedback_sharpness_boost
    d = request.json
    model_name = d.get("model"); negative_prompt = d.get("negative_prompt", negative_prompt)
    lora1_name = d.get("lora1"); lora2_name = d.get("lora2")
    current_seed = int(d.get("seed", current_seed)); denoise_current = float(d.get("denoise", 0.35))
    frames_current = int(d.get("frames", 120)); use_motion_zoom = bool(d.get("use_motion_zoom"))
    use_metadata_caption = bool(d.get("use_metadata_caption"))
    zoom_start = float(d.get("zoom_start", 1.0)); zoom_end = float(d.get("zoom_end", 1.1))
    pan_start_x = float(d.get("pan_start_x", 0.5)); pan_end_x = float(d.get("pan_end_x", 0.5))
    pan_start_y = float(d.get("pan_start_y", 0.5)); pan_end_y = float(d.get("pan_end_y", 0.5))
    roll_mode = d.get("roll_mode", "none")
    default_steps = int(d.get("steps", 15)); default_cfg = float(d.get("cfg", 5.4))
    feedback_color_boost = float(d.get("feedback_color", 1.03))
    feedback_contrast_boost = float(d.get("feedback_contrast", 1.01))
    feedback_sharpness_boost = float(d.get("feedback_sharpness", 1.10))
    save_state(); return jsonify({"status": "ok"})

@app.route("/status")
def status_route():
    files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")])
    history = files[-5:] if len(files) > 0 else []
    history.reverse()
    return jsonify({
        "running": running, "paused": paused, "frame": current_frame, "total": frames_current,
        "history": history, "keyframes": keyframes, "injections": injection_lines, "zoom": use_motion_zoom,
        "metadata_caption": use_metadata_caption,
        "progress": comfy_progress,
        "max_steps": comfy_max_steps,
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "logo_filename": logo_filename,
        "logo_x": logo_x,
        "logo_y": logo_y,
        "logo_w": logo_w,
        "logo_h": logo_h,
        "logo_opacity": logo_opacity
    })

@app.route("/add_keyframe", methods=["POST"])
def add_keyframe():
    d = request.json; f_idx = str(d.get("frame", 0))
    keyframes[f_idx] = {"prompt": d.get("prompt", "") or current_prompt, "denoise": float(d.get("denoise", 0.5)), "seed_offset": int(d.get("seed_offset", 0))}
    save_state(); return jsonify({"status": "ok", "keyframes": keyframes})

@app.route("/latest_frame")
def latest():
    files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")])
    if not files: return "none", 404
    return send_file(os.path.join(OUTPUT_DIR, files[-1]), max_age=0)

@app.route("/inject", methods=["POST"])
def inject():
    t = request.json.get("text", "").strip(); 
    if t: injection_lines.append(t); save_state()
    return jsonify({"status": "ok"})

@app.route("/set_caption", methods=["POST"])
def set_caption():
    global active_caption, caption_remaining, active_caption_font, active_caption_font_size
    d = request.json
    t = d.get("text", "").strip()
    font = d.get("font", "Default")
    font_size = int(d.get("font_size", 20))
    if t:
        with state_lock:
            active_caption = t
            caption_remaining = 5
            active_caption_font = font
            active_caption_font_size = font_size
        logit(f"Caption set: {active_caption} (Font: {font}, Size: {font_size}, Remaining: 5)")
    return jsonify({"status": "ok"})

@app.route("/teleport", methods=["POST"])
def teleport():
    global teleport_image
    if "image" not in request.files:
        return jsonify({"status": "error", "message": "No image part"}), 400
    file = request.files["image"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "No selected file"}), 400
    
    try:
        # Resize image to match project dimensions (DEFAULT_WIDTH x DEFAULT_HEIGHT)
        img = Image.open(file).convert("RGB")
        if img.size != (DEFAULT_WIDTH, DEFAULT_HEIGHT):
            logit(f"Resizing teleport image from {img.size} to {(DEFAULT_WIDTH, DEFAULT_HEIGHT)}")
            img = img.resize((DEFAULT_WIDTH, DEFAULT_HEIGHT), Image.LANCZOS)
        
        # Save to buffer
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        # Upload to ComfyUI
        files = {"image": ("teleport.png", buf)}
        resp = requests.post(f"{COMFY_URL}/upload/image", files=files, timeout=30).json()
        
        with state_lock:
            teleport_image = resp.get("name")
        
        logit(f"Teleport image set: {teleport_image}")
        return jsonify({"status": "ok", "filename": teleport_image})
    except Exception as e:
        logit(f"Teleport upload error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ============================================================
# UI TEMPLATE
# ============================================================
HTML_UI = """
<!DOCTYPE html><html><head><title>PIL AI Director</title>
<style>
    body { margin: 0; background: #0c0c0e; color: #d1d1d1; font-family: sans-serif; display: flex; height: 100vh; overflow: hidden; }
    .column { padding: 15px; box-sizing: border-box; overflow-y: auto; border-right: 1px solid #333; }
    .left { padding: 8px; width: 30%; background: #141417; }
    .center { width: 40%; background: #080808; text-align: center; }
    .right { padding: 8px; width: 30%; background: #141417; }
    label { font-size: 11px; color: #888; font-weight: bold; display: block; margin-top: 8px; }
    input, select, textarea { width: 96%; background: #1c1c21; color: #fff; border: 1px solid #333; padding: 6px; border-radius: 4px; font-size: 12px; margin-top: 4px; margin-right: 10px; }
    button { width: 98%; padding: 8px; margin-top: 8px; cursor: pointer; border: none; border-radius: 4px; font-weight: bold; transition: all 0.2s; }
    .btn-green { background: #10b981; color: white; }
    .btn-blue { background: #3b82f6; color: white; }
    .btn-orange { background: #f59e0b; color: white; }
    #preview { max-width: 98%; max-height: 55vh; border: 1px solid #444; margin-top: 10px; }
    .thumb-strip { display: flex; justify-content: center; gap: 10px; margin-top: 10px; min-height: 100px;}
    .thumb-strip img { width: 18%; height: auto; border: 2px solid #333; border-radius: 4px; opacity: 0.6; }
    .tag { background: blue; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin: 2px; display: inline-block; }
    .kf-item { background: #1c1c21; padding: 5px; margin-top: 5px; border-radius: 4px; border-left: 3px solid #8b5cf6; font-size: 10px; text-align: left;}
</style></head>
<body>
    <div class="column left">
        <h3>FlaskArchitect's EpochStreamer Engine Config</h3>
        <label>Base Prompt</label><textarea id="prompt" rows="3">Highly detailed Centered Science fiction image of a star-gate with semi transparent space creatures swimming in space similar to mythical sea monsters, surrounded with space, stars, planets, nebula, dust and space debris <lora:more_details:.8></textarea>
        
        <!-- Ollama Prompt Enhancer integration -->
        <label>AI Prompt Enhancer (Ollama)</label>
        <div style="display:flex; gap:5px; margin-top:4px; margin-bottom:8px;">
            <select id="ollama_model" style="flex:1; margin-top:0;">
                <option value="None">Loading Ollama models...</option>
            </select>
            <button class="btn-blue" onclick="enhancePrompt()" style="width:auto; margin-top:0; padding: 4px 8px; font-size:11px;">Enhance</button>
        </div>

        <label>Negative Prompt</label><textarea id="neg_prompt" rows="2">low quality, blurry, bare breasts, nipples, large breasts, NSFW, deformed fingers</textarea>
        <label>Model</label><select id="model">{% for m in MODELS %}<option>{{m}}</option>{% endfor %}</select>
        <label>LoRA 1</label><select id="lora1">{% for l in LORAS %}<option>{{l}}</option>{% endfor %}</select>
        
        <!-- Row 1 -->
        <div style="display:flex; gap:5px;">
            <div style="flex:1;">
                <label>Seed</label>
                <input type="number" id="seed" value="123456">
            </div>

            <div style="flex:1;">
                <label>Steps</label>
                <input type="number" id="steps" value="15">
            </div>

            <div style="flex:1;">
                <label>CFG</label>
                <input type="number" id="cfg" value="5.5" step="0.5">
            </div>
        </div>

        <!-- Row 2 -->
        <div style="display:flex; gap:5px; margin-top:5px;">
            <div style="flex:1;">
                <label>Denoise</label>
                <input type="number" id="denoise" step="0.05" value="0.35">
            </div>

            <div style="flex:1;">
                <label>Frames</label>
                <input type="number" id="frames" value="500">
            </div>
        </div>
<button class="btn-green" onclick="window.open('/media', '_blank')">
    MAKE VIDEOS
</button>
        <hr style="border:0; border-top:1px solid #333; margin:20px 0;">
        <h3>Motion Zoom</h3>
        <label><input type="checkbox" id="use_zoom" checked style="width:auto;"> ENABLE ZOOM</label>
        <label><input type="checkbox" id="use_caption" style="width:auto;"> SHOW METADATA CAPTION</label>
        <label>Zoom Start/End</label><div style="display:flex; gap:5px;"><input type="number" id="zs" value="1.0" step="0.01"><input type="number" id="ze" value="1.05" step="0.01"></div>
        <label>Yaw S/E</label><div style="display:flex; gap:5px;"><input type="number" id="pxs" value="0.5" step="0.01"><input type="number" id="pxe" value="0.5" step="0.01"></div>
        <label>Pitch S/E</label><div style="display:flex; gap:5px;"><input type="number" id="pys" value="0.5" step="0.01"><input type="number" id="pye" value="0.5" step="0.01"></div>
        <label>Ship Roll</label>
        <select id="roll_mode">
            <option value="none">Level (Stop)</option>
            <option value="right">Roll Right</option>
            <option value="left">Roll Left</option>
        </select>
        
        <hr style="border:0; border-top:1px solid #333; margin:10px 0;">
        <h3 style="margin-top:10px; margin-bottom:5px;">Feedback Stabilizer</h3>
        <div style="display:flex; gap:5px;">
            <div style="flex:1;">
                <label>Color</label>
                <input type="number" id="fb_color" step="0.01" value="{{ feedback_color_boost }}">
            </div>
            <div style="flex:1;">
                <label>Contrast</label>
                <input type="number" id="fb_contrast" step="0.01" value="{{ feedback_contrast_boost }}">
            </div>
            <div style="flex:1;">
                <label>Sharpness</label>
                <input type="number" id="fb_sharpness" step="0.05" value="{{ feedback_sharpness_boost }}">
            </div>
        </div>
        
        <div style="margin: 10px 0; display: flex; align-items: center; gap: 8px;">
            <input type="checkbox" id="pause_ui_sync" style="width: auto; margin: 0; cursor: pointer;">
            <label for="pause_ui_sync" style="margin: 0; cursor: pointer; font-weight: bold; color: #f59e0b; font-size: 11px;">Pause Parameter Syncing (Manual edit mode)</label>
        </div>
        <button class="btn-blue" onclick="update(this)">UPDATE ENGINE</button>
    </div>

    <div class="column center">
        <h2 id="status_text">IDLE</h2>
        <div id="injections"></div>
        <div id="preview_container" style="position: relative; display: inline-block; max-width: 98%; max-height: 55vh; margin-top: 10px;">
            <img id="preview" src="" style="max-width: 100%; max-height: 55vh; border: 1px solid #444; display: block;">
            <div id="overlay_logo_wrapper" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none;"></div>
        </div>
        <div id="thumb_strip" class="thumb-strip"></div>
        <div id="feedback" style="margin-top:10px; color:#10b981; font-weight:bold; height:20px;"></div>
    </div>

    <div class="column right">
        <h3>Director Tools -rendering takes about 3min- </h3>
        <button class="btn-green" onclick="ctrl('start', this)">NEW PRODUCTION</button>
        <button class="btn-blue" onclick="ctrl('resume', this)">RESUME SESSION</button>
        <button class="btn-orange" onclick="ctrl('pause', this)">PAUSE / UNPAUSE</button>
        <label>Inject Keyword</label><input id="inj" placeholder="neon"><button class="btn-blue" onclick="inject(this)">INJECT</button>
        
        <hr style="border:0; border-top:1px solid #333; margin:20px 0;">
        <h3>Temporary Caption</h3>
        <label>Caption Text</label>
        <textarea id="cap_text" rows="2" placeholder="Enter single line caption"></textarea>
        <label>Caption Font</label>
        <select id="cap_font">
            <option value="Default">Default PIL Font</option>
            {% for f in FONTS %}
            <option value="{{ f }}">{{ f }}</option>
            {% endfor %}
        </select>
        <label>Font Size</label>
        <input type="number" id="cap_font_size" value="20" min="8" max="72">
        <button class="btn-blue" onclick="setCaption(this)">INSERT CAPTION (5 FRAMES)</button>

        <hr style="border:0; border-top:1px solid #333; margin:20px 0;">
        <h3>Drag-and-Drop Logo Overlay</h3>
        <label>Upload Logo (transparent PNG)</label>
        <div style="display:flex; gap:5px;">
            <input type="file" id="logo_file_input" accept="image/png" style="flex:1;">
            <button class="btn-blue" onclick="uploadLogo(this)" style="width:auto; margin-top:4px;">Upload</button>
        </div>
        <label>Select Logo</label>
        <select id="logo_select" onchange="changeLogo(this.value)">
            <option value="None">None</option>
            {% for l in LOGOS %}
            <option value="{{ l }}" {% if l == CURRENT_LOGO %}selected{% endif %}>{{ l }}</option>
            {% endfor %}
        </select>
        <label>Logo Width (px)</label>
        <input type="number" id="logo_width_input" value="100" min="10" max="1000" oninput="changeLogoSize()">
        <label>Opacity</label>
        <input type="range" id="logo_opacity_slider" min="0" max="1" step="0.05" value="1.0" oninput="updateLogoOpacity(this.value)">
        <div id="logo_control_buttons" style="display:none; margin-top: 8px;">
            <button class="btn-green" onclick="saveLogoPosition()">Save Logo Position</button>
            <button class="btn-orange" onclick="cancelLogoPlacement()">Clear Logo</button>
        </div>

        <hr style="border:0; border-top:1px solid #333; margin:20px 0;">
        <h3>Teleport (One-Time)</h3>
        <label>Upload Planet Image</label>
        <input type="file" id="tele_file" accept="image/*">
        <button class="btn-orange" onclick="teleport(this)">TELEPORT NOW</button>

        <hr style="border:0; border-top:1px solid #333; margin:20px 0;">
        <h3>Keyframe Editor</h3>
        <label>Frame</label>
        <input type="number" id="kf_f" value="0">

        <label>Prompt Override</label>
        <textarea id="kf_p" rows="2" placeholder="optional prompt"></textarea>

        <label>Denoise</label>
        <input type="number" id="kf_d" step="0.05" value="0.5">

        <label>Seed Offset</label>
        <input type="number" id="kf_s" value="0">

        <button class="btn-blue" onclick="addKF(this)">ADD KEYFRAME</button>
        <div id="kf_list"></div>
    </div>

    <script>
        function showFeedback(t){ const f=document.getElementById('feedback'); f.innerText=t; setTimeout(()=>f.innerText='',7000); }
        async function ctrl(a,b){
            if (a === 'start' || a === 'resume' || a === 'pause') {
                try {
                    await fetch('/update_params',{
                        method:'POST',
                        headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({
                            model:document.getElementById('model').value,
                            negative_prompt:document.getElementById('neg_prompt').value,
                            lora1:document.getElementById('lora1').value,
                            seed:document.getElementById('seed').value,
                            denoise:document.getElementById('denoise').value,
                            frames:document.getElementById('frames').value,
                            steps:document.getElementById('steps').value,
                            cfg:document.getElementById('cfg').value,
                            use_motion_zoom: document.getElementById('use_zoom').checked,
                            use_metadata_caption: document.getElementById('use_caption').checked,
                            zoom_start:document.getElementById('zs').value,
                            zoom_end:document.getElementById('ze').value,
                            pan_start_x:document.getElementById('pxs').value,
                            pan_end_x:document.getElementById('pxe').value,
                            pan_start_y:document.getElementById('pys').value,
                            pan_end_y:document.getElementById('pye').value,
                            roll_mode:document.getElementById('roll_mode').value,
                            feedback_color:document.getElementById('fb_color').value,
                            feedback_contrast:document.getElementById('fb_contrast').value,
                            feedback_sharpness:document.getElementById('fb_sharpness').value
                        })
                    });
                } catch(e) { console.error("Autosave failed:", e); }
            }
            fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a, prompt:document.getElementById('prompt').value})})
            .then(()=>showFeedback("Action Sent: " + a.toUpperCase()));
        }
        function inject(b){ fetch('/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.getElementById('inj').value})}).then(()=>{document.getElementById('inj').value=''; showFeedback("Injected!");}); }
        function setCaption(b){
            fetch('/set_caption',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({
                    text:document.getElementById('cap_text').value,
                    font:document.getElementById('cap_font').value,
                    font_size:document.getElementById('cap_font_size').value
                })
            }).then(()=>{
                document.getElementById('cap_text').value='';
                showFeedback("Caption Armed!");
            });
        }
        function teleport(b){
            const fileInput = document.getElementById('tele_file');
            if (fileInput.files.length === 0) { alert("Please select an image first"); return; }
            const formData = new FormData();
            formData.append('image', fileInput.files[0]);
            fetch('/teleport', { method: 'POST', body: formData })
            .then(r => r.json())
            .then(d => {
                if (d.status === 'ok') {
                    showFeedback("Teleport Armed!");
                    fileInput.value = '';
                } else {
                    alert("Error: " + d.message);
                }
            });
        }
        function addKF(b){
            fetch('/add_keyframe',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({
                    frame:document.getElementById('kf_f').value,
                    prompt:document.getElementById('kf_p').value,
                    denoise:document.getElementById('kf_d').value,
                    seed_offset:document.getElementById('kf_s').value
                })
            })
            .then(r=>r.json())
            .then(d=>{
                showFeedback("KF Added!");
                updateKFList(d.keyframes);

                // clear inputs (feels much better when working fast)
                document.getElementById('kf_p').value = "";
                document.getElementById('kf_f').value = 0;
                document.getElementById('kf_d').value = 0.5;
                document.getElementById('kf_s').value = 0;
            });
        }
        function update(b){
            fetch('/update_params',{
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({
                    model:document.getElementById('model').value,
                    negative_prompt:document.getElementById('neg_prompt').value,
                    lora1:document.getElementById('lora1').value,
                    seed:document.getElementById('seed').value,
                    denoise:document.getElementById('denoise').value,
                    frames:document.getElementById('frames').value,
                    steps:document.getElementById('steps').value,
                    cfg:document.getElementById('cfg').value,
                    use_motion_zoom: document.getElementById('use_zoom').checked,
                    use_metadata_caption: document.getElementById('use_caption').checked,
                    zoom_start:document.getElementById('zs').value,
                    zoom_end:document.getElementById('ze').value,
                    pan_start_x:document.getElementById('pxs').value,
                    pan_end_x:document.getElementById('pxe').value,
                    pan_start_y:document.getElementById('pys').value,
                    pan_end_y:document.getElementById('pye').value,
                    roll_mode:document.getElementById('roll_mode').value,
                    feedback_color:document.getElementById('fb_color').value,
                    feedback_contrast:document.getElementById('fb_contrast').value,
                    feedback_sharpness:document.getElementById('fb_sharpness').value
                })
            }).then(()=>showFeedback("Parameters Updated"));
        }
        function updateKFList(kfs){ const l = document.getElementById('kf_list');
        l.innerHTML = '<h4>Active KFs</h4>';
        Object.keys(kfs).sort((a,b)=>a-b).forEach(f=>{
            l.innerHTML += `<div class="kf-item">
                <b>F${f}</b>: D:${kfs[f].denoise}<br>
                <span style="color:#aaa;">${kfs[f].prompt || '(no prompt override)'}</span>
            </div>`;
        });  }

        let isDraggingLogo = false;
        let startX = 0, startY = 0;
        let logoLeft = 0, logoTop = 0;
        let dragLogoEl = null;
        let currentLogoFilename = "{{ CURRENT_LOGO }}";
        let currentLogoX = 0;
        let currentLogoY = 0;
        let currentLogoW = 100;
        let currentLogoH = 100;
        let currentLogoOpacity = 1.0;
        let currentFrameWidth = 340;
        let currentFrameHeight = 512;

        function uploadLogo(btn) {
            const fileInput = document.getElementById('logo_file_input');
            if (fileInput.files.length === 0) { alert("Please select a PNG image first"); return; }
            const formData = new FormData();
            formData.append('logo', fileInput.files[0]);
            fetch('/upload_logo', { method: 'POST', body: formData })
            .then(r => r.json())
            .then(d => {
                if (d.status === 'ok') {
                    showFeedback("Logo uploaded successfully!");
                    fileInput.value = '';
                    setTimeout(() => location.reload(), 1000);
                } else {
                    alert("Error: " + d.message);
                }
            });
        }

        function initDraggableLogo() {
            dragLogoEl = document.getElementById("draggable_logo");
            if (!dragLogoEl) return;
            dragLogoEl.addEventListener("pointerdown", (e) => {
                e.preventDefault();
                isDraggingLogo = true;
                dragLogoEl.setPointerCapture(e.pointerId);
                const rect = dragLogoEl.getBoundingClientRect();
                const parentRect = document.getElementById("overlay_logo_wrapper").getBoundingClientRect();
                startX = e.clientX;
                startY = e.clientY;
                logoLeft = rect.left - parentRect.left;
                logoTop = rect.top - parentRect.top;
            });
            dragLogoEl.addEventListener("pointermove", (e) => {
                if (!isDraggingLogo) return;
                e.preventDefault();
                const parentRect = document.getElementById("overlay_logo_wrapper").getBoundingClientRect();
                const deltaX = e.clientX - startX;
                const deltaY = e.clientY - startY;
                let newX = logoLeft + deltaX;
                let newY = logoTop + deltaY;
                const maxW = parentRect.width - dragLogoEl.offsetWidth;
                const maxH = parentRect.height - dragLogoEl.offsetHeight;
                newX = Math.max(0, Math.min(newX, maxW));
                newY = Math.max(0, Math.min(newY, maxH));
                dragLogoEl.style.left = newX + "px";
                dragLogoEl.style.top = newY + "px";
            });
            dragLogoEl.addEventListener("pointerup", (e) => {
                if (isDraggingLogo) {
                    dragLogoEl.releasePointerCapture(e.pointerId);
                    isDraggingLogo = false;
                }
            });
        }

        function changeLogo(filename) {
            if (filename === "None") {
                currentLogoFilename = "None";
                updateLogoOverlayDisplay();
                fetch("/save_logo_position", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ logo_filename: "None" })
                });
                return;
            }
            currentLogoFilename = filename;
            currentLogoX = 20;
            currentLogoY = 20;
            currentLogoW = 100;
            currentLogoH = 100;
            currentLogoOpacity = 1.0;
            updateLogoOverlayDisplay();
        }

        function changeLogoSize() {
            const el = document.getElementById("draggable_logo");
            const widthInput = document.getElementById("logo_width_input");
            if (!el || !widthInput) return;
            const w = parseInt(widthInput.value) || 100;
            const ratio = el.naturalHeight / el.naturalWidth;
            const h = Math.round(w * ratio);
            el.style.width = w + "px";
            el.style.height = h + "px";
        }

        function updateLogoOpacity(val) {
            currentLogoOpacity = parseFloat(val);
            const el = document.getElementById("draggable_logo");
            if (el) el.style.opacity = currentLogoOpacity;
        }

        function cancelLogoPlacement() {
            changeLogo("None");
        }

        async function saveLogoPosition() {
            const el = document.getElementById("draggable_logo");
            const container = document.getElementById("overlay_logo_wrapper");
            if (!el || !container) return;
            const parentRect = container.getBoundingClientRect();
            const elRect = el.getBoundingClientRect();
            const scaleX = currentFrameWidth / parentRect.width;
            const scaleY = currentFrameHeight / parentRect.height;
            const x = Math.round((elRect.left - parentRect.left) * scaleX);
            const y = Math.round((elRect.top - parentRect.top) * scaleY);
            const w = Math.round(elRect.width * scaleX);
            const h = Math.round(elRect.height * scaleY);
            const logo_filename = document.getElementById("logo_select").value;
            const logo_opacity = parseFloat(document.getElementById("logo_opacity_slider").value) || 1.0;
            const payload = {
                logo_filename: logo_filename,
                x: x, y: y, w: w, h: h,
                opacity: logo_opacity
            };
            const resp = await fetch("/save_logo_position", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (resp.ok) {
                showFeedback("Logo position saved!");
            } else {
                alert("Failed to save logo position.");
            }
        }

        function updateLogoOverlayDisplay() {
            if (isDraggingLogo) return;
            const select = document.getElementById("logo_select");
            const container = document.getElementById("overlay_logo_wrapper");
            if (!select || !container) return;
            if (select.value !== currentLogoFilename) {
                select.value = currentLogoFilename;
            }
            document.getElementById("logo_opacity_slider").value = currentLogoOpacity;
            if (currentLogoFilename === "None") {
                container.innerHTML = "";
                document.getElementById("logo_control_buttons").style.display = "none";
                return;
            }
            document.getElementById("logo_control_buttons").style.display = "block";
            let el = document.getElementById("draggable_logo");
            if (!el) {
                el = document.createElement("img");
                el.id = "draggable_logo";
                el.className = "draggable";
                el.style.position = "absolute";
                el.style.pointerEvents = "auto";
                el.style.cursor = "move";
                el.style.outline = "2px dashed #3b82f6";
                container.appendChild(el);
                initDraggableLogo();
            }
            const expectedSrc = "/static/overlays/" + currentLogoFilename;
            if (!el.src.endsWith(expectedSrc)) {
                el.src = expectedSrc;
            }
            const parentRect = container.getBoundingClientRect();
            if (parentRect.width > 0 && parentRect.height > 0) {
                const scaleX = parentRect.width / currentFrameWidth;
                const scaleY = parentRect.height / currentFrameHeight;
                el.style.left = Math.round(currentLogoX * scaleX) + "px";
                el.style.top = Math.round(currentLogoY * scaleY) + "px";
                el.style.width = Math.round(currentLogoW * scaleX) + "px";
                el.style.height = Math.round(currentLogoH * scaleY) + "px";
                el.style.opacity = currentLogoOpacity;
                document.getElementById("logo_width_input").value = currentLogoW;
            }
        }

        function loadOllamaModels() {
            fetch('/get_ollama_models')
            .then(r => r.json())
            .then(d => {
                const select = document.getElementById("ollama_model");
                select.innerHTML = "";
                if (d.models && d.models.length > 0) {
                    d.models.forEach(m => {
                        const opt = document.createElement("option");
                        opt.value = m;
                        opt.text = m;
                        select.appendChild(opt);
                    });
                    const preferred = ["dolphin3:8b", "mistral:7b-instruct", "llama3.2:3b", "qwen3:8b"];
                    for (let p of preferred) {
                        for (let opt of select.options) {
                            if (opt.value.includes(p)) {
                                select.value = opt.value;
                                return;
                            }
                        }
                    }
                } else {
                    const opt = document.createElement("option");
                    opt.value = "None";
                    opt.text = "No Ollama models found / offline";
                    select.appendChild(opt);
                }
            });
        }

        function enhancePrompt() {
            const promptArea = document.getElementById("prompt");
            const modelSelect = document.getElementById("ollama_model");
            if (modelSelect.value === "None") {
                alert("No Ollama model selected or Ollama is offline.");
                return;
            }
            showFeedback("Enhancing prompt with AI...");
            fetch("/enhance_prompt", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    prompt: promptArea.value,
                    model: modelSelect.value
                })
            })
            .then(r => r.json())
            .then(d => {
                if (d.status === "ok") {
                    promptArea.value = d.enhanced_prompt;
                    showFeedback("Prompt enhanced successfully!");
                } else {
                    alert("Enhancement failed: " + d.error);
                }
            })
            .catch(e => {
                alert("Enhancement failed: " + e);
            });
        }

        document.addEventListener("DOMContentLoaded", () => {
            loadOllamaModels();
        });

        let initialSyncDone = false;

        setInterval(()=>{
            fetch('/status').then(r=>r.json()).then(d=>{
                let statusMsg = d.running ? (d.paused ? "PAUSED" : "RENDERING") : "IDLE";
                
                // Add Step Progress if rendering
                if (d.running && !d.paused && d.max_steps > 0) {
                    statusMsg += ` (Step ${d.progress}/${d.max_steps})`;
                }
                
                const pauseSyncEl = document.getElementById('pause_ui_sync');
                const pauseSync = (pauseSyncEl && pauseSyncEl.checked) || !d.running || d.paused;
                
                const displayStatus = (pauseSync && d.running && !d.paused) ? statusMsg + " (Sync Paused)" : statusMsg;
                document.getElementById('status_text').innerText = displayStatus + " " + d.frame + "/" + d.total;
                
                if(d.frame>0) document.getElementById('preview').src='/latest_frame?t='+Date.now();
                if(d.history) document.getElementById('thumb_strip').innerHTML = d.history.map(i=>`<img src="/static/streamer/${i}?t=${Date.now()}">`).join('');
                if(d.injections) document.getElementById('injections').innerHTML = d.injections.map(i=>`<span class="tag">${i}</span>`).join('');
                
                if (!pauseSync || !initialSyncDone) {
                    // Sync caption checkbox
                    if (d.metadata_caption !== undefined) document.getElementById('use_caption').checked = d.metadata_caption;
                    
                    // Sync Logo
                    if (d.logo_filename !== undefined) {
                        currentLogoFilename = d.logo_filename;
                        currentLogoX = d.logo_x;
                        currentLogoY = d.logo_y;
                        currentLogoW = d.logo_w;
                        currentLogoH = d.logo_h;
                        currentLogoOpacity = d.logo_opacity;
                    }
                    if (d.width !== undefined) currentFrameWidth = d.width;
                    if (d.height !== undefined) currentFrameHeight = d.height;
                    updateLogoOverlayDisplay();
                    
                    initialSyncDone = true;
                }
            });
        }, 2000);
    </script>
</body></html>
"""
# -------------------------------------------------


# --------------------------------------------------
# The Sound Stage
# --------------------------------------------------


# --------------------------------------------------
# VOICES
# --------------------------------------------------
VOICES = ['af_bella','af_sarah','am_adam','bm_george']
DEFAULT_VOICE = "am_adam"

# --------------------------------------------------
# PATHS
# --------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

SHARED_PATH = os.path.join(BASE_DIR, "static", "streamer")
FRAME_PATH = os.path.join(BASE_DIR, "static", "assets", "transtalks.png")

os.makedirs(SHARED_PATH, exist_ok=True)

UNIQUE = str(randint(100000, 999999))

# --------------------------------------------------
# HTML
# --------------------------------------------------
HTML = """
<!doctype html>
<html>
<head>
<style>
body { background:#111; color:#eee; font-family:Arial; text-align:center; }
.box { display:inline-block; margin:10px; padding:10px; border:1px solid #333; }
img, video { width:300px; }
button { padding:10px 20px; margin-top:20px; }
a { font-size:3vw;color:orange; }
</style>
</head>
<body>

<h1>Video Builder</h1>

<a href="/download_mp4">Download MP4</a><br>
<a href="/text_to_mp3">Generate MP3</a>

<form method="post" action="/process_video">

<h2>Videos</h2>
{% for v in videos %}
<div class="box">
<video controls src="{{ url_for('static', filename='streamer/' + v) }}"></video><br>
<input type="radio" name="video" value="{{v}}">
{{v}}
</div>
{% endfor %}

<h2>Images</h2>
<div style="margin-bottom: 15px;">
    <button type="button" onclick="selectAllImages(true)" style="margin-right: 10px; padding: 6px 12px; margin-top: 0; width: auto; font-weight: bold; cursor: pointer;">Select All</button>
    <button type="button" onclick="selectAllImages(false)" style="padding: 6px 12px; margin-top: 0; width: auto; font-weight: bold; cursor: pointer;">Deselect All</button>
</div>
{% for img in images %}
<div class="box">
<img src="{{ url_for('static', filename='streamer/' + img) }}"><br>
<input type="checkbox" name="images" value="{{ img }}">
</div>
{% endfor %}

<h2>Audio</h2>
{% for mp3 in mp3s %}
<div class="box" style="vertical-align: top;">
<audio controls style="width:250px; display:block; margin: 0 auto 8px auto;">
    <source src="{{ url_for('static', filename='streamer/' + mp3) }}" type="audio/mpeg">
</audio>
<input type="radio" name="audio" value="{{ mp3 }}">
{{ mp3 }}
</div>
{% endfor %}

<br>
<button type="submit">PROCESS</button>
</form>

<script>
function selectAllImages(checked) {
    const checkboxes = document.querySelectorAll('input[name="images"]');
    checkboxes.forEach(cb => cb.checked = checked);
}
</script>
</body>
</html>
"""

# --------------------------------------------------
# HOME
# --------------------------------------------------
@app.route("/media")
def media():
    import os
    from icecream import ic

    # --------------------------------------------------
    # GET FILES
    # --------------------------------------------------
    files = os.listdir(SHARED_PATH)
    ic("ALL FILES:", files)

    # --------------------------------------------------
    # HELPER: GET FILE TIME
    # --------------------------------------------------
    def get_mtime(filename):
        full_path = os.path.join(SHARED_PATH, filename)
        try:
            mtime = os.path.getmtime(full_path)
            ic(f"mtime for {filename}:", mtime)
            return mtime
        except Exception as e:
            ic(f"ERROR reading mtime for {filename}:", e)
            return 0

    # --------------------------------------------------
    # FILTER FILE TYPES
    # --------------------------------------------------
    images = [
        f for f in files 
        if f.lower().endswith((".jpg", ".png")) 
        and not f.startswith("temp_clean_")
        and not f.startswith("clean_")
    ]
    mp3s   = [f for f in files if f.lower().endswith(".mp3")]
    videos = [f for f in files if f.lower().endswith(".mp4")]

    ic("UNSORTED IMAGES:", images)
    ic("UNSORTED MP3S:", mp3s)
    ic("UNSORTED VIDEOS:", videos)

    # --------------------------------------------------
    # SORT BY DATE (NEWEST FIRST)
    # --------------------------------------------------
    images.sort(key=get_mtime, reverse=True)
    mp3s.sort(key=get_mtime, reverse=True)
    videos.sort(key=get_mtime, reverse=True)

    ic("SORTED IMAGES:", images)
    ic("SORTED MP3S:", mp3s)
    ic("SORTED VIDEOS:", videos)

    # --------------------------------------------------
    # RETURN TEMPLATE
    # --------------------------------------------------
    return render_template_string(
        HTML,
        images=images,
        mp3s=mp3s,
        videos=videos
    )
'''    
@app.route("/media")
def media():
    files = os.listdir(SHARED_PATH)

    images = [f for f in files if f.endswith((".jpg",".png"))]
    mp3s   = [f for f in files if f.endswith(".mp3")]
    videos = [f for f in files if f.endswith(".mp4")]

    ic(images, mp3s, videos)

    return render_template_string(
        HTML,
        images=images,
        mp3s=mp3s,
        videos=videos
    )
'''
# --------------------------------------------------
# DOWNLOAD MP4
# --------------------------------------------------
@app.route("/download_mp4", methods=["GET","POST"])
def download_mp4():
    if request.method == "POST":

        # file upload instead of URL
        if "file" not in request.files:
            return "No file part", 400

        file = request.files["file"]

        if file.filename == "":
            return "No selected file", 400

        filename = secure_filename(file.filename)
        out = os.path.join(SHARED_PATH, filename)

        ic("Uploading file:", filename)

        file.save(out)

        return redirect("/")

    return """
    <h2>Upload MP4 from your computer or LAN</h2>

    <form method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".mp4"><br><br>
        <button>Upload</button>
    </form>
    """
# --------------------------------------------------
# AUDIO PAD
# --------------------------------------------------
def pad_audio(src, out):
    audio = AudioSegment.from_mp3(src)
    silence = AudioSegment.silent(duration=200)
    (silence + audio + silence).export(out, format="mp3")

# --------------------------------------------------
# PROCESS VIDEO
# --------------------------------------------------
@app.route("/process_video", methods=["POST"])
def process_video():

    selected_video = request.form.get("video")
    selected_audio = request.form.get("audio")
    selected_images = request.form.getlist("images")

    ic(selected_video, selected_audio, selected_images)

    audio_path = os.path.join(SHARED_PATH, selected_audio)
    padded_audio = os.path.join(SHARED_PATH, "_pad.mp3")

    pad_audio(audio_path, padded_audio)

    audio_clip = AudioFileClip(padded_audio)

    # ------------------------------------------
    # CASE 1: EXISTING VIDEO
    # ------------------------------------------
    if selected_video:

        video_path = os.path.join(SHARED_PATH, selected_video)
        video = VideoFileClip(video_path)

        ic("Original duration:", video.duration)
        ic("Audio duration:", audio_clip.duration)

        speed_factor = video.duration / audio_clip.duration
        ic("Speed factor:", speed_factor)

        new_video = video.fx(vfx.speedx, speed_factor)

        final = new_video.set_audio(audio_clip)

    # ------------------------------------------
    # CASE 2: IMAGE SLIDESHOW
    # ------------------------------------------
    else:
        clips = []
        duration = audio_clip.duration / len(selected_images)

        for img in selected_images:
            p = os.path.join(SHARED_PATH, img)
            clip = ImageClip(p).set_duration(duration)
            clips.append(clip)

        final = concatenate_videoclips(clips).set_audio(audio_clip)

    # ------------------------------------------
    # WRITE OUTPUT
    # ------------------------------------------
    out = os.path.join(SHARED_PATH, f"{UNIQUE}_output.mp4")

    ic("Writing:", out)

    final.write_videofile(
        out,
        fps=24,
        codec="libx264",
        audio_codec="aac"
    )

    return redirect(url_for("serve_video", filename=os.path.basename(out)))

# --------------------------------------------------
# SERVE
# --------------------------------------------------
@app.route("/video/<filename>")
def serve_video(filename):
    return send_file(os.path.join(SHARED_PATH, filename))

# --------------------------------------------------
# TTS
# --------------------------------------------------
@app.route("/text_to_mp3", methods=["GET","POST"])
def text_to_mp3():

    if request.method == "POST":
        text = request.form.get("text")
        voice = request.form.get("voice")

        out = os.path.join(SHARED_PATH, text[:20].replace(" ","_")+".mp3")

        payload = {
            "model": "kokoro",
            "voice": voice,
            "input": text
        }

        r = requests.post(
            "http://localhost:8880/v1/audio/speech",
            json=payload
        )

        with open(out, "wb") as f:
            f.write(r.content)

        return redirect("/")

    return """
    <form method="post">
    <textarea name="text" style="width:60%"></textarea><br>
    <select name="voice">
        <option>af_bella</option>
        <option>am_adam</option>
    </select><br>
    <button>Generate</button>
    </form>
    """



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False, use_reloader=False)
