# EpochStreamer.py
# https://gist.github.com/JupyterJones/e67061cf3c36a1bf0968cf739b196a88
import os
import sys
import time
import json
import random
from random import randint
import math
import requests
import traceback
import io
import datetime
import uuid
import websocket
import re
import numpy as np
from threading import Thread, Lock
from PIL import Image, ImageFilter, ImageDraw
from flask import (
    Flask,
    render_template_string,
    request,
    jsonify,
    send_file,
    redirect,
    flash,
    url_for
)
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
import subprocess
# ============================================
# CONFIG & PATHS
# ============================================
COMFY_URL = "http://192.168.1.41:5001"
CLIENT_ID = str(uuid.uuid4())
# Global progress state
comfy_progress = 0
comfy_max_steps = 0
DEFAULT_WIDTH = 512
DEFAULT_HEIGHT = 336
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Ensure static/streamer exists
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "streamer512")
STATE_FILE = os.path.join(OUTPUT_DIR, "streamer512.json")
LOG_FILE_PATH = os.path.join(OUTPUT_DIR, "streamer512.txt")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# ============================================================
# GLOBAL STATE
# ============================================================
'''
Highly detailed cinematic Dr Seuss style fantasy illustration, whimsical park landscape with surreal curved trees and spiral plants, a unique Flutegoose creature (goose hybrid with flute-like feather patterns) walking toward a glowing magical waterfall, fantasy pond with pastel reflections, multiple Dr Seuss inspired quirky characters throughout the prompt for this series of images is. The scene, tall lanky beings and strange animal hybrids interacting playfully, floating elements, storybook surreal geometry, vibrant pastel palette, soft volumetric lighting, wide angle composition, ultra detailed, hand painted illustration style, sharp focus, depth, cinematic lighting.
The negative prompt is .  low quality, blurry, dull colors, realistic, photorealism, modern clothing, text, watermark, logo, distorted anatomy, extra limbs, ugly
'''
state_lock = Lock()
render_lock = Lock()
running = False
paused = False
current_frame = 0
frames_current = 1500
current_seed = random.randint(111111, 999999)
model_name = "dreamshaper_8.safetensors"
vae_name = "vae-ft-mse-840000-ema-pruned.safetensors"
lora1_name = "more_details.safetensors"
lora1_strength = 0.8
lora2_name = "None"
lora2_strength = 0.8
lora3_name = "None"
caption_font_size = 12
caption_x = 12
caption_y = 12
caption_bg_r = 61
caption_bg_g = 81
caption_bg_b = 92
caption_bg_a = 0.4
temp_caption_font_size = 14
temp_caption_x = 20
temp_caption_y = 10
temp_caption_bg_r = 0
temp_caption_bg_g = 0
temp_caption_bg_b = 0
temp_caption_bg_a = 0.5
denoise_current = 0.35
teleport_image = None
active_caption = ""
caption_remaining = 0
current_prompt = "cartoon illustration, whimsical storybook art, Dr Seuss inspired fantasy world, Flutgoose walking along a winding path toward a waterfall, colorful Seuss-like creatures everywhere, exaggerated curved trees, impossible architecture, playful fantasy park, bold black ink outlines, hand drawn children's book illustration, bright flat colors, whimsical characters, funny expressions, comic style, pen and ink drawing, detailed linework, classic illustrated storybook page, imaginative fantasy landscape, vibrant cartoon world"
def logit(*args):
    try:
        msg = " ".join(map(str, args))
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE_PATH, "a") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass
logit("EpochStreamerLandscape512back.py")    

def get_frame_num(filename):
    try:
        return int(filename.split("_")[1].split(".")[0])
    except (IndexError, ValueError):
        return -1
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
MODELS = []
LORAS = ["None"]

def refresh_models_and_loras():
    global MODELS, LORAS
    try:
        MODELS = requests.get(f"{COMFY_URL}/models/checkpoints", timeout=60).json()
        LORAS = ["None"] + requests.get(f"{COMFY_URL}/models/loras", timeout=60).json()
    except Exception as e:
        logit(f"Failed to fetch models/loras from ComfyUI: {e}")
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
def create_movie_from_frames(output_filename=None):
    """
    Joins the generated images into a movie file using ffmpeg.
    """
    if output_filename is None:
        run_uid = str(random.randint(111111, 999999))
        output_filename = f"production_{run_uid}.mp4"
    logit(f"Joining images to create movie: {output_filename}...")
    try:
        # Search for frames and compile
        cmd = [
            "ffmpeg", "-y",
            "-framerate", "5",
            "-i", os.path.join(OUTPUT_DIR, "frame_%03d.png"),
            "-vf", "scale=768:512,unsharp=5:5:1.5:5:5:0.0",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
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
    # 1. Apply periodic background overlay if requested
    if overlay_png_path and os.path.exists(overlay_png_path):
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
    
    # 5. Apply Sharpening to counteract scaling and rotation softness
    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=100, threshold=3))
    
    return img.convert("RGB"), (curr_zoom, curr_pan_x, curr_pan_y)
def draw_metadata_caption(img, frame_idx, total_frames, metadata, curr_zoom, curr_pan_x, curr_pan_y):
    try:
        from PIL import ImageDraw, ImageFont
        # Create an RGBA version of the image to support transparency in drawing
        img_rgba = img.convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        lines = [
            f"Frame: {frame_idx} of {total_frames} - Seed: {metadata.get('seed')} - Step: {metadata.get('steps')} - CFG: {metadata.get('cfg')}",
            f"Denoise: {metadata.get('denoise'):.2f} - Zoom: {curr_zoom:.3f} - Yaw: {curr_pan_x:.2f} - Pitch: {curr_pan_y:.2f}"
        ]
        text = "\n".join(lines)
        
        font = ImageFont.load_default(size=caption_font_size)
        
        # Calculate bounding box of the multiline text
        text_x = caption_x + 10
        text_y = caption_y + 6
        left, top, right, bottom = draw.multiline_textbbox((text_x, text_y), text, font=font)
        
        # Create box with padding
        box_left = left - 10
        box_top = top - 6
        box_right = right + 10
        box_bottom = bottom + 6
        
        # Convert opacity from 0.0-1.0 to 0-255
        alpha_val = int(caption_bg_a * 255)
        bg_color = (caption_bg_r, caption_bg_g, caption_bg_b, alpha_val)
        
        draw.rectangle(
            [box_left, box_top, box_right, box_bottom],
            fill=bg_color
        )
        draw.multiline_text(
            (text_x, text_y),
            text,
            fill=(255, 255, 255, 255),
            font=font
        )
        img_rgba = Image.alpha_composite(img_rgba, overlay)
        return img_rgba.convert("RGB")
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
def apply_border(img, border_path="static/border3.png"):
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
def draw_top_caption(img, text):
    """
    Draws text with customized font size, position, and background color.
    """
    if not text:
        return img
    try:
        from PIL import ImageDraw, ImageFont
        # Create an RGBA version of the image to support transparency in drawing
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        font = ImageFont.load_default(size=temp_caption_font_size)
        
        text_x = temp_caption_x
        text_y = temp_caption_y
        
        # Calculate bounding box of the text to draw the background box
        left, top, right, bottom = draw.multiline_textbbox((text_x, text_y), text, font=font)
        
        # Add padding to background box
        box_left = left - 10
        box_top = top - 6
        box_right = right + 10
        box_bottom = bottom + 6
        
        alpha_val = int(temp_caption_bg_a * 255)
        bg_color = (temp_caption_bg_r, temp_caption_bg_g, temp_caption_bg_b, alpha_val)
        
        draw.rectangle([box_left, box_top, box_right, box_bottom], fill=bg_color)
        draw.multiline_text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)
        
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
            "lora1_strength": lora1_strength,
            "lora2": lora2_name,
            "lora2_strength": lora2_strength,
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
            "caption_font_size": caption_font_size,
            "caption_x": caption_x,
            "caption_y": caption_y,
            "caption_bg_r": caption_bg_r,
            "caption_bg_g": caption_bg_g,
            "caption_bg_b": caption_bg_b,
            "caption_bg_a": caption_bg_a,
            "temp_caption_font_size": temp_caption_font_size,
            "temp_caption_x": temp_caption_x,
            "temp_caption_y": temp_caption_y,
            "temp_caption_bg_r": temp_caption_bg_r,
            "temp_caption_bg_g": temp_caption_bg_g,
            "temp_caption_bg_b": temp_caption_bg_b,
            "temp_caption_bg_a": temp_caption_bg_a
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except:
            pass
def load_state():
    global current_frame, current_seed, current_prompt, negative_prompt, model_name, denoise_current
    global keyframes, injection_lines, frames_current, lora1_name, lora2_name, lora3_name
    global lora1_strength, lora2_strength
    global caption_font_size, caption_x, caption_y, caption_bg_r, caption_bg_g, caption_bg_b, caption_bg_a
    global temp_caption_font_size, temp_caption_x, temp_caption_y, temp_caption_bg_r, temp_caption_bg_g, temp_caption_bg_b, temp_caption_bg_a
    global use_motion_zoom, zoom_start, zoom_end, pan_start_x, pan_end_x, pan_start_y, pan_end_y
    global default_steps, default_cfg, use_metadata_caption, roll_mode
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        with state_lock:
            current_frame = state.get("current_frame", 0)
            frames_current = state.get("frames_total", 1500)
            current_seed = state.get("seed", 999)
            current_prompt = state.get("prompt", "")
            negative_prompt = state.get("negative_prompt", "")
            model_name = state.get("model", "")
            lora1_name = state.get("lora1", "None")
            lora1_strength = float(state.get("lora1_strength", 0.8))
            lora2_name = state.get("lora2", "None")
            lora2_strength = float(state.get("lora2_strength", 0.8))
            lora3_name = state.get("lora3", "None")
            caption_font_size = int(state.get("caption_font_size", 12))
            caption_x = int(state.get("caption_x", 12))
            caption_y = int(state.get("caption_y", 12))
            caption_bg_r = int(state.get("caption_bg_r", 61))
            caption_bg_g = int(state.get("caption_bg_g", 81))
            caption_bg_b = int(state.get("caption_bg_b", 92))
            caption_bg_a = float(state.get("caption_bg_a", 0.4))
            temp_caption_font_size = int(state.get("temp_caption_font_size", 14))
            temp_caption_x = int(state.get("temp_caption_x", 20))
            temp_caption_y = int(state.get("temp_caption_y", 10))
            temp_caption_bg_r = int(state.get("temp_caption_bg_r", 0))
            temp_caption_bg_g = int(state.get("temp_caption_bg_g", 0))
            temp_caption_bg_b = int(state.get("temp_caption_bg_b", 0))
            temp_caption_bg_a = float(state.get("temp_caption_bg_a", 0.5))
            denoise_current = state.get("denoise", 0.35)
            keyframes = state.get("keyframes", {})
            injection_lines = state.get("injection_lines", [])
            use_motion_zoom = state.get("use_motion_zoom", True)
            use_metadata_caption = state.get("use_metadata_caption", False)
            zoom_start = state.get("z_s", 1.0)
            zoom_end = state.get("z_e", 1.1)
            pan_start_x = state.get("px_s", 0.5)
            pan_end_x = state.get("px_e", 0.5)
            pan_start_y = state.get("py_s", 0.5)
            pan_end_y = state.get("py_e", 0.5)
            roll_mode = state.get("roll_mode", "none")
        # CRITICAL: Detect actual last frame on disk
        files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")], key=get_frame_num)
        if files:
            last_file = files[-1]
            try:
                # Extract number from frame_XXX.png
                last_disk_frame = get_frame_num(last_file)
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
    lora_configs = [
        (lora1_name, lora1_strength),
        (lora2_name, lora2_strength),
        (lora3_name, 0.8)
    ]
    for i, (ln, lst) in enumerate(lora_configs):
        if ln and ln != "None":
            nid = f"lora_{i}"
            wf[nid] = {"inputs": {"lora_name": ln, "strength_model": lst, "strength_clip": lst, "model": lm, "clip": lc}, "class_type": "LoraLoader"}
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
    if running: 
        logit("REJECTED: Engine already running.")
        return
    
    try:
        logit("ENGINE STARTING...")
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
                        up_resp = requests.post(f"{COMFY_URL}/upload/image", files={"image": ("init.png", f)}, timeout=2000)
                        if up_resp.status_code == 200:
                            up = up_resp.json()
                            last_server_filename = up.get("name")
                            logit(f"Uploaded resume init image: {last_server_filename}")
                        else:
                            logit(f"Upload init error: {up_resp.status_code} - {up_resp.text}")
                except Exception as e:
                    logit(f"Error uploading init image: {e}")
        
        logit(f"LOOP ENTERED: Start Frame {current_frame} / {frames_current}")
        while current_frame < frames_current:
            if not running: 
                logit("LOOP TERMINATED: running is False")
                break
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
                r_post = requests.post(f"{COMFY_URL}/prompt", json={"prompt": wf, "client_id": CLIENT_ID}, timeout=2000)
                if r_post.status_code != 200:
                    logit(f"ComfyUI Error (Status {r_post.status_code}): {r_post.text}")
                    time.sleep(5)
                    continue
                resp = r_post.json()
                pid = resp["prompt_id"]
                logit(f"Prompt sent (Frame {current_frame}). PID: {pid}")
            except Exception as e:
                logit(f"Error sending prompt: {e}")
                time.sleep(5)
                continue
            
            image_info = None
            # Wait for either WebSocket signal or poll fallback
            for i in range(28800): 
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
                # Apply slight color and contrast enhancement to counteract VAE desaturation drift
                from PIL import ImageEnhance
                img = ImageEnhance.Color(img).enhance(1.03)      # 3% saturation boost
                img = ImageEnhance.Contrast(img).enhance(1.015)   # 1.5% contrast boost
                
                clean_path = os.path.join(OUTPUT_DIR, f"clean_{current_frame:03d}.png")
                img.save(clean_path)
                with open(clean_path, "rb") as f:
                    up_resp = requests.post(f"{COMFY_URL}/upload/image", files={"image": (f"f_{current_frame}.png", f)}, timeout=2000)
                    if up_resp.status_code == 200:
                        up = up_resp.json()
                        last_server_filename = up.get("name")
                    else:
                        logit(f"Clean image upload error: {up_resp.status_code}")
                        break
                
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
                        local_img = draw_top_caption(local_img, active_caption)
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
                logit(traceback.format_exc())
                break
    except Exception as e:
        logit(f"CRITICAL RENDER ERROR: {e}")
        logit(traceback.format_exc())
    finally:
        running = False
        logit("ENGINE STOPPED.")
# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)
@app.route("/")
def index():
    if not MODELS or len(LORAS) <= 1:
        refresh_models_and_loras()
    return render_template_string(
        HTML_UI,
        MODELS=MODELS,
        LORAS=LORAS,
        lora1_name=lora1_name,
        lora1_strength=lora1_strength,
        lora2_name=lora2_name,
        lora2_strength=lora2_strength,
        caption_font_size=caption_font_size,
        caption_x=caption_x,
        caption_y=caption_y,
        caption_bg_r=caption_bg_r,
        caption_bg_g=caption_bg_g,
        caption_bg_b=caption_bg_b,
        caption_bg_a=caption_bg_a,
        temp_caption_font_size=temp_caption_font_size,
        temp_caption_x=temp_caption_x,
        temp_caption_y=temp_caption_y,
        temp_caption_bg_r=temp_caption_bg_r,
        temp_caption_bg_g=temp_caption_bg_g,
        temp_caption_bg_b=temp_caption_bg_b,
        temp_caption_bg_a=temp_caption_bg_a
    )
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
    global model_name, negative_prompt, lora1_name, lora2_name, lora3_name, current_seed, denoise_current, frames_current
    global lora1_strength, lora2_strength
    global caption_font_size, caption_x, caption_y, caption_bg_r, caption_bg_g, caption_bg_b, caption_bg_a
    global zoom_start, zoom_end, pan_start_x, pan_end_x, pan_start_y, pan_end_y, default_steps, default_cfg, use_motion_zoom, use_metadata_caption
    global roll_mode
    d = request.json
    caption_font_size = int(d.get("caption_font_size", caption_font_size))
    caption_x = int(d.get("caption_x", caption_x))
    caption_y = int(d.get("caption_y", caption_y))
    caption_bg_r = int(d.get("caption_bg_r", caption_bg_r))
    caption_bg_g = int(d.get("caption_bg_g", caption_bg_g))
    caption_bg_b = int(d.get("caption_bg_b", caption_bg_b))
    caption_bg_a = float(d.get("caption_bg_a", caption_bg_a))
    model_name = d.get("model", model_name)
    negative_prompt = d.get("negative_prompt", negative_prompt)
    lora1_name = d.get("lora1", lora1_name)
    lora1_strength = float(d.get("lora1_strength", lora1_strength))
    lora2_name = d.get("lora2", lora2_name)
    lora2_strength = float(d.get("lora2_strength", lora2_strength))
    lora3_name = d.get("lora3", lora3_name)
    current_seed = int(d.get("seed", current_seed))
    denoise_current = float(d.get("denoise", denoise_current))
    frames_current = int(d.get("frames", frames_current))
    use_motion_zoom = bool(d.get("use_motion_zoom"))
    use_metadata_caption = bool(d.get("use_metadata_caption"))
    zoom_start = float(d.get("zoom_start", zoom_start))
    zoom_end = float(d.get("zoom_end", zoom_end))
    pan_start_x = float(d.get("pan_start_x", pan_start_x))
    pan_end_x = float(d.get("pan_end_x", pan_end_x))
    pan_start_y = float(d.get("pan_start_y", pan_start_y))
    pan_end_y = float(d.get("pan_end_y", pan_end_y))
    roll_mode = d.get("roll_mode", roll_mode)
    default_steps = int(d.get("steps", default_steps))
    default_cfg = float(d.get("cfg", default_cfg))
    save_state()
    return jsonify({"status": "ok"})
@app.route("/status")
def status_route():
    files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")], key=get_frame_num)
    history = files[-5:] if len(files) > 0 else []
    history.reverse()
    return jsonify({
        "running": running, "paused": paused, "frame": current_frame, "total": frames_current,
        "history": history, "keyframes": keyframes, "injections": injection_lines, "zoom": use_motion_zoom,
        "metadata_caption": use_metadata_caption,
        "progress": comfy_progress,
        "max_steps": comfy_max_steps
    })
@app.route("/add_keyframe", methods=["POST"])
def add_keyframe():
    d = request.json; f_idx = str(d.get("frame", 0))
    keyframes[f_idx] = {"prompt": d.get("prompt", "") or current_prompt, "denoise": float(d.get("denoise", 0.5)), "seed_offset": int(d.get("seed_offset", 0))}
    save_state(); return jsonify({"status": "ok", "keyframes": keyframes})
@app.route("/latest_frame")
def latest():
    files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")], key=get_frame_num)
    if not files: return "none", 404
    return send_file(os.path.join(OUTPUT_DIR, files[-1]), max_age=0)
@app.route("/inject", methods=["POST"])
def inject():
    t = request.json.get("text", "").strip(); 
    if t: injection_lines.append(t); save_state()
    return jsonify({"status": "ok"})

@app.route("/set_caption", methods=["POST"])
def set_caption():
    global active_caption, caption_remaining
    global temp_caption_font_size, temp_caption_x, temp_caption_y
    global temp_caption_bg_r, temp_caption_bg_g, temp_caption_bg_b, temp_caption_bg_a
    d = request.json
    t = d.get("text", "").strip()
    if t:
        with state_lock:
            active_caption = t
            caption_remaining = 5
            temp_caption_font_size = int(d.get("font_size", temp_caption_font_size))
            temp_caption_x = int(d.get("x", temp_caption_x))
            temp_caption_y = int(d.get("y", temp_caption_y))
            temp_caption_bg_r = int(d.get("bg_r", temp_caption_bg_r))
            temp_caption_bg_g = int(d.get("bg_g", temp_caption_bg_g))
            temp_caption_bg_b = int(d.get("bg_b", temp_caption_bg_b))
            temp_caption_bg_a = float(d.get("bg_a", temp_caption_bg_a))
        save_state()
        logit(f"Caption set: {active_caption} (Remaining: 5)")
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
    .thumb-strip img { width: 19%; height: auto; border: 2px solid #333; border-radius: 4px; opacity: 0.6; }
    .tag { background: blue; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin: 2px; display: inline-block; }
    .kf-item { background: #1c1c21; padding: 5px; margin-top: 5px; border-radius: 4px; border-left: 3px solid #8b5cf6; font-size: 10px; text-align: left;}
</style></head>
<body>
    <div class="column left">
        <h3>FlaskArchitect's EpochStreamer Engine Config</h3>
        <label>Base Prompt</label><textarea id="prompt" rows="3">cartoon illustration, whimsical storybook art, Dr Seuss inspired fantasy world, Flutgoose walking along a winding path toward a waterfall, colorful Seuss-like creatures everywhere, exaggerated curved trees, impossible architecture, playful fantasy park, bold black ink outlines, hand drawn children's book illustration, bright flat colors, whimsical characters, funny expressions, comic style, pen and ink drawing, detailed linework, classic illustrated storybook page, imaginative fantasy landscape, vibrant cartoon world</textarea>
        <label>Negative Prompt</label><textarea id="neg_prompt" rows="2">photorealistic, realistic, concept art, matte painting, 3d render, cinematic lighting, volumetric lighting, oil painting, digital painting, hyper detailed textures, realism, photograph</textarea>
        <label>Model</label><select id="model">{% for m in MODELS %}<option>{{m}}</option>{% endfor %}</select>
        <!-- LoRA 1 & Strength -->
        <div style="display:flex; gap:10px;">
            <div style="flex:2;">
                <label>LoRA 1</label>
                <select id="lora1">
                    {% for l in LORAS %}
                    <option {% if l == lora1_name %}selected{% endif %}>{{l}}</option>
                    {% endfor %}
                </select>
            </div>
            <div style="flex:1;">
                <label>Strength 1</label>
                <input type="number" id="lora1_strength" value="{{lora1_strength}}" step="0.1" min="0.0" max="2.0">
            </div>
        </div>
        <!-- LoRA 2 & Strength -->
        <div style="display:flex; gap:10px; margin-top:5px;">
            <div style="flex:2;">
                <label>LoRA 2</label>
                <select id="lora2">
                    {% for l in LORAS %}
                    <option {% if l == lora2_name %}selected{% endif %}>{{l}}</option>
                    {% endfor %}
                </select>
            </div>
            <div style="flex:1;">
                <label>Strength 2</label>
                <input type="number" id="lora2_strength" value="{{lora2_strength}}" step="0.1" min="0.0" max="2.0">
            </div>
        </div>
        
        <!-- Row 1 -->
        <div style="display:flex; gap:5px;">
            <div style="flex:1;">
                <label>Seed</label>
                <input type="number" id="seed" value="123456">
            </div>
            <div style="flex:1;">
                <label>Steps</label>
                <input type="number" id="steps" value="12">
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
                <input type="number" id="frames" value="1500">
            </div>
        </div>
<button class="btn-green" onclick="window.open('/media', '_blank')">
    MAKE VIDEOS
</button>
        <hr style="border:0; border-top:1px solid #333; margin:20px 0;">
        <h3>Motion Zoom</h3>
        <label><input type="checkbox" id="use_zoom" checked style="width:auto;"> ENABLE ZOOM</label>
        <label><input type="checkbox" id="use_caption" style="width:auto;"> SHOW METADATA CAPTION</label>
        <!-- Metadata Caption Style Customization -->
        <div style="background: #1c1c21; padding: 8px; border-radius: 4px; margin-top: 5px; border: 1px solid #333;">
            <div style="display:flex; gap:5px;">
                <div style="flex:1;">
                    <label>Font Size</label>
                    <input type="number" id="cap_font_size" value="{{caption_font_size}}">
                </div>
                <div style="flex:1;">
                    <label>Loc X (px)</label>
                    <input type="number" id="cap_x" value="{{caption_x}}">
                </div>
                <div style="flex:1;">
                    <label>Loc Y (px)</label>
                    <input type="number" id="cap_y" value="{{caption_y}}">
                </div>
            </div>
            <div style="display:flex; gap:5px; margin-top:5px; align-items:center;">
                <div style="flex:3;">
                    <label>BG Color (R, G, B)</label>
                    <div style="display:flex; gap:2px;">
                        <input type="number" id="cap_bg_r" value="{{caption_bg_r}}" min="0" max="255" style="padding: 4px; margin-right: 0;">
                        <input type="number" id="cap_bg_g" value="{{caption_bg_g}}" min="0" max="255" style="padding: 4px; margin-right: 0;">
                        <input type="number" id="cap_bg_b" value="{{caption_bg_b}}" min="0" max="255" style="padding: 4px; margin-right: 0;">
                    </div>
                </div>
                <div style="flex:1;">
                    <label>Opacity</label>
                    <input type="number" id="cap_bg_a" value="{{caption_bg_a}}" step="0.1" min="0" max="1" style="padding: 4px;">
                </div>
            </div>
        </div>
        <label>Zoom Start/End</label><div style="display:flex; gap:5px;"><input type="number" id="zs" value="1.0" step="0.01"><input type="number" id="ze" value="1.05" step="0.01"></div>
        <label>Yaw S/E</label><div style="display:flex; gap:5px;"><input type="number" id="pxs" value="0.5" step="0.01"><input type="number" id="pxe" value="0.5" step="0.01"></div>
        <label>Pitch S/E</label><div style="display:flex; gap:5px;"><input type="number" id="pys" value="0.5" step="0.01"><input type="number" id="pye" value="0.5" step="0.01"></div>
        <label>Ship Roll</label>
        <select id="roll_mode">
            <option value="none">Level (Stop)</option>
            <option value="right">Roll Right</option>
            <option value="left">Roll Left</option>
        </select>
        <div style="margin: 10px 0; display: flex; align-items: center; gap: 8px;">
            <input type="checkbox" id="pause_ui_sync" style="width: auto; margin: 0; cursor: pointer;">
            <label for="pause_ui_sync" style="margin: 0; cursor: pointer; font-weight: bold; color: #f59e0b; font-size: 11px;">Pause Parameter Syncing (Manual edit mode)</label>
        </div>
        <button class="btn-blue" onclick="update(this)">UPDATE ENGINE</button>
    </div>
    <div class="column center">
        <h2 id="status_text">IDLE</h2>
        <div id="injections"></div>
        <img id="preview" src="">
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
        <!-- Temporary Caption Style Customization -->
        <div style="background: #1c1c21; padding: 8px; border-radius: 4px; margin-top: 5px; border: 1px solid #333; margin-bottom: 5px;">
            <div style="display:flex; gap:5px;">
                <div style="flex:1;">
                    <label>Font Size</label>
                    <input type="number" id="temp_cap_font_size" value="{{temp_caption_font_size}}">
                </div>
                <div style="flex:1;">
                    <label>Loc X (px)</label>
                    <input type="number" id="temp_cap_x" value="{{temp_caption_x}}">
                </div>
                <div style="flex:1;">
                    <label>Loc Y (px)</label>
                    <input type="number" id="temp_cap_y" value="{{temp_caption_y}}">
                </div>
            </div>
            <div style="display:flex; gap:5px; margin-top:5px; align-items:center;">
                <div style="flex:3;">
                    <label>BG Color (R, G, B)</label>
                    <div style="display:flex; gap:2px;">
                        <input type="number" id="temp_cap_bg_r" value="{{temp_caption_bg_r}}" min="0" max="255" style="padding: 4px; margin-right: 0;">
                        <input type="number" id="temp_cap_bg_g" value="{{temp_caption_bg_g}}" min="0" max="255" style="padding: 4px; margin-right: 0;">
                        <input type="number" id="temp_cap_bg_b" value="{{temp_caption_bg_b}}" min="0" max="255" style="padding: 4px; margin-right: 0;">
                    </div>
                </div>
                <div style="flex:1;">
                    <label>Opacity</label>
                    <input type="number" id="temp_cap_bg_a" value="{{temp_caption_bg_a}}" step="0.1" min="0" max="1" style="padding: 4px;">
                </div>
            </div>
        </div>
        <button class="btn-blue" onclick="setCaption(this)">INSERT CAPTION (5 FRAMES)</button>
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
                            caption_font_size:document.getElementById('cap_font_size').value,
                            caption_x:document.getElementById('cap_x').value,
                            caption_y:document.getElementById('cap_y').value,
                            caption_bg_r:document.getElementById('cap_bg_r').value,
                            caption_bg_g:document.getElementById('cap_bg_g').value,
                            caption_bg_b:document.getElementById('cap_bg_b').value,
                            caption_bg_a:document.getElementById('cap_bg_a').value,
                            lora1:document.getElementById('lora1').value,
                            lora1_strength:document.getElementById('lora1_strength').value,
                            lora2:document.getElementById('lora2').value,
                            lora2_strength:document.getElementById('lora2_strength').value,
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
                            roll_mode:document.getElementById('roll_mode').value
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
                    font_size:document.getElementById('temp_cap_font_size').value,
                    x:document.getElementById('temp_cap_x').value,
                    y:document.getElementById('temp_cap_y').value,
                    bg_r:document.getElementById('temp_cap_bg_r').value,
                    bg_g:document.getElementById('temp_cap_bg_g').value,
                    bg_b:document.getElementById('temp_cap_bg_b').value,
                    bg_a:document.getElementById('temp_cap_bg_a').value
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
                    caption_font_size:document.getElementById('cap_font_size').value,
                    caption_x:document.getElementById('cap_x').value,
                    caption_y:document.getElementById('cap_y').value,
                    caption_bg_r:document.getElementById('cap_bg_r').value,
                    caption_bg_g:document.getElementById('cap_bg_g').value,
                    caption_bg_b:document.getElementById('cap_bg_b').value,
                    caption_bg_a:document.getElementById('cap_bg_a').value,
                    lora1:document.getElementById('lora1').value,
                    lora1_strength:document.getElementById('lora1_strength').value,
                    lora2:document.getElementById('lora2').value,
                    lora2_strength:document.getElementById('lora2_strength').value,
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
                    roll_mode:document.getElementById('roll_mode').value
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
                if(d.history) document.getElementById('thumb_strip').innerHTML = d.history.map(i=>`<img src="/static/streamer512/${i}?t=${Date.now()}">`).join('');
                if(d.injections) document.getElementById('injections').innerHTML = d.injections.map(i=>`<span class="tag">${i}</span>`).join('');
                
                if (!pauseSync || !initialSyncDone) {
                    // Sync caption checkbox
                    if (d.metadata_caption !== undefined) document.getElementById('use_caption').checked = d.metadata_caption;
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
SHARED_PATH = os.path.join(BASE_DIR, "static", "streamer512")
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
<video controls src="{{ url_for('static', filename='streamer512/' + v) }}"></video><br>
<input type="radio" name="video" value="{{v}}">
{{v}}
</div>
{% endfor %}
<h2>Images</h2>
<button type="button" onclick="selectAllImages()">Select All Images</button>
{% for img in images %}
<div class="box">
<img src="{{ url_for('static', filename='streamer512/' + img) }}"><br>
<input type="checkbox" name="images" value="{{ img }}">
</div>
{% endfor %}
<h2>Audio</h2>
{% for mp3 in mp3s %}
<div class="box">
<audio controls style="width:250px;">
    <source src="{{ url_for('static', filename='streamer512/' + mp3) }}" type="audio/mpeg">
</audio><br>
<input type="radio" name="audio" value="{{ mp3 }}">
{{ mp3 }}
</div>
{% endfor %}
<div style="margin: 20px auto; max-width: 400px; text-align: left; background: #1c1c21; padding: 15px; border-radius: 4px; border: 1px solid #333;">
    <h3 style="margin-top: 0; font-size: 14px; color: orange;">Audio Trimming (Optional)</h3>
    <label style="display:inline-block; font-size:11px; color:#888; font-weight:bold; width:130px;">Start Time (sec):</label>
    <input type="number" name="audio_start" step="0.1" min="0" placeholder="0.0" style="width:80px; display:inline-block; margin-bottom: 8px;"><br>
    <label style="display:inline-block; font-size:11px; color:#888; font-weight:bold; width:130px;">End Time (sec):</label>
    <input type="number" name="audio_end" step="0.1" min="0" placeholder="End of file" style="width:80px; display:inline-block;">
</div>
<br>
<button type="submit">PROCESS</button>
</form>
<script>
function selectAllImages() {
    const checkboxes = document.querySelectorAll('input[name="images"]');
    checkboxes.forEach(cb => cb.checked = true);
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
            return os.path.getmtime(full_path)
        except Exception as e:
            ic(f"ERROR reading mtime for {filename}:", e)
            return 0
    # --------------------------------------------------
    # FILTER FILE TYPES (excluding frame sequences)
    # --------------------------------------------------
    images = [
        f for f in files 
        if f.lower().endswith((".jpg", ".png")) 
        and not f.startswith("temp_clean_")
        and not f.startswith("clean_")
    ]
    mp3s   = [f for f in files if f.lower().endswith(".mp3") and not f.startswith("_pad_")]
    videos = [f for f in files if f.lower().endswith(".mp4")]
    # --------------------------------------------------
    # SORT BY DATE (NEWEST FIRST)
    # --------------------------------------------------
    images.sort(key=get_mtime, reverse=True)
    mp3s.sort(key=get_mtime, reverse=True)
    videos.sort(key=get_mtime, reverse=True)
    # --------------------------------------------------
    # RETURN TEMPLATE
    # --------------------------------------------------
    return render_template_string(
        HTML,
        images=images,
        mp3s=mp3s,
        videos=videos
    )

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
        return redirect("/media")
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
def pad_audio(src, out, start_sec=None, end_sec=None):
    audio = AudioSegment.from_mp3(src)
    
    # Calculate millisecond trim points
    start_ms = int(start_sec * 1000) if start_sec is not None else 0
    end_ms = int(end_sec * 1000) if end_sec is not None else len(audio)
    
    # Clamp to audio bounds
    start_ms = max(0, min(start_ms, len(audio)))
    end_ms = max(start_ms, min(end_ms, len(audio)))
    
    # Slice the audio segment
    trimmed_audio = audio[start_ms:end_ms]
    
    silence = AudioSegment.silent(duration=200)
    (silence + trimmed_audio + silence).export(out, format="mp3")
# --------------------------------------------------
# PROCESS VIDEO
# --------------------------------------------------
@app.route("/process_video", methods=["POST"])
def process_video():
    selected_video = request.form.get("video")
    selected_audio = request.form.get("audio")
    selected_images = request.form.getlist("images")
    audio_start = request.form.get("audio_start")
    audio_end = request.form.get("audio_end")
    
    start_sec = float(audio_start) if audio_start and audio_start.strip() else None
    end_sec = float(audio_end) if audio_end and audio_end.strip() else None
    
    ic(selected_video, selected_audio, selected_images, start_sec, end_sec)
    
    if not selected_audio:
        return "Please select an audio file", 400
        
    unique_run = str(uuid.uuid4())[:8]
    audio_path = os.path.join(SHARED_PATH, selected_audio)
    padded_audio = os.path.join(SHARED_PATH, f"_pad_{unique_run}.mp3")
    
    video_clip = None
    audio_clip = None
    new_video = None
    final = None
    clips = []
    
    try:
        pad_audio(audio_path, padded_audio, start_sec, end_sec)
        audio_clip = AudioFileClip(padded_audio)
        
        # ------------------------------------------
        # CASE 1: EXISTING VIDEO
        # ------------------------------------------
        if selected_video:
            video_path = os.path.join(SHARED_PATH, selected_video)
            video_clip = VideoFileClip(video_path)
            ic("Original duration:", video_clip.duration)
            ic("Audio duration:", audio_clip.duration)
            speed_factor = video_clip.duration / audio_clip.duration
            ic("Speed factor:", speed_factor)
            new_video = video_clip.fx(vfx.speedx, speed_factor)
            final = new_video.set_audio(audio_clip)
        # ------------------------------------------
        # CASE 2: IMAGE SLIDESHOW
        # ------------------------------------------
        else:
            if not selected_images:
                return "Please select a video OR some images", 400
                
            # Sort selected images by creation date (oldest first)
            selected_images.sort(key=lambda x: os.path.getmtime(os.path.join(SHARED_PATH, x)))
            
            duration = audio_clip.duration / len(selected_images)
            for img in selected_images:
                p = os.path.join(SHARED_PATH, img)
                clip = ImageClip(p).set_duration(duration)
                clips.append(clip)
            final = concatenate_videoclips(clips).set_audio(audio_clip)
        
        # ------------------------------------------
        # WRITE OUTPUT
        # ------------------------------------------
        out = os.path.join(SHARED_PATH, f"output_{unique_run}.mp4")
        ic("Writing:", out)
        final.write_videofile(
            out,
            fps=24,
            codec="libx264",
            audio_codec="aac"
        )
        return redirect(url_for("serve_video", filename=os.path.basename(out)))
    except Exception as e:
        ic("Process video error:", e)
        return f"Error processing video: {str(e)}", 500
    finally:
        # Ensure all MoviePy resources are closed
        if final:
            try: final.close()
            except: pass
        if new_video:
            try: new_video.close()
            except: pass
        if video_clip:
            try: video_clip.close()
            except: pass
        if audio_clip:
            try: audio_clip.close()
            except: pass
        for clip in clips:
            try: clip.close()
            except: pass
        # Clean up temporary padded audio file
        if os.path.exists(padded_audio):
            try: os.remove(padded_audio)
            except: pass
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
        text = request.form.get("text", "")
        voice = request.form.get("voice")
        # Sanitize text to form a safe filename
        safe_text = secure_filename(text[:20].strip())
        if not safe_text:
            safe_text = "audio"
        out = os.path.join(SHARED_PATH, safe_text.replace(" ", "_") + ".mp3")
        payload = {
            "model": "kokoro",
            "voice": voice,
            "input": text
        }
        try:
            r = requests.post(
                "http://localhost:8880/v1/audio/speech",
                json=payload,
                timeout=500
            )
            r.raise_for_status()
            with open(out, "wb") as f:
                f.write(r.content)
        except Exception as e:
            ic("TTS Generation error:", e)
            return f"Error generating TTS: {str(e)}", 500
        return redirect("/media")
    return """
    <a style= "font-size:2vw;color:red;" href="/">HOME</a>
    <form method="post">
    <textarea name="text" style="width:60%;height:300px;"></textarea><br>
    <select name="voice">
        <option>af_bella</option>
        <option>am_adam</option>
    </select><br>
    <button>Generate</button>
    </form>
    """

if __name__ == "__main__":
    load_state()
    refresh_models_and_loras()
    app.run(host="0.0.0.0", port=5101, debug=False, use_reloader=False)
