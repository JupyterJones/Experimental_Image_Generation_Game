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
DEFAULT_WIDTH = 340
DEFAULT_HEIGHT =512

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Ensure static/darkrooms exists
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "darkrooms")
STATE_FILE = os.path.join(OUTPUT_DIR, "darkrooms.json")
LOG_FILE_PATH = os.path.join(OUTPUT_DIR, "darkrooms.txt")

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
lora1_strength = 0.8
lora2_strength = 0.8
lora3_strength = 0.8

use_visual_director = False
visual_director_interval = 25
visual_director_model = "moondream:latest"

use_prompt_interpolation = False
use_video_interpolation = False

denoise_current = 0.35
teleport_image = None
active_caption = ""
caption_remaining = 0

# Caption custom style variables
caption_font_size = 12
caption_x = 10
caption_y = 10
caption_bg_r = 61
caption_bg_g = 81
caption_bg_b = 92
caption_bg_a = 0.4

temp_caption_font_size = 20
temp_caption_x = 20
temp_caption_y = 20
temp_caption_bg_r = 0
temp_caption_bg_g = 0
temp_caption_bg_b = 0
temp_caption_bg_a = 0.5
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
current_prompt = "You are an atmospheric horror narrator. A traveler is trapped and wandering lost in the Backrooms. Write a first-person,  psychological horror diary entry that progresses frame-by-frame, directly  matching this sequence of descriptions. Keep the tone tense, whispering, and dread-filled. abandoned, empty"
original_starting_prompt = current_prompt

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
            ws = websocket.create_connection(ws_url, timeout=2000)
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
negative_prompt = "low quality, blurry, nudity, breasts, NSWF, people, man, woman"

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
        if use_video_interpolation:
            cmd = [
                "ffmpeg", "-y", "-framerate", "5", 
                "-i", os.path.join(OUTPUT_DIR, "frame_%03d.png"),
                "-vf", "minterpolate=fps=24:mi_mode=mci:mc_me=epzs:me_mode=bidir",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", 
                os.path.join(OUTPUT_DIR, output_filename)
            ]
        else:
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

    # 1. Apply fullscreen overlay if requested
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

    return img.convert("RGB"), (curr_zoom, curr_pan_x, curr_pan_y)

def apply_logo_to_image(img, filename, lx, ly, lw, lh, l_opacity):
    if filename and filename != "None":
        logo_path = os.path.join(BASE_DIR, "static", "overlays", filename)
        if os.path.exists(logo_path):
            try:
                logo_img = Image.open(logo_path).convert("RGBA")
                if lw > 0 and lh > 0:
                    logo_img = logo_img.resize((lw, lh), Image.LANCZOS)
                
                if l_opacity < 1.0:
                    alpha = logo_img.getchannel("A")
                    alpha = alpha.point(lambda p: int(p * l_opacity))
                    logo_img.putalpha(alpha)
                
                w, h = img.size
                logo_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                logo_layer.paste(logo_img, (lx, ly), logo_img)
                orig_mode = img.mode
                img = Image.alpha_composite(img.convert("RGBA"), logo_layer)
                if orig_mode != "RGBA":
                    img = img.convert(orig_mode)
            except Exception as le:
                logit(f"Custom logo composition error: {le}")
    return img

def apply_custom_logo(img):
    global logo_filename, logo_x, logo_y, logo_w, logo_h, logo_opacity
    return apply_logo_to_image(img, logo_filename, logo_x, logo_y, logo_w, logo_h, logo_opacity)

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
        
        try:
            font = ImageFont.load_default(size=caption_font_size)
        except:
            font = ImageFont.load_default()
        
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
def apply_border(img, border_path=None):
    if border_path is None:
        border_path = random.choice([
            "static/border_dirty.png",
            "static/border_dirty1.png",
            "static/border_dirty2.png",
            "static/border_dirty3.png"
        ])
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
    Draws text with customized font name, size, position, and background color.
    """
    if not text:
        return img
    try:
        from PIL import ImageDraw, ImageFont
        # Create an RGBA version of the image to support transparency in drawing
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
        # Load font if not Default
        font = None
        if font_name and font_name != "Default":
            font_path = os.path.join(BASE_DIR, "fonts", font_name)
            if os.path.exists(font_path):
                try:
                    font = ImageFont.truetype(font_path, temp_caption_font_size)
                except Exception as fe:
                    logit(f"Failed to load font {font_path}: {fe}")
        
        if font is None:
            try:
                font = ImageFont.load_default(size=temp_caption_font_size)
            except:
                font = ImageFont.load_default()
        
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
            "original_starting_prompt": original_starting_prompt,
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
            "feedback_sharpness_boost": feedback_sharpness_boost,
            "lora1_strength": lora1_strength,
            "lora2_strength": lora2_strength,
            "lora3_strength": lora3_strength,
            "use_visual_director": use_visual_director,
            "visual_director_interval": visual_director_interval,
            "visual_director_model": visual_director_model,
            "use_prompt_interpolation": use_prompt_interpolation,
            "use_video_interpolation": use_video_interpolation,
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
    global use_motion_zoom, zoom_start, zoom_end, pan_start_x, pan_end_x, pan_start_y, pan_end_y
    global default_steps, default_cfg, use_metadata_caption
    global logo_filename, logo_x, logo_y, logo_w, logo_h, logo_opacity
    global caption_font_size, caption_x, caption_y, caption_bg_r, caption_bg_g, caption_bg_b, caption_bg_a
    global temp_caption_font_size, temp_caption_x, temp_caption_y, temp_caption_bg_r, temp_caption_bg_g, temp_caption_bg_b, temp_caption_bg_a
    global original_starting_prompt
    global feedback_color_boost, feedback_contrast_boost, feedback_sharpness_boost
    global lora1_strength, lora2_strength, lora3_strength
    global use_visual_director, visual_director_interval, visual_director_model
    global use_prompt_interpolation, use_video_interpolation, roll_mode

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
            original_starting_prompt = state.get("original_starting_prompt", current_prompt)
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
            lora1_strength = float(state.get("lora1_strength", 0.8))
            lora2_strength = float(state.get("lora2_strength", 0.8))
            lora3_strength = float(state.get("lora3_strength", 0.8))
            use_visual_director = bool(state.get("use_visual_director", False))
            visual_director_interval = int(state.get("visual_director_interval", 25))
            visual_director_model = state.get("visual_director_model", "moondream")
            use_prompt_interpolation = bool(state.get("use_prompt_interpolation", False))
            use_video_interpolation = bool(state.get("use_video_interpolation", False))
            zoom_end = state.get("z_e", 1.1)
            pan_start_x = state.get("px_s", 0.5)
            pan_end_x = state.get("px_e", 0.5)
            pan_start_y = state.get("py_s", 0.5)
            pan_end_y = state.get("py_e", 0.5)
            roll_mode = state.get("roll_mode", "none")
            caption_font_size = int(state.get("caption_font_size", 12))
            caption_x = int(state.get("caption_x", 10))
            caption_y = int(state.get("caption_y", 10))
            caption_bg_r = int(state.get("caption_bg_r", 61))
            caption_bg_g = int(state.get("caption_bg_g", 81))
            caption_bg_b = int(state.get("caption_bg_b", 92))
            caption_bg_a = float(state.get("caption_bg_a", 0.4))
            temp_caption_font_size = int(state.get("temp_caption_font_size", 20))
            temp_caption_x = int(state.get("temp_caption_x", 20))
            temp_caption_y = int(state.get("temp_caption_y", 20))
            temp_caption_bg_r = int(state.get("temp_caption_bg_r", 0))
            temp_caption_bg_g = int(state.get("temp_caption_bg_g", 0))
            temp_caption_bg_b = int(state.get("temp_caption_bg_b", 0))
            temp_caption_bg_a = float(state.get("temp_caption_bg_a", 0.5))

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
    lora_names = [lora1_name, lora2_name, lora3_name]
    lora_strengths = [lora1_strength, lora2_strength, lora3_strength]
    for i, ln in enumerate(lora_names):
        if ln and ln != "None":
            nid = f"lora_{i}"
            str_val = lora_strengths[i]
            wf[nid] = {"inputs": {"lora_name": ln, "strength_model": str_val, "strength_clip": str_val, "model": lm, "clip": lc}, "class_type": "LoraLoader"}
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

def parse_prompt(prompt_text):
    import re
    loras = re.findall(r"<lora:[^>]+>", prompt_text)
    clean_prompt = re.sub(r"<lora:[^>]+>", "", prompt_text).strip()
    clean_prompt = re.sub(r"\s+", " ", clean_prompt)
    clean_prompt = clean_prompt.strip(", ")
    return clean_prompt, loras

def clean_desc(desc_text):
    if not desc_text:
        return ""
    desc_text = desc_text.strip()
    
    # Strip quotes
    while True:
        stripped = False
        for q in ['"', "'", '`']:
            if desc_text.startswith(q) and desc_text.endswith(q) and len(desc_text) > 1:
                desc_text = desc_text[1:-1].strip()
                stripped = True
        if not stripped:
            break
            
    # Strip trailing period
    if desc_text.endswith('.'):
        desc_text = desc_text[:-1].strip()
        
    # Strip common visual model prefixes
    lower_desc = desc_text.lower()
    prefixes = [
        "i see a ", "i see ", "this is an image of ", "this is a picture of ", 
        "this is a ", "this is ", "the image shows ", "the image depicts ", 
        "the picture shows ", "there is a ", "there are ", "shows a ", "depicts a "
    ]
    for prefix in prefixes:
        if lower_desc.startswith(prefix):
            desc_text = desc_text[len(prefix):].strip()
            if desc_text:
                desc_text = desc_text[0].upper() + desc_text[1:]
            break
            
    return desc_text

def query_llava(image_path, system_instruction):
    """
    Queries local Ollama LlaVa/Moondream model with base64 encoded image.
    """
    import gc
    gc.collect()  # Force Python to release unused memory
    import base64
    try:
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        
        payload = {
            "model": "LlaVa:latest",
            "prompt": system_instruction,
            "images": [img_data],
            "stream": False,
            "options": {
                "temperature": 0.5,
                "max_tokens": 120
            }
        }
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=1000)
        if r.status_code == 200:
            return r.json().get("response", "").strip()
        else:
            logit(f"Ollama query returned status code {r.status_code}: {r.text}")
    except Exception as e:
        logit(f"LlaVa query failed: {e}")
    return None

def get_interpolated_prompt(frame_idx, base_prompt):
    """
    Finds the surrounding keyframes and returns a weighted blend of their prompts.
    """
    if not keyframes:
        return base_prompt
        
    kf_nums = sorted([int(k) for k in keyframes.keys()])
    if not kf_nums:
        return base_prompt
        
    if frame_idx in kf_nums:
        return keyframes[str(frame_idx)].get("prompt", base_prompt)
        
    prev_kf = None
    next_kf = None
    for k in kf_nums:
        if k < frame_idx:
            prev_kf = k
        elif k > frame_idx and next_kf is None:
            next_kf = k
            break
            
    if prev_kf is None and next_kf is None:
        return base_prompt
    elif prev_kf is None:
        return keyframes[str(next_kf)].get("prompt", base_prompt)
    elif next_kf is None:
        return keyframes[str(prev_kf)].get("prompt", base_prompt)
        
    p_prev = keyframes[str(prev_kf)].get("prompt", base_prompt)
    p_next = keyframes[str(next_kf)].get("prompt", base_prompt)
    
    if p_prev == p_next:
        return p_prev
        
    total_dist = next_kf - prev_kf
    weight_next = (frame_idx - prev_kf) / total_dist
    weight_prev = 1.0 - weight_next
    
    p_prev_clean = p_prev.replace("(", "").replace(")", "")
    p_next_clean = p_next.replace("(", "").replace(")", "")
    
    return f"({p_prev_clean}:{weight_prev:.2f}), ({p_next_clean}:{weight_next:.2f})"

def get_interpolated_denoise(frame_idx, default_d):
    """
    Finds the surrounding keyframes and returns a weighted blend of their denoise values.
    """
    if not keyframes:
        return default_d
        
    kf_nums = sorted([int(k) for k in keyframes.keys()])
    if not kf_nums:
        return default_d
        
    if frame_idx in kf_nums:
        return float(keyframes[str(frame_idx)].get("denoise", default_d))
        
    prev_kf = None
    next_kf = None
    for k in kf_nums:
        if k < frame_idx:
            prev_kf = k
        elif k > frame_idx and next_kf is None:
            next_kf = k
            break
            
    if prev_kf is None and next_kf is None:
        return default_d
    elif prev_kf is None:
        return float(keyframes[str(next_kf)].get("denoise", default_d))
    elif next_kf is None:
        return float(keyframes[str(prev_kf)].get("denoise", default_d))
        
    d_prev = float(keyframes[str(prev_kf)].get("denoise", default_d))
    d_next = float(keyframes[str(next_kf)].get("denoise", default_d))
    
    total_dist = next_kf - prev_kf
    weight_next = (frame_idx - prev_kf) / total_dist
    weight_prev = 1.0 - weight_next
    
    return d_prev * weight_prev + d_next * weight_next


def render_video(resume=False):
    global running, current_frame, paused, current_seed, teleport_image
    global caption_remaining, active_caption, roll_mode, current_prompt, original_starting_prompt
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
        original_starting_prompt = current_prompt
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
            
            if use_prompt_interpolation:
                prompt_base = get_interpolated_prompt(current_frame, current_prompt)
                active_d = get_interpolated_denoise(current_frame, denoise_current)
            else:
                prompt_base = current_prompt
                active_d = denoise_current
                kf_nums = sorted([int(k) for k in keyframes.keys()])
                last_kf = None
                for k in kf_nums:
                    if k <= current_frame:
                        last_kf = k
                if last_kf is not None:
                    active_d = float(keyframes[str(last_kf)].get("denoise", denoise_current))
                
            prompt = prompt_base + (", " + ", ".join(injection_lines[-MAX_LINES:]) if injection_lines else "")
            
            # Calculate active params (for keyframe support)
            active_p, active_s = prompt, seed
            kf = keyframes.get(str(current_frame))
            if kf:
                kf_prompt = kf.get("prompt", active_p)
                active_p = kf_prompt
                active_d = float(kf.get("denoise", active_d))
                active_s = seed + int(kf.get("seed_offset", 0))
                with state_lock:
                    current_prompt = kf_prompt
                    original_starting_prompt = kf_prompt
                logit(f"Keyframe {current_frame} applied. Redirecting Visual Director baseline to: '{kf_prompt}'")

            wf = get_workflow(active_s, active_p, negative_prompt, last_server_filename, current_frame, active_d)
            
            try:
                resp = requests.post(f"{COMFY_URL}/prompt", json={"prompt": wf, "client_id": CLIENT_ID}, timeout=2000).json()
                pid = resp["prompt_id"]
                logit(f"Prompt sent (Frame {current_frame}). PID: {pid}. Prompt: '{active_p}'")
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
                
                # Apply the custom logo to the feedback image so it is burned into the image2image process
                feedback_img = apply_custom_logo(feedback_img)
                
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

                # Save the clean base frame for dynamic logo overlays
                latest_base_path = os.path.join(OUTPUT_DIR, "latest_base.png")
                local_img.copy().convert("RGBA").save(latest_base_path)
                
                # Also save a per-frame clean base to support robust local watermarking
                per_frame_base = os.path.join(OUTPUT_DIR, f"clean_base_{current_frame:03d}.png")
                local_img.copy().convert("RGBA").save(per_frame_base)

                # Apply the custom logo
                local_img = apply_custom_logo(local_img)

                local_path = os.path.join(OUTPUT_DIR, f"frame_{current_frame:03d}.png")
                local_img.save(local_path)
                
                # Visual Director prompt evolution
                if use_visual_director and (current_frame > 0) and (current_frame % visual_director_interval == 0):
                    logit(f"Visual Director: Evolving prompt based on frame {current_frame}...")
                    system_instruction = "Describe what you see in this image in one brief sentence."
                    img_desc = query_llava(clean_path, system_instruction)
                    if img_desc:
                        cleaned_desc = clean_desc(img_desc)
                        logit(f"Visual Director: Image description -> '{cleaned_desc}'")
                        
                        # Combine starting theme and latest description
                        clean_start, start_loras = parse_prompt(original_starting_prompt)
                        if cleaned_desc:
                            new_prompt_core = f"{clean_start}, {cleaned_desc}"
                        else:
                            new_prompt_core = clean_start
                            
                        # Re-append unique LoRAs
                        all_loras = list(dict.fromkeys(start_loras))
                        loras_str = " ".join(all_loras)
                        new_prompt = f"{new_prompt_core} {loras_str}".strip()
                        
                        with state_lock:
                            current_prompt = new_prompt
                        logit(f"Visual Director: New base prompt set -> '{current_prompt}'")
                    else:
                        logit("Visual Director: Vision model returned no description, keeping previous prompt.")
                
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
        feedback_sharpness_boost=feedback_sharpness_boost,
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
        temp_caption_bg_b=temp_caption_bg_b,
        temp_caption_bg_a=temp_caption_bg_a
    )

# --------------------------------------------------
# JSON KEYFRAME BUILDER ENDPOINTS
# --------------------------------------------------
@app.route("/json_builder")
def json_builder():
    fonts = get_available_fonts()
    logos = get_available_logos()
    return render_template_string(
        HTML_JSON_BUILDER,
        MODELS=MODELS,
        LORAS=LORAS,
        FONTS=fonts,
        LOGOS=logos
    )

@app.route("/get_config")
def get_config():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return jsonify(json.load(f))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # Fallback to current memory state if file not present
    save_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return jsonify(json.load(f))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Config file not found"}), 404

@app.route("/save_config", methods=["POST"])
def save_config():
    try:
        new_state = request.json
        if not new_state:
            return jsonify({"error": "No data provided"}), 400
        
        with open(STATE_FILE, "w") as f:
            json.dump(new_state, f, indent=2)
            
        load_state()
        logit("Configuration updated via JSON Builder.")
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate_keyframes_ollama", methods=["POST"])
def generate_keyframes_ollama():
    try:
        d = request.json
        outline = d.get("outline", "").strip()
        model = d.get("model", "llama3.2").strip()
        model = resolve_model_name(model)
        instructions = d.get("instructions", "").strip()
        
        if not outline:
            return jsonify({"error": "Outline is empty"}), 400
            
        system_prompt = (
            "You are a Stable Diffusion keyframe generator. Your goal is to generate structured keyframes for an AI video animation based on a rough user outline. "
            "For each event in the outline, determine a frame number (e.g. 0, 50, 100) and create a detailed visual prompt (30-50 words) that describes the scene, focusing on visual details, lighting, atmosphere, and textures. "
            "Also choose a denoise value (between 0.3 and 0.85, depending on how much change there is from the previous scene; higher denoise for dramatic changes) and a seed_offset (typically between -10 and 10).\\n\\n"
            "Format your output strictly as a JSON object inside a single markdown code block (using ```json and ```). No conversation, explanations, or filler. The JSON structure must be:\\n"
            "{\\n"
            "  \\\"keyframes\\\": {\\n"
            "    \\\"0\\\": {\\n"
            "      \\\"prompt\\\": \\\"highly detailed visual description...\\\",\\n"
            "      \\\"denoise\\\": 0.35,\\n"
            "      \\\"seed_offset\\\": 0\\n"
            "    },\\n"
            "    \\\"50\\\": {\\n"
            "      \\\"prompt\\\": \\\"highly detailed visual description...\\\",\\n"
            "      \\\"denoise\\\": 0.55,\\n"
            "      \\\"seed_offset\\\": 3\\n"
            "    }\\n"
            "  }\\n"
            "}"
        )
        
        prompt = f"{system_prompt}\\n\\nOutline:\\n{outline}\\n\\nAdditional Instructions:\\n{instructions}\\n\\nJSON output:"
        
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.7,
                "max_tokens": 1500
            }
        }
        
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=2000)
        if r.status_code != 200:
            return jsonify({"error": f"Ollama error: status code {r.status_code}"}), 500
            
        response_text = r.json().get("response", "").strip()
        
        try:
            parsed_json = json.loads(response_text)
        except Exception:
            # Fallback to regex-based extraction if there's any surrounding text
            import re
            json_match = re.search(r'(\{.*?\})', response_text, re.DOTALL)
            if json_match:
                parsed_json = json.loads(json_match.group(1))
            else:
                raise ValueError(f"Could not find valid JSON in response: {response_text}")
                
        if "keyframes" not in parsed_json:
            parsed_json = {"keyframes": parsed_json}
            
        return jsonify({"status": "ok", "keyframes": parsed_json.get("keyframes", {})})
    except Exception as e:
        return jsonify({"error": f"Failed to generate keyframes: {str(e)}"}), 500

@app.route("/generate_story", methods=["POST"])
def generate_story():
    try:
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=1000)
            available = [m["name"] for m in r.json().get("models", [])] if r.status_code == 200 else []
        except:
            available = []
            
        vision_model = "moondream:latest"
        if vision_model not in available:
            v_candidates = [m for m in available if "llava" in m or "moondream" in m]
            if v_candidates: vision_model = v_candidates[0]
            
        text_model = "llama3.2:3b"
        if text_model not in available:
            preferred_text = ["llama3.2:3b", "deepseek-r1:1.5b", "dolphin3:8b", "llama2-uncensored:7b"]
            for p in preferred_text:
                if p in available:
                    text_model = p
                    break
            else:
                t_candidates = [m for m in available if any(x in m.lower() for x in ["llama", "dolphin", "deepseek", "qwen", "mistral"])]
                if t_candidates: text_model = t_candidates[0]
            
        files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("clean_base_") and f.endswith(".png")])
        if not files:
            files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")])
            
        if not files:
            return jsonify({"error": "No generated frames found in static/darkrooms/ to process."}), 400
            
        cache_path = os.path.join(OUTPUT_DIR, "descriptions_cache.json")
        cache = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    cache = json.load(f)
            except:
                pass
                
        import base64
        import re
        descriptions = []
        for idx, filename in enumerate(files):
            match = re.search(r'_(\d+)\.png$', filename)
            frame_num = int(match.group(1)) if match else idx
            file_path = os.path.join(OUTPUT_DIR, filename)
            
            if filename in cache:
                desc = cache[filename]
            else:
                try:
                    with open(file_path, "rb") as f:
                        img_data = base64.b64encode(f.read()).decode("utf-8")
                    payload = {
                        "model": vision_model,
                        "prompt": "Describe this liminal space/backrooms image in one short sentence.",
                        "images": [img_data],
                        "stream": False,
                        "options": {"temperature": 0.4, "max_tokens": 100}
                    }
                    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=6000)
                    if r.status_code == 200:
                        desc = r.json().get("response", "").strip().replace('"', '').replace("'", "")
                        cache[filename] = desc
                        with open(cache_path, "w") as f:
                            json.dump(cache, f, indent=2)
                    else:
                        desc = "An empty liminal corridor."
                except Exception as e:
                    desc = f"An empty liminal corridor. Error: {str(e)}"
            descriptions.append({"frame": frame_num, "file": filename, "description": desc})
            
        # Prepare stateful timeline combining intent (keyframes) and visual descriptions
        desc_str = ""
        max_frame = max(descriptions, key=lambda x: x["frame"])["frame"] if descriptions else 1
        for item in descriptions:
            f = item["frame"]
            
            # 1. Get interpolated prompt (Director Intent)
            intent = get_interpolated_prompt(f, original_starting_prompt)
            clean_intent, _ = parse_prompt(intent)
            
            # 2. Interpolate emotional parameters
            progress = f / max_frame if max_frame > 0 else 0.0
            tension = 1.0 + progress * 8.5
            sanity = 100.0 - progress * 90.0
            
            if tension < 3.0:
                emotion = "Calm and observant, trying to find a way out."
                inflection_hint = "Write in complete, slow, descriptive sentences."
                speed = 0.85
            elif tension < 6.0:
                emotion = "Growing paranoiac, feeling like the environment is shifting."
                inflection_hint = "Use occasional ellipses (...) to simulate hesitation, pausing, and whispering."
                speed = 1.00
            elif tension < 8.0:
                emotion = "Deep dread and anxiety. Suspects something is hunting them."
                inflection_hint = "Use frequent ellipses (...) and shorter, breathy, fragmented sentences."
                speed = 1.10
            else:
                emotion = "Extreme terror, panic, running for survival."
                inflection_hint = "Use very short, fragmented, frantic phrases, repetition, and exclamation marks!"
                speed = 1.25
                
            item["tension"] = tension
            item["sanity"] = sanity
            item["emotion"] = emotion
            item["inflection_hint"] = inflection_hint
            item["speed"] = speed
            item["intent"] = clean_intent
            
            desc_str += (
                f"Frame {f}:\n"
                f"  - Actual Visuals: {item['description']}\n"
                f"  - Director Intent / Story Beat: {clean_intent}\n"
                f"  - Emotional State: {emotion}\n"
                f"  - Speech Inflection Style: {inflection_hint}\n\n"
            )
            
        prompt = (
            "You are an expert psychological horror writer. You will write a survival diary of a traveler lost in the Backrooms.\n\n"
            "Below is a sequence of frames representing the traveler's journey. For each frame, you are given:\n"
            "1. Actual Visuals: What is physically visible in the image frame.\n"
            "2. Director Intent / Story Beat: The thematic concept, action, or progression that must happen.\n"
            "3. Emotional State: The traveler's current level of sanity and fear.\n"
            "4. Speech Inflection Style: How the text must be formatted to guide voice inflection (e.g., ellipses for pauses, exclamation marks for panic).\n\n"
            "Your task is to write a cohesive, continuous first-person story diary matching this sequence.\n"
            f"You MUST write exactly {len(descriptions)} diary entries, one for each frame. "
            "Keep each entry brief (15-30 words).\n\n"
            "Format your output strictly using '[ENTRY]' as a separator before each frame entry, like this:\n"
            "[ENTRY] Entry 0 text...\n"
            "[ENTRY] Entry 1 text...\n\n"
            "Do not output any introductory or concluding text. Output ONLY the entry texts with their [ENTRY] separators.\n\n"
            f"Timeline Sequence:\n{desc_str}"
        )
        
        payload = {
            "model": text_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.7, "max_tokens": 2048}
        }
        
        story_raw = ""
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=1200)
        if r.status_code == 200:
            story_raw = r.json().get("response", "").strip()
            
        if not story_raw:
            return jsonify({"error": "Ollama story generation failed."}), 500
            
        entries = [e.strip() for e in story_raw.split("[ENTRY]") if e.strip()]
        
        if len(entries) < len(descriptions):
            while len(entries) < len(descriptions):
                idx = len(entries)
                entries.append(f"Wandering deeper. The visual match points to: {descriptions[idx]['description']}.")
        elif len(entries) > len(descriptions):
            entries = entries[:len(descriptions)]
            
        diary_data = []
        for idx, item in enumerate(descriptions):
            item["story"] = entries[idx]
            diary_data.append(item)
            
        diary_path = os.path.join(OUTPUT_DIR, "backrooms_diary.json")
        with open(diary_path, "w") as f:
            json.dump(diary_data, f, indent=2)
            
        voice = "af_bella"
        for item in diary_data:
            frame_num = item["frame"]
            text = item["story"]
            speed_val = item.get("speed", 1.0)
            output_name = f"narration_{frame_num:03d}.mp3"
            output_path = os.path.join(OUTPUT_DIR, output_name)
            
            try:
                payload = {
                    "model": "kokoro",
                    "voice": voice,
                    "input": text,
                    "speed": speed_val
                }
                r = requests.post("http://localhost:8880/v1/audio/speech", json=payload, timeout=600)
                if r.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(r.content)
            except Exception as e:
                logit(f"TTS generation error for frame {frame_num}: {e}")
                
        return jsonify({"status": "ok", "diary": diary_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

@app.route("/save_logo_local", methods=["POST"])
def save_logo_local():
    d = request.json
    filename = d.get("logo_filename", "None")
    lx = int(d.get("x", 0))
    ly = int(d.get("y", 0))
    lw = int(d.get("w", 100))
    lh = int(d.get("h", 100))
    l_opacity = float(d.get("opacity", 1.0))

    # Find last generated frame
    files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")])
    if not files:
        return jsonify({"status": "error", "message": "No frames generated yet"}), 400

    last_frame_file = files[-1]
    last_frame_path = os.path.join(OUTPUT_DIR, last_frame_file)
    
    # Try to find the per-frame clean base first to avoid stacking logos
    frame_num_str = last_frame_file.replace("frame_", "").replace(".png", "")
    per_frame_base_file = f"clean_base_{frame_num_str}.png"
    per_frame_base_path = os.path.join(OUTPUT_DIR, per_frame_base_file)
    
    if os.path.exists(per_frame_base_path):
        base_img_path = per_frame_base_path
    else:
        # Fallback to latest_base.png or the frame itself
        base_img_path = os.path.join(OUTPUT_DIR, "latest_base.png")
        if not os.path.exists(base_img_path):
            base_img_path = last_frame_path

    try:
        img = Image.open(base_img_path)
        img = apply_logo_to_image(img, filename, lx, ly, lw, lh, l_opacity)
        img.save(last_frame_path)
        logit(f"Saved logo locally on frame: {last_frame_file}")
        return jsonify({"status": "ok"})
    except Exception as e:
        logit(f"Error saving logo locally: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def get_ollama_models():
    """
    Queries local Ollama instance for installed models.
    """
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2000)
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            return models
    except:
        pass
    return []

def resolve_model_name(model_name):
    """
    Resolves a requested model name to one of the installed Ollama models.
    """
    available = get_ollama_models()
    if not available:
        return model_name
    
    if model_name in available:
        return model_name
        
    # Case-insensitive match
    for m in available:
        if m.lower() == model_name.lower():
            return m
            
    # Try common alias mappings (e.g. llama3.2 -> llama3.2:3b)
    cleaned = model_name.lower().replace(":latest", "")
    for m in available:
        m_base = m.lower().split(":")[0]
        if m_base == cleaned:
            return m
            
    # Substring match (e.g. llama3.2 matching llama3.2:3b)
    for m in available:
        if cleaned in m.lower():
            return m
            
    return model_name


@app.route("/get_ollama_models")
def get_ollama_route():
    models = get_ollama_models()
    return jsonify({"models": models})

@app.route("/enhance_prompt", methods=["POST"])
def enhance_prompt():
    d = request.json
    user_prompt = d.get("prompt", "").strip()
    model = d.get("model", "").strip()
    model = resolve_model_name(model)
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
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=1500)
        if r.status_code == 200:
            enhanced = r.json().get("response", "").strip()
            if enhanced.startswith('"') and enhanced.endswith('"'):
                enhanced = enhanced[1:-1]
            
            # Clean quotes and punctuation
            enhanced = clean_desc(enhanced)
            
            global current_prompt, original_starting_prompt
            with state_lock:
                current_prompt = enhanced
                original_starting_prompt = enhanced
            save_state()
            
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
    global model_name, negative_prompt, lora1_name, lora2_name, lora3_name, current_seed, denoise_current, frames_current, current_prompt, original_starting_prompt
    global zoom_start, zoom_end, pan_start_x, pan_end_x, pan_start_y, pan_end_y, default_steps, default_cfg, use_motion_zoom, use_metadata_caption
    global logo_filename, logo_x, logo_y, logo_w, logo_h, logo_opacity
    global roll_mode
    global feedback_color_boost, feedback_contrast_boost, feedback_sharpness_boost
    global lora1_strength, lora2_strength, lora3_strength
    global use_visual_director, visual_director_interval, visual_director_model
    global use_prompt_interpolation, use_video_interpolation
    global caption_font_size, caption_x, caption_y, caption_bg_r, caption_bg_g, caption_bg_b, caption_bg_a
    d = request.json
    new_prompt = d.get("prompt")
    if new_prompt:
        if new_prompt != current_prompt:
            current_prompt = new_prompt
            original_starting_prompt = new_prompt
            logit(f"Prompt manually updated during stream. New baseline: '{current_prompt}'")
    caption_font_size = int(d.get("caption_font_size", caption_font_size))
    caption_x = int(d.get("caption_x", caption_x))
    caption_y = int(d.get("caption_y", caption_y))
    caption_bg_r = int(d.get("caption_bg_r", caption_bg_r))
    caption_bg_g = int(d.get("caption_bg_g", caption_bg_g))
    caption_bg_b = int(d.get("caption_bg_b", caption_bg_b))
    caption_bg_a = float(d.get("caption_bg_a", caption_bg_a))
    model_name = d.get("model"); negative_prompt = d.get("negative_prompt", negative_prompt)
    lora1_name = d.get("lora1"); lora2_name = d.get("lora2"); lora3_name = d.get("lora3", "None")
    current_seed = int(d.get("seed", current_seed)); denoise_current = float(d.get("denoise", 0.30))
    frames_current = int(d.get("frames", 120)); use_motion_zoom = bool(d.get("use_motion_zoom"))
    use_metadata_caption = bool(d.get("use_metadata_caption"))
    zoom_start = float(d.get("zoom_start", 1.0)); zoom_end = float(d.get("zoom_end", 1.1))
    pan_start_x = float(d.get("pan_start_x", 0.5)); pan_end_x = float(d.get("pan_end_x", 0.5))
    pan_start_y = float(d.get("pan_start_y", 0.5)); pan_end_y = float(d.get("pan_end_y", 0.5))
    roll_mode = d.get("roll_mode", "none")
    default_steps = int(d.get("steps", 14)); default_cfg = float(d.get("cfg", 5.4))
    feedback_color_boost = float(d.get("feedback_color", 1.03))
    feedback_contrast_boost = float(d.get("feedback_contrast", 1.01))
    feedback_sharpness_boost = float(d.get("feedback_sharpness", 1.10))
    
    lora1_strength = float(d.get("lora1_strength", 0.6))
    lora2_strength = float(d.get("lora2_strength", 0.6))
    lora3_strength = float(d.get("lora3_strength", 0.6))
    use_visual_director = bool(d.get("use_visual_director"))
    visual_director_interval = int(d.get("visual_director_interval", 15))
    visual_director_model = d.get("visual_director_model", "moondream")
    use_prompt_interpolation = bool(d.get("use_prompt_interpolation"))
    use_video_interpolation = bool(d.get("use_video_interpolation"))
    
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
        "prompt": current_prompt,
        "progress": comfy_progress,
        "max_steps": comfy_max_steps,
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "logo_filename": logo_filename,
        "logo_x": logo_x,
        "logo_y": logo_y,
        "logo_w": logo_w,
        "logo_h": logo_h,
        "logo_opacity": logo_opacity,
        "lora1_strength": lora1_strength,
        "lora2_strength": lora2_strength,
        "lora3_strength": lora3_strength,
        "use_visual_director": use_visual_director,
        "visual_director_interval": visual_director_interval,
        "visual_director_model": visual_director_model,
        "use_prompt_interpolation": use_prompt_interpolation,
        "use_video_interpolation": use_video_interpolation,
        "caption_font_size": caption_font_size,
        "caption_x": caption_x,
        "caption_y": caption_y,
        "caption_bg_r": caption_bg_r,
        "caption_bg_g": caption_bg_g,
        "caption_bg_b": caption_bg_b,
        "caption_bg_a": caption_bg_a
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
    global active_caption, caption_remaining, active_caption_font
    global temp_caption_font_size, temp_caption_x, temp_caption_y
    global temp_caption_bg_r, temp_caption_bg_g, temp_caption_bg_b, temp_caption_bg_a
    d = request.json
    t = d.get("text", "").strip()
    font = d.get("font", "Default")
    if t:
        with state_lock:
            active_caption = t
            caption_remaining = 5
            active_caption_font = font
            temp_caption_font_size = int(d.get("font_size", temp_caption_font_size))
            temp_caption_x = int(d.get("x", temp_caption_x))
            temp_caption_y = int(d.get("y", temp_caption_y))
            temp_caption_bg_r = int(d.get("bg_r", temp_caption_bg_r))
            temp_caption_bg_g = int(d.get("bg_g", temp_caption_bg_g))
            temp_caption_bg_b = int(d.get("bg_b", temp_caption_bg_b))
            temp_caption_bg_a = float(d.get("bg_a", temp_caption_bg_a))
        save_state()
        logit(f"Caption set: {active_caption} (Font: {font}, Size: {temp_caption_font_size}, Remaining: 5)")
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
        resp = requests.post(f"{COMFY_URL}/upload/image", files=files, timeout=1000).json()
        
        with state_lock:
            teleport_image = resp.get("name")
        
        logit(f"Teleport image set: {teleport_image}")
        return jsonify({"status": "ok", "filename": teleport_image})
    except Exception as e:
        logit(f"Teleport upload error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ============================================================
# JSON BUILDER TEMPLATE
# ============================================================
HTML_JSON_BUILDER = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EPOCH Liminal Spaces - Keyframe JSON Architect</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

        :root {
            --bg-primary: #0a0a0c;
            --bg-secondary: #121216;
            --bg-tertiary: #1a1a22;
            --accent: #e5a93b;
            --accent-glow: rgba(229, 169, 59, 0.2);
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
            --border-color: rgba(255, 255, 255, 0.08);
            --border-focus: rgba(229, 169, 59, 0.5);
            --success: #10b981;
            --error: #ef4444;
        }

        body {
            background-color: var(--bg-primary);
            color: var(--text-main);
            font-family: 'Outfit', sans-serif;
            margin: 0;
            padding: 0;
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }

        ::-webkit-scrollbar {
            width: 6px;
            height: 6px;
        }
        ::-webkit-scrollbar-track {
            background: var(--bg-primary);
        }
        ::-webkit-scrollbar-thumb {
            background: var(--bg-tertiary);
            border-radius: 3px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: var(--accent);
        }

        .header {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            padding: 10px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
            z-index: 10;
        }

        .header h1 {
            margin: 0;
            font-size: 20px;
            font-weight: 700;
            color: var(--accent);
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .header-links {
            display: flex;
            gap: 15px;
            align-items: center;
        }

        .header-link {
            color: var(--text-muted);
            text-decoration: none;
            font-size: 13px;
            font-weight: 500;
            transition: color 0.2s;
        }

        .header-link:hover {
            color: var(--accent);
        }

        .btn {
            background: var(--bg-tertiary);
            color: var(--text-main);
            border: 1px solid var(--border-color);
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }

        .btn:hover {
            border-color: var(--accent);
            box-shadow: 0 0 10px var(--accent-glow);
        }

        .btn-primary {
            background: var(--accent);
            color: #000;
            border: none;
        }

        .btn-primary:hover {
            background: #f0b852;
            box-shadow: 0 0 15px rgba(229, 169, 59, 0.4);
        }

        .btn-danger {
            background: rgba(239, 68, 68, 0.15);
            color: var(--error);
            border: 1px solid rgba(239, 68, 68, 0.3);
        }

        .btn-danger:hover {
            background: var(--error);
            color: #fff;
            border-color: var(--error);
        }

        .btn-success {
            background: rgba(16, 185, 129, 0.15);
            color: var(--success);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .btn-success:hover {
            background: var(--success);
            color: #fff;
            border-color: var(--success);
        }

        .status-badge {
            padding: 4px 10px;
            border-radius: 9999px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }

        .status-badge.running {
            background: rgba(16, 185, 129, 0.15);
            color: var(--success);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .status-badge.paused {
            background: rgba(229, 169, 59, 0.15);
            color: var(--accent);
            border: 1px solid rgba(229, 169, 59, 0.3);
        }

        .container {
            display: grid;
            grid-template-columns: 28% 44% 28%;
            height: calc(100vh - 57px);
            overflow: hidden;
        }

        .panel {
            background: var(--bg-secondary);
            display: flex;
            flex-direction: column;
            height: 100%;
            border-right: 1px solid var(--border-color);
            min-height: 0;
        }

        .panel:last-child {
            border-right: none;
        }

        .panel-header {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border-color);
            font-weight: 600;
            font-size: 14px;
            color: var(--text-main);
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--bg-tertiary);
        }

        .panel-body {
            padding: 16px;
            overflow-y: auto;
            flex-grow: 1;
        }

        .form-group {
            margin-bottom: 14px;
        }

        .form-row {
            display: flex;
            gap: 10px;
        }

        .form-row .form-group {
            flex: 1;
        }

        label {
            display: block;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }

        input, select, textarea {
            width: 100%;
            box-sizing: border-box;
            background: var(--bg-tertiary);
            color: var(--text-main);
            border: 1px solid var(--border-color);
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 13px;
            transition: all 0.2s;
            font-family: inherit;
        }

        input:focus, select:focus, textarea:focus {
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 2px var(--accent-glow);
        }

        textarea {
            resize: vertical;
        }

        .kf-card {
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 12px;
            position: relative;
            transition: all 0.2s;
        }

        .kf-card:hover {
            border-color: var(--accent-glow);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        }

        .kf-card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }

        .kf-badge {
            background: var(--accent);
            color: #000;
            font-weight: 700;
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 4px;
        }

        .json-textarea {
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            font-size: 12px;
            line-height: 1.5;
            background: #050507;
            height: 100%;
            resize: none;
        }

        .live-preview {
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            background: #000;
            aspect-ratio: 340/512;
            max-width: 150px;
            margin: 10px auto;
            position: relative;
        }

        .live-preview img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }

        .live-preview-label {
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            background: rgba(0,0,0,0.6);
            color: var(--accent);
            font-size: 9px;
            text-align: center;
            padding: 2px;
            font-weight: bold;
        }

        .notification {
            position: fixed;
            bottom: 20px;
            right: 20px;
            padding: 12px 24px;
            border-radius: 8px;
            background: var(--bg-tertiary);
            border-left: 4px solid var(--accent);
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
            display: none;
            z-index: 100;
            animation: slideIn 0.3s ease-out;
        }

        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }

        .loading-spinner {
            border: 2px solid rgba(255,255,255,0.1);
            border-radius: 50%;
            border-top: 2px solid var(--accent);
            width: 12px;
            height: 12px;
            animation: spin 0.8s linear infinite;
            display: inline-block;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent);"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><line x1="9" y1="3" x2="9" y2="21"></line><line x1="15" y1="3" x2="15" y2="21"></line><line x1="3" y1="9" x2="21" y2="9"></line><line x1="3" y1="15" x2="21" y2="15"></line></svg>
            EPOCH Liminal Spaces - Keyframe Architect
        </h1>
        <div class="header-links">
            <div id="status-container" class="status-badge paused">Offline</div>
            <a href="/" class="header-link">Main Dashboard</a>
            <a href="/media" class="header-link">Media Library</a>
            <button class="btn btn-primary" id="save-btn" onclick="saveConfigToServer()">Save to Server</button>
        </div>
    </div>

    <div class="container">
        <!-- LEFT PANEL: Global Configuration -->
        <div class="panel">
            <div class="panel-header">
                <span>Global Configuration</span>
            </div>
            <div class="panel-body">
                <div class="form-group">
                    <label>Stable Diffusion Model</label>
                    <select id="model" onchange="updateConfigField('model', this.value)">
                        {% for m in MODELS %}
                        <option value="{{ m }}">{{ m }}</option>
                        {% endfor %}
                    </select>
                </div>

                <div class="form-group">
                    <label>Base Prompt</label>
                    <textarea id="prompt" rows="3" oninput="updateConfigField('prompt', this.value)"></textarea>
                </div>

                <div class="form-group">
                    <label>Negative Prompt</label>
                    <textarea id="negative_prompt" rows="2" oninput="updateConfigField('negative_prompt', this.value)"></textarea>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Seed</label>
                        <input type="number" id="seed" oninput="updateConfigField('seed', parseInt(this.value) || 0)">
                    </div>
                    <div class="form-group">
                        <label>CFG Scale</label>
                        <input type="number" id="cfg" step="0.5" oninput="updateConfigField('cfg', parseFloat(this.value) || 0)">
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Steps</label>
                        <input type="number" id="steps" oninput="updateConfigField('steps', parseInt(this.value) || 0)">
                    </div>
                    <div class="form-group">
                        <label>Default Denoise</label>
                        <input type="number" id="denoise" step="0.05" oninput="updateConfigField('denoise', parseFloat(this.value) || 0)">
                    </div>
                </div>

                <div style="border-top: 1px solid var(--border-color); margin: 15px 0; padding-top: 10px;"></div>

                <label>LoRA 1 & Strength</label>
                <div class="form-row" style="margin-bottom: 8px;">
                    <select id="lora1" style="flex:2;" onchange="updateConfigField('lora1', this.value)">
                        {% for l in LORAS %}
                        <option value="{{ l }}">{{ l }}</option>
                        {% endfor %}
                    </select>
                    <input type="number" id="lora1_strength" step="0.1" style="flex:1;" oninput="updateConfigField('lora1_strength', parseFloat(this.value) || 0)">
                </div>

                <label>LoRA 2 & Strength</label>
                <div class="form-row" style="margin-bottom: 8px;">
                    <select id="lora2" style="flex:2;" onchange="updateConfigField('lora2', this.value)">
                        {% for l in LORAS %}
                        <option value="{{ l }}">{{ l }}</option>
                        {% endfor %}
                    </select>
                    <input type="number" id="lora2_strength" step="0.1" style="flex:1;" oninput="updateConfigField('lora2_strength', parseFloat(this.value) || 0)">
                </div>

                <label>LoRA 3 & Strength</label>
                <div class="form-row" style="margin-bottom: 8px;">
                    <select id="lora3" style="flex:2;" onchange="updateConfigField('lora3', this.value)">
                        {% for l in LORAS %}
                        <option value="{{ l }}">{{ l }}</option>
                        {% endfor %}
                    </select>
                    <input type="number" id="lora3_strength" step="0.1" style="flex:1;" oninput="updateConfigField('lora3_strength', parseFloat(this.value) || 0)">
                </div>

                <div style="border-top: 1px solid var(--border-color); margin: 15px 0; padding-top: 10px;"></div>

                <div class="form-group" style="display:flex; justify-content:space-between; align-items:center;">
                    <label style="margin-bottom:0;">Use Motion Zoom</label>
                    <input type="checkbox" id="use_motion_zoom" style="width:auto;" onchange="updateConfigField('use_motion_zoom', this.checked)">
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Zoom Start</label>
                        <input type="number" id="z_s" step="0.01" oninput="updateConfigField('z_s', parseFloat(this.value) || 0)">
                    </div>
                    <div class="form-group">
                        <label>Zoom End</label>
                        <input type="number" id="z_e" step="0.01" oninput="updateConfigField('z_e', parseFloat(this.value) || 0)">
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Pan X Start/End</label>
                        <div style="display:flex; gap:5px;">
                            <input type="number" id="px_s" step="0.05" oninput="updateConfigField('px_s', parseFloat(this.value) || 0)">
                            <input type="number" id="px_e" step="0.05" oninput="updateConfigField('px_e', parseFloat(this.value) || 0)">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Pan Y Start/End</label>
                        <div style="display:flex; gap:5px;">
                            <input type="number" id="py_s" step="0.05" oninput="updateConfigField('py_s', parseFloat(this.value) || 0)">
                            <input type="number" id="py_e" step="0.05" oninput="updateConfigField('py_e', parseFloat(this.value) || 0)">
                        </div>
                    </div>
                </div>

                <div class="form-group">
                    <label>Roll Mode</label>
                    <select id="roll_mode" onchange="updateConfigField('roll_mode', this.value)">
                        <option value="none">None</option>
                        <option value="left">Left Roll</option>
                        <option value="right">Right Roll</option>
                    </select>
                </div>

                <div style="border-top: 1px solid var(--border-color); margin: 15px 0; padding-top: 10px;"></div>
                
                <label>Stream Preview</label>
                <div class="live-preview">
                    <img id="stream-preview-img" src="/latest_frame" onerror="this.src='/static/border.png'">
                    <div class="live-preview-label" id="preview-label">Frame: Loading</div>
                </div>
            </div>
        </div>

        <!-- CENTER PANEL: Keyframes Sequence Manager -->
        <div class="panel">
            <div class="panel-header">
                <span>Keyframes Sequence</span>
                <span id="kf-count" style="font-size: 11px; background: var(--bg-tertiary); padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border-color);">0 Keyframes</span>
            </div>
            <div class="panel-body">
                <!-- Add Keyframe Form -->
                <div style="background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 8px; padding: 15px; margin-bottom: 20px;">
                    <h3 style="margin-top:0; margin-bottom:12px; font-size:13px; color:var(--accent); text-transform:uppercase; letter-spacing:0.5px;">Add New Keyframe</h3>
                    <div class="form-row">
                        <div class="form-group" style="flex:0.8;">
                            <label>Frame #</label>
                            <input type="number" id="new-kf-frame" min="0" placeholder="e.g. 50">
                        </div>
                        <div class="form-group" style="flex:1.2;">
                            <label>Denoise</label>
                            <input type="number" id="new-kf-denoise" step="0.05" min="0.1" max="1.0" value="0.55">
                        </div>
                        <div class="form-group" style="flex:1;">
                            <label>Seed Offset</label>
                            <input type="number" id="new-kf-offset" value="3">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Keyframe Prompt</label>
                        <textarea id="new-kf-prompt" rows="2" placeholder="Leave empty to use base prompt..."></textarea>
                    </div>
                    <button class="btn btn-primary" style="width:100%;" onclick="addKeyframeLocal()">Add Keyframe</button>
                </div>

                <div id="keyframes-container">
                    <!-- Keyframe cards will render here -->
                </div>
            </div>
        </div>

        <!-- RIGHT PANEL: AI Generation & Raw JSON -->
        <div class="panel">
            <div class="panel-header">
                <span>AI Generator & Raw JSON</span>
            </div>
            <div class="panel-body" style="display:flex; flex-direction:column; gap:15px; height:100%; box-sizing:border-box;">
                <!-- AI Keyframe Generator Section -->
                <div style="background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 8px; padding: 15px; flex-shrink:0;">
                    <h3 style="margin-top:0; margin-bottom:8px; font-size:13px; color:var(--accent); text-transform:uppercase; letter-spacing:0.5px; display:flex; justify-content:space-between; align-items:center;">
                        <span>Liminal AI Keyframe Generator</span>
                        <div class="loading-spinner" id="ai-spinner" style="display:none;"></div>
                    </h3>
                    
                    <div class="form-group">
                        <label>AI Outline / Script</label>
                        <textarea id="ai-outline" rows="3" value="frame 0: lost in liminal yellow corridor.&#10;frame 100: dark flickering fluorescent lights.&#10;frame 200: distant shadow moves.&#10;frame 300: sprinting through tiled hallways."></textarea>
                    </div>
                    <div class="form-group">
                        <label>Ollama Model</label>
                        <select id="ai-model">
                            <option value="loading">Loading Ollama Models...</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Custom Instructions</label>
                        <input type="text" id="ai-instructions" value="Make it visual, photorealistic, and extremely unsettling." value="Style settings...">
                    </div>
                    <button class="btn btn-success" style="width:100%;" id="ai-gen-btn" onclick="generateKeyframesAI()">Generate & Merge Keyframes</button>
                </div>

                <!-- Liminal Storyteller & Voiceover Section -->
                <div style="background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 8px; padding: 15px; flex-shrink:0; max-height: 300px; display: flex; flex-direction: column;">
                    <h3 style="margin-top:0; margin-bottom:8px; font-size:13px; color:var(--accent); text-transform:uppercase; letter-spacing:0.5px; display:flex; justify-content:space-between; align-items:center;">
                        <span>Liminal Storyteller & TTS</span>
                        <div class="loading-spinner" id="story-spinner" style="display:none;"></div>
                    </h3>
                    <p style="font-size: 11px; color: var(--text-muted); margin-top: 0; margin-bottom: 10px;">
                        Analyze generated frames, write a cohesive horror survival diary, and generate Kokoro TTS voiceovers.
                    </p>
                    <button class="btn btn-primary" style="width:100%; margin-bottom:10px;" id="story-btn" onclick="generateStoryDiary()">Compile Story & Audio Tracks</button>
                    
                    <div id="story-entries-container" style="flex-grow: 1; overflow-y: auto; gap: 8px; display: flex; flex-direction: column; max-height: 180px; padding-right: 2px;">
                        <!-- Story paragraphs will render here -->
                    </div>
                </div>

                <!-- Raw JSON Section -->
                <div style="display:flex; flex-direction:column; flex-grow:1; min-height:150px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                        <label style="margin-bottom:0;">Raw Config JSON</label>
                        <div style="display:flex; gap:6px;">
                            <button class="btn" style="padding:2px 6px; font-size:11px;" onclick="formatRawJson()">Format</button>
                            <button class="btn" style="padding:2px 6px; font-size:11px;" onclick="applyRawJsonToForm()">Apply</button>
                        </div>
                    </div>
                    <textarea id="raw-json" class="json-textarea" style="flex-grow:1; width:100%;"></textarea>
                </div>
            </div>
        </div>
    </div>

    <!-- Notification system -->
    <div class="notification" id="notification">Config saved successfully.</div>

    <script>
        let configData = {};
        let unsavedChanges = false;
        let selectedOllamaModel = '';

        window.addEventListener('DOMContentLoaded', () => {
            fetchConfigFromServer();
            fetchOllamaModels();
            
            setInterval(pollStatus, 3000);
            setInterval(updatePreview, 2500);
        });

        function showNotification(text, duration = 3000) {
            const el = document.getElementById('notification');
            el.innerText = text;
            el.style.display = 'block';
            setTimeout(() => {
                el.style.display = 'none';
            }, duration);
        }

        function fetchConfigFromServer() {
            fetch('/get_config')
                .then(res => {
                    if (!res.ok) throw new Error("Failed to load config");
                    return res.json();
                })
                .then(data => {
                    configData = data;
                    if(!configData.keyframes) configData.keyframes = {};
                    populateForm();
                    renderKeyframes();
                    updateRawJson();
                    unsavedChanges = false;
                    updateSaveButtonState();
                })
                .catch(err => {
                    console.error(err);
                    showNotification("Error loading configuration: " + err.message);
                });
        }

        function fetchOllamaModels() {
            fetch('/get_ollama_models')
                .then(res => res.json())
                .then(data => {
                    const select = document.getElementById('ai-model');
                    select.innerHTML = '';
                    if (data.models && data.models.length > 0) {
                        data.models.forEach(model => {
                            const opt = document.createElement('option');
                            opt.value = model;
                            opt.text = model;
                            select.appendChild(opt);
                        });
                        const preferred = ["dolphin3:8b", "mistral:7b-instruct", "llama3.2:3b", "qwen3:8b", "llama3.2", "llama3.2:latest"];
                        let foundPreferred = false;
                        for (let p of preferred) {
                            for (let opt of select.options) {
                                if (opt.value.includes(p)) {
                                    select.value = opt.value;
                                    foundPreferred = true;
                                    break;
                                }
                            }
                        }
                        if (!foundPreferred) {
                            select.value = data.models[0];
                        }
                    } else {
                        const opt = document.createElement('option');
                        opt.value = 'llama3.2';
                        opt.text = 'llama3.2 (Fallback)';
                        select.appendChild(opt);
                    }
                })
                .catch(err => {
                    console.error("Failed to load Ollama models", err);
                    const select = document.getElementById('ai-model');
                    select.innerHTML = '<option value="llama3.2">llama3.2 (Fallback)</option>';
                });
        }

        function pollStatus() {
            fetch('/status')
                .then(res => res.json())
                .then(data => {
                    const badge = document.getElementById('status-container');
                    if (data.running) {
                        badge.className = 'status-badge running';
                        badge.innerText = 'Running';
                    } else if (data.paused) {
                        badge.className = 'status-badge paused';
                        badge.innerText = 'Paused';
                    } else {
                        badge.className = 'status-badge';
                        badge.style.background = '#333';
                        badge.style.color = '#ccc';
                        badge.style.border = '1px solid #444';
                        badge.innerText = 'Idle';
                    }
                    
                    document.getElementById('preview-label').innerText = `Frame: ${data.frame} / ${data.total}`;
                })
                .catch(err => {
                    document.getElementById('status-container').className = 'status-badge';
                    document.getElementById('status-container').innerText = 'Offline';
                });
        }

        function updatePreview() {
            const img = document.getElementById('stream-preview-img');
            img.src = '/latest_frame?t=' + Date.now();
        }

        function populateForm() {
            const fields = [
                'model', 'prompt', 'negative_prompt', 'seed', 'cfg', 
                'steps', 'denoise', 'lora1', 'lora1_strength', 
                'lora2', 'lora2_strength', 'lora3', 'lora3_strength',
                'z_s', 'z_e', 'px_s', 'px_e', 'py_s', 'py_e', 'roll_mode'
            ];
            
            fields.forEach(field => {
                const el = document.getElementById(field);
                if (el) {
                    if (configData[field] !== undefined) {
                        el.value = configData[field];
                    }
                }
            });

            const check = document.getElementById('use_motion_zoom');
            if (check) {
                check.checked = !!configData.use_motion_zoom;
            }
        }

        function updateConfigField(field, val) {
            configData[field] = val;
            unsavedChanges = true;
            updateSaveButtonState();
            updateRawJson();
        }

        function renderKeyframes() {
            const container = document.getElementById('keyframes-container');
            container.innerHTML = '';
            
            const kfObj = configData.keyframes || {};
            const frames = Object.keys(kfObj).sort((a, b) => parseInt(b) - parseInt(a));
            
            document.getElementById('kf-count').innerText = `${frames.length} Keyframes`;
            
            if (frames.length === 0) {
                container.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted); font-size:13px; border:1px dashed var(--border-color); border-radius:8px;">No keyframes defined yet. Add some above!</div>';
                return;
            }
            
            frames.forEach(frame => {
                const kf = kfObj[frame];
                const card = document.createElement('div');
                card.className = 'kf-card';
                card.innerHTML = `
                    <div class="kf-card-header">
                        <span class="kf-badge">Frame ${frame}</span>
                        <button class="btn btn-danger" style="padding:2px 8px; font-size:11px;" onclick="deleteKeyframeLocal('${frame}')">Delete</button>
                    </div>
                    <div class="form-group">
                        <label>Prompt</label>
                        <textarea rows="2" style="font-size:12px; line-height:1.4;" oninput="updateKeyframeField('${frame}', 'prompt', this.value)">${kf.prompt || ''}</textarea>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Denoise</label>
                            <input type="number" step="0.05" min="0.1" max="1.0" value="${kf.denoise !== undefined ? kf.denoise : 0.5}" oninput="updateKeyframeField('${frame}', 'denoise', parseFloat(this.value) || 0.5)">
                        </div>
                        <div class="form-group">
                            <label>Seed Offset</label>
                            <input type="number" value="${kf.seed_offset !== undefined ? kf.seed_offset : 0}" oninput="updateKeyframeField('${frame}', 'seed_offset', parseInt(this.value) || 0)">
                        </div>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        function updateKeyframeField(frame, field, value) {
            if (configData.keyframes && configData.keyframes[frame]) {
                configData.keyframes[frame][field] = value;
                unsavedChanges = true;
                updateSaveButtonState();
                updateRawJson();
            }
        }

        function addKeyframeLocal() {
            const frameInput = document.getElementById('new-kf-frame');
            const promptInput = document.getElementById('new-kf-prompt');
            const denoiseInput = document.getElementById('new-kf-denoise');
            const offsetInput = document.getElementById('new-kf-offset');
            
            const frame = frameInput.value.trim();
            if (!frame || isNaN(parseInt(frame))) {
                alert("Please enter a valid frame number.");
                return;
            }
            
            const frameStr = parseInt(frame).toString();
            const prompt = promptInput.value.trim();
            const denoise = parseFloat(denoiseInput.value) || 0.5;
            const seed_offset = parseInt(offsetInput.value) || 0;
            
            if (!configData.keyframes) configData.keyframes = {};
            
            configData.keyframes[frameStr] = {
                prompt: prompt,
                denoise: denoise,
                seed_offset: seed_offset
            };
            
            frameInput.value = '';
            promptInput.value = '';
            
            unsavedChanges = true;
            updateSaveButtonState();
            renderKeyframes();
            updateRawJson();
            showNotification(`Keyframe ${frameStr} added locally.`);
        }

        function deleteKeyframeLocal(frame) {
            if (configData.keyframes && configData.keyframes[frame]) {
                delete configData.keyframes[frame];
                unsavedChanges = true;
                updateSaveButtonState();
                renderKeyframes();
                updateRawJson();
                showNotification(`Keyframe ${frame} deleted locally.`);
            }
        }

        function updateRawJson() {
            const txt = document.getElementById('raw-json');
            txt.value = JSON.stringify(configData, null, 2);
        }

        function formatRawJson() {
            const txt = document.getElementById('raw-json');
            try {
                const parsed = JSON.parse(txt.value);
                configData = parsed;
                if(!configData.keyframes) configData.keyframes = {};
                txt.value = JSON.stringify(configData, null, 2);
                populateForm();
                renderKeyframes();
                showNotification("JSON Formatted.");
            } catch (e) {
                alert("Invalid JSON: " + e.message);
            }
        }

        function applyRawJsonToForm() {
            const txt = document.getElementById('raw-json');
            try {
                const parsed = JSON.parse(txt.value);
                configData = parsed;
                if(!configData.keyframes) configData.keyframes = {};
                populateForm();
                renderKeyframes();
                unsavedChanges = true;
                updateSaveButtonState();
                showNotification("JSON parsed and loaded into editor.");
            } catch (e) {
                alert("Invalid JSON: " + e.message);
            }
        }

        function updateSaveButtonState() {
            const btn = document.getElementById('save-btn');
            if (unsavedChanges) {
                btn.innerText = "Save to Server *";
                btn.className = "btn btn-primary";
                btn.style.boxShadow = "0 0 15px rgba(229,169,59,0.5)";
            } else {
                btn.innerText = "Saved to Server";
                btn.className = "btn";
                btn.style.boxShadow = "none";
            }
        }

        function saveConfigToServer() {
            const btn = document.getElementById('save-btn');
            btn.disabled = true;
            btn.innerText = "Saving...";
            
            const rawText = document.getElementById('raw-json').value;
            try {
                configData = JSON.parse(rawText);
            } catch (e) {
                alert("Cannot save: Invalid JSON in editor box. " + e.message);
                btn.disabled = false;
                updateSaveButtonState();
                return;
            }
            
            fetch('/save_config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(configData)
            })
            .then(res => {
                if(!res.ok) throw new Error("Failed to save configuration.");
                return res.json();
            })
            .then(data => {
                unsavedChanges = false;
                updateSaveButtonState();
                btn.disabled = false;
                showNotification("Configuration saved and reloaded on server.");
            })
            .catch(err => {
                alert("Error saving config: " + err.message);
                btn.disabled = false;
                updateSaveButtonState();
            });
        }

        function generateKeyframesAI() {
            const outline = document.getElementById('ai-outline').value.trim();
            const model = document.getElementById('ai-model').value;
            const instructions = document.getElementById('ai-instructions').value;
            const genBtn = document.getElementById('ai-gen-btn');
            const spinner = document.getElementById('ai-spinner');
            
            if (!outline) {
                alert("Please enter a rough outline or script first.");
                return;
            }
            
            genBtn.disabled = true;
            genBtn.innerText = "Generating with Ollama...";
            spinner.style.display = 'inline-block';
            
            fetch('/generate_keyframes_ollama', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    outline: outline,
                    model: model,
                    instructions: instructions
                })
            })
            .then(res => {
                if (!res.ok) {
                    return res.json().then(errData => {
                        throw new Error(errData.error || "Ollama generation failed.");
                    });
                }
                return res.json();
            })
            .then(data => {
                if (data.status === 'ok' && data.keyframes) {
                    if (!configData.keyframes) configData.keyframes = {};
                    
                    const newKfs = data.keyframes;
                    let count = 0;
                    for (const frame in newKfs) {
                        configData.keyframes[frame] = newKfs[frame];
                        count++;
                    }
                    
                    unsavedChanges = true;
                    updateSaveButtonState();
                    renderKeyframes();
                    updateRawJson();
                    showNotification(`AI generated & merged ${count} keyframes successfully!`);
                } else {
                    throw new Error(data.error || "Unknown error during keyframe generation.");
                }
            })
            .catch(err => {
                alert("Ollama Error: " + err.message);
            })
            .finally(() => {
                genBtn.disabled = false;
                genBtn.innerText = "Generate & Merge Keyframes";
                spinner.style.display = 'none';
            });
        }

        function generateStoryDiary() {
            const btn = document.getElementById('story-btn');
            const spinner = document.getElementById('story-spinner');
            const container = document.getElementById('story-entries-container');
            
            btn.disabled = true;
            btn.innerText = "Analyzing & Synthesizing...";
            spinner.style.display = 'inline-block';
            container.innerHTML = '<div style="text-align:center; font-size:12px; color:var(--text-muted); padding:10px;">Running vision analysis, story composition, and Kokoro TTS speech generation... (this may take 1-2 minutes)</div>';
            
            fetch('/generate_story', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            })
            .then(res => {
                if(!res.ok) throw new Error("Story teller pipeline failed.");
                return res.json();
            })
            .then(data => {
                if (data.status === 'ok' && data.diary) {
                    container.innerHTML = '';
                    data.diary.forEach(item => {
                        const entryEl = document.createElement('div');
                        entryEl.style.background = 'var(--bg-secondary)';
                        entryEl.style.padding = '8px';
                        entryEl.style.borderRadius = '6px';
                        entryEl.style.border = '1px solid var(--border-color)';
                        entryEl.style.fontSize = '12px';
                        
                        entryEl.innerHTML = `
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                                <span style="font-weight:bold; color:var(--accent);">Frame ${item.frame}</span>
                                <span style="font-size:10px; color:var(--text-muted); font-style:italic;">${item.description.substring(0, 30)}...</span>
                            </div>
                            <div style="margin-bottom:6px; line-height:1.4;">${item.story}</div>
                            <audio controls style="width:100%; height:24px;" src="/static/darkrooms/narration_${String(item.frame).padStart(3, '0')}.mp3?t=${Date.now()}"></audio>
                        `;
                        container.appendChild(entryEl);
                    });
                    showNotification("Story diary and voice narration tracks compiled!");
                } else {
                    throw new Error(data.error || "Failed to compile story.");
                }
            })
            .catch(err => {
                alert("Storyteller Error: " + err.message);
                container.innerHTML = `<div style="text-align:center; font-size:12px; color:var(--error); padding:10px;">Error: ${err.message}</div>`;
            })
            .finally(() => {
                btn.disabled = false;
                btn.innerText = "Compile Story & Audio Tracks";
                spinner.style.display = 'none';
            });
        }
    </script>
</body>
</html>
"""

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
        <h3>FlaskArchitect's EpochDarkrooms Engine Config</h3>
        <div style="margin-bottom: 12px;">
            <a href="/json_builder" target="_blank" style="display: block; text-align: center; color: #fff; background: #8b5cf6; padding: 6px; border-radius: 4px; text-decoration: none; font-weight: bold; font-size: 11px; transition: background 0.2s;" onmouseover="this.style.background='#7c3aed'" onmouseout="this.style.background='#8b5cf6'">Open JSON Keyframe Builder</a>
        </div>
        <label>Base Prompt</label><textarea id="prompt" rows="3">You are an atmospheric horror narrator. A traveler is trapped and wandering lost in the Backrooms. Write a first-person,  psychological horror diary entry that progresses frame-by-frame, directly  matching this sequence of descriptions. Keep the tone tense, whispering, and dread-filled.</textarea>
        
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
        
        <label>LoRA 1 & Strength</label>
        <div style="display:flex; gap:5px; margin-top:4px;">
            <select id="lora1" style="flex:2; margin-top:0;">{% for l in LORAS %}<option>{{l}}</option>{% endfor %}</select>
            <input type="number" id="lora1_str" value="0.8" step="0.1" min="0" max="2.0" style="flex:1; margin-top:0;">
        </div>

        <label>LoRA 2 & Strength</label>
        <div style="display:flex; gap:5px; margin-top:4px;">
            <select id="lora2" style="flex:2; margin-top:0;">{% for l in LORAS %}<option>{{l}}</option>{% endfor %}</select>
            <input type="number" id="lora2_str" value="0.8" step="0.1" min="0" max="2.0" style="flex:1; margin-top:0;">
        </div>

        <label>LoRA 3 & Strength</label>
        <div style="display:flex; gap:5px; margin-top:4px;">
            <select id="lora3" style="flex:2; margin-top:0;">{% for l in LORAS %}<option>{{l}}</option>{% endfor %}</select>
            <input type="number" id="lora3_str" value="0.8" step="0.1" min="0" max="2.0" style="flex:1; margin-top:0;">
        </div>
        
        <!-- Row 1 -->
        <div style="display:flex; gap:5px;">
            <div style="flex:1;">
                <label>Seed</label>
                <input type="number" id="seed" value="123456">
            </div>

            <div style="flex:1;">
                <label>Steps</label>
                <input type="number" id="steps" value="14">
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
                <input type="number" id="denoise" step="0.05" value="0.30">
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
        
        <!-- Metadata Caption Style Customization -->
        <div style="background: #1c1c21; padding: 8px; border-radius: 4px; margin-top: 5px; border: 1px solid #333; margin-bottom: 5px;">
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

        <hr style="border:0; border-top:1px solid #333; margin:10px 0;">
        <h3 style="margin-top:10px; margin-bottom:5px;">AI Visual Director (moondream)</h3>
        <label><input type="checkbox" id="use_director" style="width:auto;"> ENABLE VISUAL DIRECTOR</label>
        <label>Decision Interval (Frames)</label>
        <input type="number" id="director_interval" value="25" min="5" max="200">
        <label>Description Model</label>
        <select id="director_model">
            <option value="LlaVa:latest">LlaVa:latest</option>
        </select>

        <hr style="border:0; border-top:1px solid #333; margin:10px 0;">
        <h3 style="margin-top:10px; margin-bottom:5px;">Morphing & Video Options</h3>
        <label><input type="checkbox" id="use_prompt_interpolation" style="width:auto;"> ENABLE PROMPT INTERPOLATION</label>
        <label><input type="checkbox" id="use_video_interpolation" style="width:auto;"> SMOOTH VIDEO INTERPOLATION (RIFE)</label>
        
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
        <label>Inject Keyword</label><input id="inj" placeholder="colorful"><button class="btn-blue" onclick="inject(this)">INJECT</button>
        
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
        <input type="number" id="logo_width_input" value="100" min="10" max="1000" oninput="changeLogoSize()" onchange="saveLogoLocally()">
        <label>Opacity</label>
        <input type="range" id="logo_opacity_slider" min="0" max="1" step="0.05" value="1.0" oninput="updateLogoOpacity(this.value)" onchange="saveLogoLocally()">
        <div id="logo_control_buttons" style="display:none; margin-top: 8px; gap: 4px; flex-wrap: wrap;">
            <button class="btn-blue" onclick="saveLogoLocally()" style="flex: 1; min-width: 120px;">Save Locally Only</button>
            <button class="btn-green" onclick="saveLogoToServer()" style="flex: 1; min-width: 120px;">Save to Server</button>
            <button class="btn-orange" onclick="cancelLogoPlacement()" style="flex: 1; min-width: 120px;">Clear Logo</button>
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
                            lora2:document.getElementById('lora2').value,
                            lora3:document.getElementById('lora3').value,
                            lora1_strength:document.getElementById('lora1_str').value,
                            lora2_strength:document.getElementById('lora2_str').value,
                            lora3_strength:document.getElementById('lora3_str').value,
                            seed:document.getElementById('seed').value,
                            denoise:document.getElementById('denoise').value,
                            frames:document.getElementById('frames').value,
                            steps:document.getElementById('steps').value,
                            cfg:document.getElementById('cfg').value,
                            use_motion_zoom: document.getElementById('use_zoom').checked,
                            use_metadata_caption: document.getElementById('use_caption').checked,
                            caption_font_size: document.getElementById('cap_font_size').value,
                            caption_x: document.getElementById('cap_x').value,
                            caption_y: document.getElementById('cap_y').value,
                            caption_bg_r: document.getElementById('cap_bg_r').value,
                            caption_bg_g: document.getElementById('cap_bg_g').value,
                            caption_bg_b: document.getElementById('cap_bg_b').value,
                            caption_bg_a: document.getElementById('cap_bg_a').value,
                            zoom_start:document.getElementById('zs').value,
                            zoom_end:document.getElementById('ze').value,
                            pan_start_x:document.getElementById('pxs').value,
                            pan_end_x:document.getElementById('pxe').value,
                            pan_start_y:document.getElementById('pys').value,
                            pan_end_y:document.getElementById('pye').value,
                            roll_mode:document.getElementById('roll_mode').value,
                            feedback_color:document.getElementById('fb_color').value,
                            feedback_contrast:document.getElementById('fb_contrast').value,
                            feedback_sharpness:document.getElementById('fb_sharpness').value,
                            use_visual_director:document.getElementById('use_director').checked,
                            visual_director_interval:document.getElementById('director_interval').value,
                            visual_director_model:document.getElementById('director_model').value,
                            use_prompt_interpolation:document.getElementById('use_prompt_interpolation').checked,
                            use_video_interpolation:document.getElementById('use_video_interpolation').checked
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
                    prompt:document.getElementById('prompt').value,
                    model:document.getElementById('model').value,
                    negative_prompt:document.getElementById('neg_prompt').value,
                    lora1:document.getElementById('lora1').value,
                    lora2:document.getElementById('lora2').value,
                    lora3:document.getElementById('lora3').value,
                    lora1_strength:document.getElementById('lora1_str').value,
                    lora2_strength:document.getElementById('lora2_str').value,
                    lora3_strength:document.getElementById('lora3_str').value,
                    seed:document.getElementById('seed').value,
                    denoise:document.getElementById('denoise').value,
                    frames:document.getElementById('frames').value,
                    steps:document.getElementById('steps').value,
                    cfg:document.getElementById('cfg').value,
                    use_motion_zoom: document.getElementById('use_zoom').checked,
                    use_metadata_caption: document.getElementById('use_caption').checked,
                    caption_font_size: document.getElementById('cap_font_size').value,
                    caption_x: document.getElementById('cap_x').value,
                    caption_y: document.getElementById('cap_y').value,
                    caption_bg_r: document.getElementById('cap_bg_r').value,
                    caption_bg_g: document.getElementById('cap_bg_g').value,
                    caption_bg_b: document.getElementById('cap_bg_b').value,
                    caption_bg_a: document.getElementById('cap_bg_a').value,
                    zoom_start:document.getElementById('zs').value,
                    zoom_end:document.getElementById('ze').value,
                    pan_start_x:document.getElementById('pxs').value,
                    pan_end_x:document.getElementById('pxe').value,
                    pan_start_y:document.getElementById('pys').value,
                    pan_end_y:document.getElementById('pye').value,
                    roll_mode:document.getElementById('roll_mode').value,
                    feedback_color:document.getElementById('fb_color').value,
                    feedback_contrast:document.getElementById('fb_contrast').value,
                    feedback_sharpness:document.getElementById('fb_sharpness').value,
                    use_visual_director:document.getElementById('use_director').checked,
                    visual_director_interval:document.getElementById('director_interval').value,
                    visual_director_model:document.getElementById('director_model').value,
                    use_prompt_interpolation:document.getElementById('use_prompt_interpolation').checked,
                    use_video_interpolation:document.getElementById('use_video_interpolation').checked
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

        // Load local logo parameters on startup if they exist
        try {
            const localParamsStr = localStorage.getItem("local_logo_params");
            if (localParamsStr) {
                const localParams = JSON.parse(localParamsStr);
                currentLogoFilename = localParams.logo_filename;
                currentLogoX = localParams.x;
                currentLogoY = localParams.y;
                currentLogoW = localParams.w;
                currentLogoH = localParams.h;
                currentLogoOpacity = localParams.opacity;
            }
        } catch(e) {}

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
                    setTimeout(() => location.reload(), 2000);
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
                    
                    // Immediately calculate and sync local coordinates to prevent status polling snap-back
                    const parentRect = document.getElementById("overlay_logo_wrapper").getBoundingClientRect();
                    if (parentRect.width > 0 && parentRect.height > 0) {
                        const scaleX = currentFrameWidth / parentRect.width;
                        const scaleY = currentFrameHeight / parentRect.height;
                        currentLogoX = Math.round(parseFloat(dragLogoEl.style.left) * scaleX);
                        currentLogoY = Math.round(parseFloat(dragLogoEl.style.top) * scaleY);
                    }
                    
                    // Autosave locally on drop
                    saveLogoLocally();
                }
            });
        }

        function changeLogo(filename) {
            if (filename === "None") {
                currentLogoFilename = "None";
                localStorage.removeItem("local_logo_params");
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
            currentLogoH = 0; // set to 0 so onload handler calculates correct aspect ratio
            currentLogoOpacity = 1.0;
            initialSyncDone = true; // Mark sync done immediately to prevent status poll overriding
            localStorage.setItem("local_logo_params", JSON.stringify({
                logo_filename: filename,
                x: 20,
                y: 20,
                w: 100,
                h: 0,
                opacity: 1.0
            }));
            updateLogoOverlayDisplay();
        }

        function changeLogoSize() {
            const el = document.getElementById("draggable_logo");
            const widthInput = document.getElementById("logo_width_input");
            if (!el || !widthInput) return;
            const w = parseInt(widthInput.value) || 100;
            const ratio = (el.naturalWidth > 0) ? (el.naturalHeight / el.naturalWidth) : 1.0;
            const h = Math.round(w * ratio);
            el.style.width = w + "px";
            el.style.height = h + "px";
            
            // Sync local dimensions immediately to prevent polling reset
            currentLogoW = w;
            currentLogoH = h;
        }

        function updateLogoOpacity(val) {
            currentLogoOpacity = parseFloat(val);
            const el = document.getElementById("draggable_logo");
            if (el) el.style.opacity = currentLogoOpacity;
        }

        function cancelLogoPlacement() {
            localStorage.removeItem("local_logo_params");
            changeLogo("None");
        }

        function calculateCurrentLogoParams() {
            const select = document.getElementById("logo_select");
            const logo_filename = select ? select.value : currentLogoFilename;
            const opacitySlider = document.getElementById("logo_opacity_slider");
            const logo_opacity = opacitySlider ? parseFloat(opacitySlider.value) : currentLogoOpacity;
            
            return {
                logo_filename: logo_filename,
                x: currentLogoX,
                y: currentLogoY,
                w: currentLogoW,
                h: currentLogoH,
                opacity: logo_opacity
            };
        }

        async function saveLogoLocally() {
            const params = calculateCurrentLogoParams();
            if (!params) return;
            currentLogoFilename = params.logo_filename;
            currentLogoX = params.x;
            currentLogoY = params.y;
            currentLogoW = params.w;
            currentLogoH = params.h;
            currentLogoOpacity = params.opacity;
            
            // Save to browser's localStorage
            localStorage.setItem("local_logo_params", JSON.stringify(params));
            
            // Instantly overlay on the current frame saved locally on server's disk
            const resp = await fetch("/save_logo_local", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(params)
            });
            if (resp.ok) {
                showFeedback("Logo position saved locally & overlaid on current frame!");
                // Force preview update to show the newly overlaid logo
                const previewImg = document.getElementById("preview");
                if (previewImg) {
                    previewImg.src = "/latest_frame?t=" + Date.now();
                }
            } else {
                showFeedback("Logo position saved locally in browser!");
            }
        }

        async function saveLogoToServer() {
            const params = calculateCurrentLogoParams();
            if (!params) return;
            currentLogoFilename = params.logo_filename;
            currentLogoX = params.x;
            currentLogoY = params.y;
            currentLogoW = params.w;
            currentLogoH = params.h;
            currentLogoOpacity = params.opacity;
            
            // Save to browser's localStorage
            localStorage.setItem("local_logo_params", JSON.stringify(params));
            
            const resp = await fetch("/save_logo_position", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(params)
            });
            if (resp.ok) {
                showFeedback("Logo position saved to server!");
            } else {
                alert("Failed to save logo position to server.");
            }
        }

        function safeSyncValue(id, val) {
            const el = document.getElementById(id);
            if (el && document.activeElement !== el) {
                el.value = val;
            }
        }

        function safeSyncChecked(id, checked) {
            const el = document.getElementById(id);
            if (el && document.activeElement !== el) {
                el.checked = checked;
            }
        }

        function updateLogoOverlayDisplay() {
            if (isDraggingLogo) return;
            const select = document.getElementById("logo_select");
            const container = document.getElementById("overlay_logo_wrapper");
            if (!select || !container) return;
            if (document.activeElement !== select && select.value !== currentLogoFilename) {
                select.value = currentLogoFilename;
            }
            const opacitySlider = document.getElementById("logo_opacity_slider");
            if (document.activeElement !== opacitySlider) {
                opacitySlider.value = currentLogoOpacity;
            }
            if (currentLogoFilename === "None") {
                container.innerHTML = "";
                document.getElementById("logo_control_buttons").style.display = "none";
                return;
            }
            document.getElementById("logo_control_buttons").style.display = "flex";
            let el = document.getElementById("draggable_logo");
            if (!el) {
                el = document.createElement("img");
                el.id = "draggable_logo";
                el.className = "draggable";
                el.style.position = "absolute";
                el.style.pointerEvents = "auto";
                el.style.cursor = "move";
                el.style.outline = "2px dashed #3b82f6";
                el.onload = function() {
                    if (el.naturalWidth > 0 && currentLogoH === 0) {
                        const ratio = el.naturalHeight / el.naturalWidth;
                        currentLogoH = Math.round(currentLogoW * ratio);
                        const parentRect = container.getBoundingClientRect();
                        if (parentRect.width > 0 && parentRect.height > 0) {
                            const scaleY = parentRect.height / currentFrameHeight;
                            el.style.height = Math.round(currentLogoH * scaleY) + "px";
                        }
                        saveLogoLocally();
                    }
                };
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
                
                const widthInput = document.getElementById("logo_width_input");
                if (document.activeElement !== widthInput) {
                    widthInput.value = currentLogoW;
                }
            }
        }

        function loadOllamaModels() {
            fetch('/get_ollama_models')
            .then(r => r.json())
            .then(d => {
                const select = document.getElementById("ollama_model");
                const dirSelect = document.getElementById("director_model");
                select.innerHTML = "";
                dirSelect.innerHTML = "";
                if (d.models && d.models.length > 0) {
                    d.models.forEach(m => {
                        const opt = document.createElement("option");
                        opt.value = m;
                        opt.text = m;
                        select.appendChild(opt);
                        
                        const optDir = document.createElement("option");
                        optDir.value = m;
                        optDir.text = m;
                        dirSelect.appendChild(optDir);
                    });
                    const preferred = ["dolphin3:8b", "mistral:7b-instruct", "llama3.2:3b", "qwen3:8b"];
                    for (let p of preferred) {
                        for (let opt of select.options) {
                            if (opt.value.includes(p)) {
                                select.value = opt.value;
                                break;
                            }
                        }
                    }
                    const preferredDir = ["moondream", "llava", "dolphin3", "llama3.2", "mistral"];
                    for (let p of preferredDir) {
                        for (let opt of dirSelect.options) {
                            if (opt.value.includes(p)) {
                                dirSelect.value = opt.value;
                                break;
                            }
                        }
                    }
                } else {
                    const opt = document.createElement("option");
                    opt.value = "None";
                    opt.text = "No Ollama models found / offline";
                    select.appendChild(opt);
                    
                    const optDir = document.createElement("option");
                    optDir.value = "None";
                    optDir.text = "No Ollama models found / offline";
                    dirSelect.appendChild(optDir);
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
            
            const previewImg = document.getElementById("preview");
            if (previewImg) {
                // Set placeholder sizing before loading
                previewImg.style.width = currentFrameWidth + "px";
                previewImg.style.height = currentFrameHeight + "px";
                previewImg.style.background = "#111";
                
                // Attempt to load the latest frame
                previewImg.src = "/latest_frame?t=" + Date.now();
                
                previewImg.onload = function() {
                    // Reset custom placeholder styles when image loads
                    previewImg.style.width = "";
                    previewImg.style.height = "";
                    previewImg.style.background = "";
                    updateLogoOverlayDisplay();
                };
                
                previewImg.onerror = function() {
                    // Keep placeholder styles if image fails to load
                    previewImg.style.width = currentFrameWidth + "px";
                    previewImg.style.height = currentFrameHeight + "px";
                    previewImg.style.background = "#111";
                    updateLogoOverlayDisplay();
                };
            }
        });

        let initialSyncDone = false;
        let lastLoadedFrame = -1;

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
                
                if(d.frame > 0 && d.frame !== lastLoadedFrame) {
                    document.getElementById('preview').src = '/latest_frame?t=' + Date.now();
                    lastLoadedFrame = d.frame;
                }
                if(d.history) document.getElementById('thumb_strip').innerHTML = d.history.map(i=>`<img src="/static/darkrooms/${i}?t=${Date.now()}">`).join('');
                if(d.injections) document.getElementById('injections').innerHTML = d.injections.map(i=>`<span class="tag">${i}</span>`).join('');
                
                if (!pauseSync || !initialSyncDone) {
                    // Sync prompt text area
                    if (d.prompt !== undefined) safeSyncValue('prompt', d.prompt);
                    // Sync caption checkbox
                    safeSyncChecked('use_caption', d.metadata_caption);
                    if (d.caption_font_size !== undefined) safeSyncValue('cap_font_size', d.caption_font_size);
                    if (d.caption_x !== undefined) safeSyncValue('cap_x', d.caption_x);
                    if (d.caption_y !== undefined) safeSyncValue('cap_y', d.caption_y);
                    if (d.caption_bg_r !== undefined) safeSyncValue('cap_bg_r', d.caption_bg_r);
                    if (d.caption_bg_g !== undefined) safeSyncValue('cap_bg_g', d.caption_bg_g);
                    if (d.caption_bg_b !== undefined) safeSyncValue('cap_bg_b', d.caption_bg_b);
                    if (d.caption_bg_a !== undefined) safeSyncValue('cap_bg_a', d.caption_bg_a);
                    
                    // Sync Logo (only on initial load to prevent status poll from overriding unsaved local placements)
                    if (!initialSyncDone && d.logo_filename !== undefined) {
                        const localParamsStr = localStorage.getItem("local_logo_params");
                        if (localParamsStr) {
                            try {
                                const localParams = JSON.parse(localParamsStr);
                                currentLogoFilename = localParams.logo_filename;
                                currentLogoX = localParams.x;
                                currentLogoY = localParams.y;
                                currentLogoW = localParams.w;
                                currentLogoH = localParams.h;
                                currentLogoOpacity = localParams.opacity;
                            } catch(e) {
                                currentLogoFilename = d.logo_filename;
                                currentLogoX = d.logo_x;
                                currentLogoY = d.logo_y;
                                currentLogoW = d.logo_w;
                                currentLogoH = d.logo_h;
                                currentLogoOpacity = d.logo_opacity;
                            }
                        } else {
                            currentLogoFilename = d.logo_filename;
                            currentLogoX = d.logo_x;
                            currentLogoY = d.logo_y;
                            currentLogoW = d.logo_w;
                            currentLogoH = d.logo_h;
                            currentLogoOpacity = d.logo_opacity;
                        }
                    }
                    if (d.width !== undefined) currentFrameWidth = d.width;
                    if (d.height !== undefined) currentFrameHeight = d.height;
                    updateLogoOverlayDisplay();
                    
                    // Sync Ollama & Interpolation params
                    if (d.use_visual_director !== undefined) safeSyncChecked('use_director', d.use_visual_director);
                    if (d.visual_director_interval !== undefined) safeSyncValue('director_interval', d.visual_director_interval);
                    if (d.visual_director_model !== undefined) safeSyncValue('director_model', d.visual_director_model);
                    if (d.use_prompt_interpolation !== undefined) safeSyncChecked('use_prompt_interpolation', d.use_prompt_interpolation);
                    if (d.use_video_interpolation !== undefined) safeSyncChecked('use_video_interpolation', d.use_video_interpolation);
                    if (d.lora1_strength !== undefined) safeSyncValue('lora1_str', d.lora1_strength);
                    if (d.lora2_strength !== undefined) safeSyncValue('lora2_str', d.lora2_strength);
                    if (d.lora3_strength !== undefined) safeSyncValue('lora3_str', d.lora3_strength);
                    
                    initialSyncDone = true;
                }
            });
        }, 4000);
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

SHARED_PATH = os.path.join(BASE_DIR, "static", "darkrooms")
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
<a href="/text_to_mp3">Generate MP3</a><br>
<a href="/compile_story_movie" style="color: #4ade80; font-size: 2.2vw; font-weight: bold; display: block; margin-top: 15px; text-decoration: underline;">Compile Synchronized Story Video (Images + Narrations)</a>

<form method="post" action="/process_video">

<h2>Videos</h2>
{% for v in videos %}
<div class="box">
<video controls src="{{ url_for('static', filename='darkrooms/' + v) }}"></video><br>
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
<img src="{{ url_for('static', filename='darkrooms/' + img) }}"><br>
<input type="checkbox" name="images" value="{{ img }}">
</div>
{% endfor %}

<h2>Audio</h2>
{% for mp3 in mp3s %}
<div class="box" style="vertical-align: top;">
<audio controls style="width:250px; display:block; margin: 0 auto 8px auto;">
    <source src="{{ url_for('static', filename='darkrooms/' + mp3) }}" type="audio/mpeg">
</audio>
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

    audio_path = os.path.join(SHARED_PATH, selected_audio)
    padded_audio = os.path.join(SHARED_PATH, "_pad.mp3")

    pad_audio(audio_path, padded_audio, start_sec, end_sec)

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

        # Sort images chronologically (alphabetically) to prevent reverse playback
        for img in sorted(selected_images):
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
# COMPILE STORY MOVIE
# --------------------------------------------------
@app.route("/compile_story_movie")
def compile_story_movie():
    import re
    # Find all narration MP3 files
    mp3_files = sorted([f for f in os.listdir(SHARED_PATH) if f.startswith("narration_") and f.endswith(".mp3")])
    if not mp3_files:
        return "No narration MP3 files found in static/darkrooms/. Please generate your story first.", 400

    clips = []
    for mp3_name in mp3_files:
        match = re.search(r'narration_(\d+)\.mp3$', mp3_name)
        if not match:
            continue
        idx = int(match.group(1))

        img_candidates = [
            f"clean_base_{idx:03d}.png",
            f"frame_{idx:03d}.png",
            f"clean_{idx:03d}.png"
        ]
        
        img_path = None
        for cand in img_candidates:
            cand_path = os.path.join(SHARED_PATH, cand)
            if os.path.exists(cand_path):
                img_path = cand_path
                break
                
        if not img_path:
            continue

        try:
            audio_clip = AudioFileClip(os.path.join(SHARED_PATH, mp3_name))
            img_clip = ImageClip(img_path).set_duration(audio_clip.duration)
            video_segment = img_clip.set_audio(audio_clip)
            clips.append(video_segment)
        except Exception as e:
            logit(f"Error processing index {idx:03d} for story video: {e}")

    if not clips:
        return "No valid video segments (matching image-audio pairs) could be created.", 400

    out = os.path.join(SHARED_PATH, "story_production.mp4")
    try:
        final_video = concatenate_videoclips(clips, method="compose")
        final_video.write_videofile(
            out,
            fps=24,
            codec="libx264",
            audio_codec="aac"
        )
        return redirect(url_for("serve_video", filename="story_production.mp4"))
    except Exception as e:
        return f"Error during video compilation: {str(e)}", 500

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
    load_state()
    app.run(host="0.0.0.0", port=5003, debug=False, use_reloader=False)
