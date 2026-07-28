"""
================================================================================
                           EPOCH STREAMER ENGINE v2
                        - ANNOTATED REFERENCE EDITION -
================================================================================

This file serves as a comprehensive reference guide to understand the inner workings
of the AI Streamer Engine. It details how the system integrates Flask, ComfyUI,
Ollama, PIL, moviepy, and FFmpeg into a real-time visual streaming feedback loop.

CORE ARCHITECTURE:
1. Flask Web Server (Port 5002):
   Serves the Frontend UI, manages the global state (configuration parameters),
   handles file uploads (logos, teleport frames), and triggers video compiling.
   
2. Multi-threaded Render Loop:
   Runs in a background thread to prevent blocking the web server. It continuously
   queries ComfyUI, downloads generated frames, performs PIL image operations, 
   runs the AI Visual Director, and saves state to disk.
   
3. ComfyUI Integration:
   Communicates with ComfyUI over HTTP (posting prompts) and WebSockets (listening
   for live progress updates and execution completions).
   
4. Image-to-Image (Img2Img) Feedback Loop:
   Feeds the previous frame back into ComfyUI as the starter image (init image)
   with a low denoise setting (typically 0.35). This creates sequential video frames
   that inherit composition and colors from the last frame.
   
5. Visual Director (Ollama Vision):
   Every N frames, a lightweight vision model (like Moondream) describes the current
   frame. The engine programmatically parses this description and blends it with
   your original starting theme to dynamically evolve the scene.
"""

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

# ==============================================================================
# CONFIG & PATHS
# ==============================================================================
# The address of your ComfyUI instance. Port 5001 is used for API communication.
COMFY_URL = "http://192.168.1.41:5001"

# The address of your local Ollama instance running vision/text models on CPU.
OLLAMA_URL = "http://localhost:11434"

# A unique client ID generated per session to identify this client's requests in ComfyUI.
CLIENT_ID = str(uuid.uuid4())

# Global progress trackers updated by the ComfyUI WebSocket listener.
comfy_progress = 0
comfy_max_steps = 0

# Default frame resolution dimensions.
DEFAULT_WIDTH = 340
DEFAULT_HEIGHT = 512

# Project directory paths.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "streamer")
STATE_FILE = os.path.join(OUTPUT_DIR, "streamer.json")
LOG_FILE_PATH = os.path.join(OUTPUT_DIR, "streamer.txt")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ==============================================================================
# GLOBAL STATE VARIABLES
# ==============================================================================
# Threading locks to prevent race conditions when reading/writing global state.
state_lock = Lock()
render_lock = Lock()

# Stream status flags.
running = False
paused = False
current_frame = 0
frames_current = 500
current_seed = random.randint(111111, 999999)

# Active model selection keys matching folders in your ComfyUI models directory.
model_name = "dreamshaper_8.safetensors"
vae_name = "vae-ft-mse-840000-ema-pruned.safetensors"
lora1_name = "more_details.safetensors"
lora2_name = "face_only_01.safetensors"
lora3_name = "None"
lora1_strength = 0.8
lora2_strength = 0.8
lora3_strength = 0.8

# AI Visual Director configuration.
use_visual_director = False
visual_director_interval = 25
visual_director_model = "moondream"

# Morphing & Video Interpolation flags.
use_prompt_interpolation = False
use_video_interpolation = False

# Image-to-Image baseline settings.
denoise_current = 0.35
teleport_image = None
active_caption = ""
caption_remaining = 0

# Visual Overlay Caption Settings (Metadata/Permanent Caption).
caption_font_size = 12
caption_x = 10
caption_y = 10
caption_bg_r = 61
caption_bg_g = 81
caption_bg_b = 92
caption_bg_a = 0.4

# Temporary Caption settings (Armed for 5 frames).
temp_caption_font_size = 20
temp_caption_x = 20
temp_caption_y = 20
temp_caption_bg_r = 0
temp_caption_bg_g = 0
temp_caption_bg_b = 0
temp_caption_bg_a = 0.5
active_caption_font = "Default"
active_caption_font_size = 20

# Drag-and-drop Logo overlay settings.
logo_filename = "None"
logo_x = 0
logo_y = 0
logo_w = 100
logo_h = 100
logo_opacity = 1.0

# Feedback Loop Stabilizers (Color/Contrast/Sharpness boosters).
# Applied to the clean frame before loading it back into ComfyUI.
# Counteracts the natural tendency of VAEs to bleach colors or blur images over loops.
feedback_color_boost = 1.03
feedback_contrast_boost = 1.01
feedback_sharpness_boost = 1.10

# The initial text prompts (updated dynamically as the stream evolves).
current_prompt = "Highly detailed Centered Science fiction image of a star-gate with semi transparent space creatures swimming in space similar to mythical sea monsters, surrounded with space, stars, planets, nebula, dust and space debris <lora:more_details:.8>"
original_starting_prompt = current_prompt

def logit(*args):
    """
    Appends a formatted log message with a timestamp to static/streamer/streamer.txt.
    Thread-safe and fails silently in case of file locking conflicts.
    """
    try:
        msg = " ".join(map(str, args))
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE_PATH, "a") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: 
        pass

# ==============================================================================
# WEBSOCKET PROGRESS LISTENER
# ==============================================================================
def listen_to_comfy():
    """
    Runs in a background thread to listen to the ComfyUI WebSocket API.
    Updates the global progress variables ('comfy_progress' and 'comfy_max_steps')
    and notifies the engine when a prompt ID completes rendering.
    """
    global comfy_progress, comfy_max_steps, comfy_finished_prompts
    # Convert HTTP URL to WebSocket URL
    ws_url = COMFY_URL.replace("http://", "ws://") + f"/ws?clientId={CLIENT_ID}"
    while True:
        try:
            ws = websocket.create_connection(ws_url, timeout=20)
            while True:
                result = ws.recv()
                if isinstance(result, str):
                    msg = json.loads(result)
                    m_type = msg.get('type')
                    m_data = msg.get('data', {})
                    
                    # Handles step progress within the KSampler node
                    if m_type == 'progress':
                        with state_lock:
                            comfy_progress = m_data.get('value', 0)
                            comfy_max_steps = m_data.get('max', 0)
                    
                    # Handles node execution completion signals
                    elif m_type == 'executing':
                        # If node is None, it indicates the entire prompt is finished
                        if m_data.get('node') is None:
                            p_id = m_data.get('prompt_id')
                            if p_id:
                                with state_lock:
                                    comfy_finished_prompts.add(p_id)
                else:
                    continue 
        except Exception:
            # Reconnect after a brief delay if connection is dropped
            time.sleep(5)

comfy_finished_prompts = set()
# Start WebSocket listener in a daemon thread so it runs in the background
Thread(target=listen_to_comfy, daemon=True).start()

# Default negative prompts to discourage low quality features or distortions
negative_prompt = "low quality, blurry, nudity, breasts, NSWF"

injection_lines = []
MAX_LINES = 5
keyframes = {}

# Motion Zoom & Pan Parameters
use_motion_zoom = True
use_metadata_caption = False
zoom_start = 1.0
zoom_end = 1.01
pan_start_x = 0.5
pan_end_x = 0.5
pan_start_y = 0.5
pan_end_y = 0.5

roll_mode = "none" # Options: "none", "right", "left"

default_steps = 14
default_cfg = 4.0

# Query loaded checkpoints and LoRAs from ComfyUI on startup
try:
    MODELS = requests.get(f"{COMFY_URL}/models/checkpoints", timeout=30).json()
    LORAS = ["None"] + requests.get(f"{COMFY_URL}/models/loras", timeout=30).json()
except:
    MODELS, LORAS = [], ["None"]

# ==============================================================================
# SPACESHIP OVERLAY & MOVEMENT
# ==============================================================================
def move_spaceship(img, frame_idx, w, h, spaceship_path="static/blank.png"):
    """
    Overlays a small spaceship graphic on top of the frame.
    Calculates coordinates dynamically to slide the spaceship from right to left,
    applying a sinusoidal vertical drift and subtle scale change (depth illusion).
    """
    if not os.path.exists(spaceship_path):
        try:
            os.makedirs(os.path.dirname(spaceship_path), exist_ok=True)
            # Create a placeholder red triangle spaceship if file doesn't exist
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

        # Slide horizontally from right edge to left edge based on frame index
        speed = 2.0
        cycle_len = w + ship_w
        offset = (frame_idx * speed) % cycle_len
        ship_x = w - offset

        # Create a wavy vertical float using a sine wave
        drift = int(20 * np.sin(frame_idx * 0.05))
        ship_y = (h // 2 - ship_h // 2) + drift

        # Scale size dynamically using a sine wave to simulate moving closer/further
        scale = 1.0 + 0.05 * np.sin(frame_idx * 0.03)
        new_w = int(ship_w * scale)
        new_h = int(ship_h * scale)
        ship_resized = ship.resize((new_w, new_h), Image.LANCZOS)

        # Draw the spaceship layer onto the base frame image
        ship_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ship_layer.paste(ship_resized, (int(ship_x), int(ship_y)), ship_resized)

        return Image.alpha_composite(img, ship_layer)

    except Exception as e:
        logit(f"Spaceship overlay error: {e}")
        return img

# ==============================================================================
# MOVIE EXPORTER (FFMPEG Wrapper)
# ==============================================================================
def create_movie_from_frames(output_filename="production_june8.mp4"):
    """
    Invokes FFmpeg subprocess to compile the compiled static frame images
    (frame_000.png, frame_001.png...) into a unified H.264 video.
    Optionally applies RIFE/minterpolate to smooth frame rate from 5fps to 24fps.
    """
    logit("Joining images to create movie...")
    try:
        import subprocess
        if use_video_interpolation:
            # Uses motion vector estimation to interpolate intermediate frames
            cmd = [
                "ffmpeg", "-y", "-framerate", "5", 
                "-i", os.path.join(OUTPUT_DIR, "frame_%03d.png"),
                "-vf", "minterpolate=fps=24:mi_mode=mci:mc_me=epzs:me_mode=bidir",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", 
                os.path.join(OUTPUT_DIR, output_filename)
            ]
        else:
            # Basic slideshow compile
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

# ==============================================================================
# IMAGE PROCESSING HELPERS (PIL)
# ==============================================================================
def apply_pil_zoom(img, frame_idx, total_frames, overlay_png_path=None, overlay_opacity=0.10, spaceship_path="static/spaceship.png"):
    """
    Calculates zoom crops and pan points mathematically to apply dynamic motion.
    Also handles custom watermark logo placement and rotating (roll) the image.
    """
    w, h = img.size
    img = img.convert("RGBA")

    # 1. Composite Custom Logo Watermark (or fullscreen layout fallback)
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

    # 2. Draw Moving Spacecraft
    img = move_spaceship(img, frame_idx, w, h, spaceship_path=spaceship_path)

    # 3. Calculate Pan and Zoom Crops
    progress = frame_idx / max(total_frames - 1, 1)
    curr_zoom = zoom_start + (zoom_end - zoom_start) * progress
    curr_pan_x = pan_start_x + (pan_end_x - pan_start_x) * progress
    curr_pan_y = pan_start_y + (pan_end_y - pan_start_y) * progress

    # Define crop bounding box dimensions
    crop_w = w / curr_zoom
    crop_h = h / curr_zoom

    # Calculate coordinates centering on current pan coordinates
    left = (w * curr_pan_x) - (crop_w / 2)
    top = (h * curr_pan_y) - (crop_h / 2)

    # Prevent crop boundaries from extending beyond original dimensions
    left = max(0, min(w - crop_w, left))
    top = max(0, min(h - crop_h, top))
    right = left + crop_w
    bottom = top + crop_h

    img = img.crop((left, top, right, bottom)).resize((w, h), Image.LANCZOS)
    
    # 4. Rotation (Roll Effect)
    if roll_mode != "none":
        direction = -1 if roll_mode == "right" else 1
        angle = frame_idx * 0.005 * direction
        img = img.rotate(angle, resample=Image.BICUBIC, expand=False)

    return img.convert("RGB"), (curr_zoom, curr_pan_x, curr_pan_y)

def draw_metadata_caption(img, frame_idx, total_frames, metadata, curr_zoom, curr_pan_x, curr_pan_y):
    """
    Draws a translucent diagnostic panel box in the top corner of the frame.
    Displays parameters like Seed, Steps, CFG, Zoom level, and Yaw/Pitch.
    """
    try:
        from PIL import ImageDraw, ImageFont
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
        
        # Determine multiline bounding box to size the background panel
        text_x = caption_x + 10
        text_y = caption_y + 6
        left, top, right, bottom = draw.multiline_textbbox((text_x, text_y), text, font=font)
        
        box_left = left - 10
        box_top = top - 6
        box_right = right + 10
        box_bottom = bottom + 6
        
        alpha_val = int(caption_bg_a * 255)
        bg_color = (caption_bg_r, caption_bg_g, caption_bg_b, alpha_val)
        
        draw.rectangle([box_left, box_top, box_right, box_bottom], fill=bg_color)
        draw.multiline_text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)
        img_rgba = Image.alpha_composite(img_rgba, overlay)
        return img_rgba.convert("RGB")
    except Exception as e:
        logit(f"Caption error: {e}")
        return img

def apply_border(img, border_path="static/border_portrait.png"):
    """
    Adds a cinematic border or frame asset overlay onto the saved frame.
    """
    if not os.path.exists(border_path):
        return img
    try:
        border = Image.open(border_path).convert("RGBA")
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
    Recursively scans base font folders to discover TTF/OTF files.
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
    Draws temporary subtitle overlays loaded dynamically by Flask endpoints.
    """
    if not text:
        return img
    try:
        from PIL import ImageDraw, ImageFont
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        
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
        
        left, top, right, bottom = draw.multiline_textbbox((text_x, text_y), text, font=font)
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

# ==============================================================================
# STATE FILE MANAGEMENT
# ==============================================================================
def save_state():
    """
    Writes all current settings, paths, and values into static/streamer/streamer.json.
    Allows session parameters to persist after server restarts.
    """
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
    """
    Loads saved settings from the JSON state file.
    Calculates the last frame saved on disk to resume generation seamlessly.
    """
    global current_frame, current_seed, current_prompt, negative_prompt, model_name, denoise_current
    global keyframes, injection_lines, frames_current, lora1_name, lora2_name, lora3_name
    global use_motion_zoom, zoom_start, zoom_end, pan_start_x, pan_end_x, pan_start_y, pan_end_y
    global default_steps, default_cfg, use_metadata_caption
    global logo_filename, logo_x, logo_y, logo_w, logo_h, logo_opacity
    global caption_font_size, caption_x, caption_y, caption_bg_r, caption_bg_g, caption_bg_b, caption_bg_a
    global temp_caption_font_size, temp_caption_x, temp_caption_y, temp_caption_bg_r, temp_caption_bg_g, temp_caption_bg_b, temp_caption_bg_a
    global original_starting_prompt

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

        # Check existing frame files in the folder to set the resume offset
        files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")])
        if files:
            last_file = files[-1]
            try:
                last_disk_frame = int(last_file.split("_")[1].split(".")[0])
                current_frame = last_disk_frame + 1
                logit(f"Resume: Detected frame {last_disk_frame} on disk. Starting at {current_frame}")
            except:
                pass

        if current_frame >= frames_current:
            logit(f"Session reached limit ({current_frame}/{frames_current}). Increase 'Frames' in UI to continue.")

        return True

    except Exception as e:
        logit("Load state error:", e)
        return False

# ==============================================================================
# COMFYUI API WORKFLOW COMPILER
# ==============================================================================
def get_workflow(active_s, active_p, neg_text, server_filename=None, frame_idx=0, active_d=0.35):
    """
    Compiles a python dictionary representing a ComfyUI JSON API workflow.
    Configures checkpoint, VAE, LoRA nodes, CLIP Text encoders, latent images,
    the KSampler, and image save endpoints dynamically.
    """
    wf = {
        "10": {"inputs": {"ckpt_name": model_name}, "class_type": "CheckpointLoaderSimple"},
        "20": {"inputs": {"vae_name": vae_name}, "class_type": "VAELoader"},
        "6": {"inputs": {"text": active_p, "clip": ["10", 1]}, "class_type": "CLIPTextEncode"},
        "7": {"inputs": {"text": neg_text, "clip": ["10", 1]}, "class_type": "CLIPTextEncode"}
    }
    lm, lc = ["10", 0], ["10", 1]
    
    # Dynamically chain LoRA loader nodes if they are set in parameters
    lora_names = [lora1_name, lora2_name, lora3_name]
    lora_strengths = [lora1_strength, lora2_strength, lora3_strength]
    for i, ln in enumerate(lora_names):
        if ln and ln != "None":
            nid = f"lora_{i}"
            str_val = lora_strengths[i]
            wf[nid] = {"inputs": {"lora_name": ln, "strength_model": str_val, "strength_clip": str_val, "model": lm, "clip": lc}, "class_type": "LoraLoader"}
            lm, lc = [nid, 0], [nid, 1]
    
    # Wire the outputs of the last LoRA loader into the positive/negative text encoders
    wf["6"]["inputs"]["clip"] = lc; wf["7"]["inputs"]["clip"] = lc

    # If an initialization image is provided, configure image loading and encoding
    if server_filename:
        wf["11"] = {"inputs": {"image": server_filename}, "class_type": "LoadImage"}
        wf["12"] = {"inputs": {"pixels": ["11", 0], "vae": ["20", 0]}, "class_type": "VAEEncode"}
        lat = ["12", 0]
    else:
        # Otherwise, start with an empty latent canvas
        wf["5"] = {"inputs": {"width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT, "batch_size": 1}, "class_type": "EmptyLatentImage"}
        lat = ["5", 0]

    # Configure the central KSampler node
    wf["3"] = {"inputs": {"seed": active_s, "steps": default_steps, "cfg": default_cfg, "sampler_name": "euler", "scheduler": "normal", "denoise": active_d if server_filename else 1.0, "model": lm, "positive": ["6", 0], "negative": ["7", 0], "latent_image": lat}, "class_type": "KSampler"}
    wf["8"] = {"inputs": {"samples": ["3", 0], "vae": ["20", 0]}, "class_type": "VAEDecode"}
    wf["9"] = {"inputs": {"filename_prefix": "epoch_", "images": ["8", 0]}, "class_type": "SaveImage"}
    return wf

# ==============================================================================
# OLLAMA VISION QUERY
# ==============================================================================
def query_llava(image_path, system_instruction):
    """
    Submits a base64 encoded image and a text prompt to a local Ollama model
    (like Moondream or LLaVA). Configured with a large 1000s timeout to allow
    inference on CPU-only machines without throwing exceptions.
    """
    import gc
    gc.collect()  # Force Python to release unused memory
    import base64
    try:
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        
        payload = {
            "model": visual_director_model,
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

# ==============================================================================
# KEYFRAME & PROMPT INTERPOLATION MATH
# ==============================================================================
def get_interpolated_prompt(frame_idx, base_prompt):
    """
    Identifies the keyframes immediately preceding and succeeding the current frame,
    and calculates a weighted blend of their text prompts based on distance.
    Returns a weighted prompt formatted for ComfyUI, e.g.:
    "(Prompt A:0.60), (Prompt B:0.40)"
    """
    if not keyframes:
        return base_prompt
        
    kf_nums = sorted([int(k) for k in keyframes.keys()])
    if not kf_nums:
        return base_prompt
        
    # If the current frame falls exactly on a keyframe, return its prompt
    if frame_idx in kf_nums:
        return keyframes[str(frame_idx)].get("prompt", base_prompt)
        
    # Search for the boundaries
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
        
    # Calculate interpolation weights
    total_dist = next_kf - prev_kf
    weight_next = (frame_idx - prev_kf) / total_dist
    weight_prev = 1.0 - weight_next
    
    # Strip any formatting brackets to prevent weight conflicts
    p_prev_clean = p_prev.replace("(", "").replace(")", "")
    p_next_clean = p_next.replace("(", "").replace(")", "")
    
    return f"({p_prev_clean}:{weight_prev:.2f}), ({p_next_clean}:{weight_next:.2f})"

# ==============================================================================
# MAIN RENDER LOOP THREAD
# ==============================================================================
def render_video(resume=False):
    """
    The heart of the streamer engine. Runs in a persistent background thread.
    Compiles setting workflows, pushes jobs to ComfyUI, waits for WebSocket
    completion signals, processes frames using PIL (Watermarks, Zoom, Panning),
    applies the AI Visual Director, and loops continuously.
    """
    global running, current_frame, paused, current_seed, teleport_image
    global caption_remaining, active_caption, roll_mode, current_prompt, original_starting_prompt
    if running: 
        return
    logit("ENGINE STARTED: Entering render loop.")
    
    if resume:
        # Load the configuration and offset frame index to resume from disk
        if not load_state():
            logit("Failed to load state, starting fresh.")
            current_frame = 0
            injection_lines.clear()
    else:
        # Start a brand new session, deleting old files to prevent frame bleeding
        current_frame = 0
        injection_lines.clear()
        original_starting_prompt = current_prompt
        for f in os.listdir(OUTPUT_DIR):
            if (f.startswith("frame_") and f.endswith(".png")) or (f.startswith("temp_clean_") and f.endswith(".png")) or (f.startswith("clean_") and f.endswith(".png")):
                try:
                    os.remove(os.path.join(OUTPUT_DIR, f))
                except Exception as e:
                    logit(f"Failed to remove old frame {f}: {e}")
    
    running = True
    last_server_filename = None

    # Upload the last frame on disk as the init image if resuming
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
            if not running: 
                break
            if paused:
                time.sleep(1)
                continue
            
            # Retrieve teleport image if armed from the UI
            with state_lock:
                if teleport_image:
                    last_server_filename = teleport_image
                    teleport_image = None
                    logit(f"Teleporting! Using external image for frame {current_frame}")

            # Advance the seed sequentially by 1
            seed = current_seed + current_frame
            
            # Apply prompt interpolation weights if configured
            if use_prompt_interpolation:
                prompt_base = get_interpolated_prompt(current_frame, current_prompt)
            else:
                prompt_base = current_prompt
                
            # Append keyword injections
            prompt = prompt_base + (", " + ", ".join(injection_lines[-MAX_LINES:]) if injection_lines else "")
            
            # Retrieve active keyframe overrides
            active_p, active_d, active_s = prompt, denoise_current, seed
            kf = keyframes.get(str(current_frame))
            if kf:
                kf_prompt = kf.get("prompt", active_p)
                active_p = kf_prompt
                # Overrides denoise parameter temporarily for this frame
                active_d = float(kf.get("denoise", active_d))
                active_s = seed + int(kf.get("seed_offset", 0))
                with state_lock:
                    current_prompt = kf_prompt
                    original_starting_prompt = kf_prompt
                logit(f"Keyframe {current_frame} applied. Redirecting Visual Director baseline to: '{kf_prompt}'")

            # Compile the workflow dictionary
            wf = get_workflow(active_s, active_p, negative_prompt, last_server_filename, current_frame, active_d)
            
            # Post the payload to ComfyUI's prompt queue
            try:
                resp = requests.post(f"{COMFY_URL}/prompt", json={"prompt": wf, "client_id": CLIENT_ID}, timeout=2000).json()
                pid = resp["prompt_id"]
                logit(f"Prompt sent (Frame {current_frame}). PID: {pid}. Prompt: '{active_p}'")
            except Exception as e:
                logit(f"Error sending prompt: {e}")
                time.sleep(5)
                continue
            
            image_info = None
            # Block the thread until ComfyUI WebSocket reports rendering is finished
            for i in range(3600): 
                if not running: 
                    break
                time.sleep(1)
                
                is_finished = False
                with state_lock:
                    if pid in comfy_finished_prompts:
                        is_finished = True
                        comfy_finished_prompts.remove(pid) # Clean up tracker
                
                # Fetch history as a polling fallback if WebSocket connection is dropped
                if is_finished or i % 10 == 0:
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
                # Retrieve the newly generated raw image from ComfyUI
                raw = requests.get(f"{COMFY_URL}/view", params=image_info, timeout=2000).content
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                
                meta = {
                    "seed": active_s,
                    "steps": default_steps,
                    "cfg": default_cfg,
                    "denoise": active_d if last_server_filename else 1.0
                }

                # 1. Apply zoom, panning, spaceship overlay, and roll
                img, zoom_data = apply_pil_zoom(
                    img, 
                    current_frame, 
                    frames_current,
                    overlay_png_path="static/logo.png",
                    overlay_opacity=0.8,
                    spaceship_path="static/spaceship.png"
                )
                
                # 2. Apply Feedback Loop Stabilization (VAEs blur and bleach pixels over cycles)
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
                
                # Save the stabilized clean copy to disk
                clean_path = os.path.join(OUTPUT_DIR, f"clean_{current_frame:03d}.png")
                feedback_img.save(clean_path)
                
                # Upload the clean image to ComfyUI for the next frame's start image
                with open(clean_path, "rb") as f:
                    up = requests.post(f"{COMFY_URL}/upload/image", files={"image": (f"f_{current_frame}.png", f)}, timeout=2000).json()
                    last_server_filename = up.get("name")
                
                # 3. Create Local Frame Archive with diagnostic overlays, borders, and subtitles
                local_img = img.copy()
                if use_metadata_caption:
                    local_img = draw_metadata_caption(local_img, current_frame, frames_current, meta, *zoom_data)
                
                local_img = apply_border(local_img)
                
                with state_lock:
                    if caption_remaining > 0:
                        local_img = draw_top_caption(local_img, active_caption, active_caption_font, active_caption_font_size)
                        caption_remaining -= 1
                        if caption_remaining == 0:
                            logit("Temporary caption finished.")

                local_path = os.path.join(OUTPUT_DIR, f"frame_{current_frame:03d}.png")
                local_img.save(local_path)
                
                # 4. Visual Director evolution
                if use_visual_director and (current_frame > 0) and (current_frame % visual_director_interval == 0):
                    logit(f"Visual Director: Evolving prompt based on frame {current_frame}...")
                    system_instruction = "Describe what you see in this image in one brief sentence."
                    img_desc = query_llava(clean_path, system_instruction)
                    if img_desc:
                        cleaned_desc = clean_desc(img_desc)
                        logit(f"Visual Director: Image description -> '{cleaned_desc}'")
                        
                        # Blend the starting theme with the vision model's description of what it sees
                        clean_start, start_loras = parse_prompt(original_starting_prompt)
                        if cleaned_desc:
                            new_prompt_core = f"{clean_start}, {cleaned_desc}"
                        else:
                            new_prompt_core = clean_start
                            
                        # Re-append LoRAs cleanly
                        all_loras = list(dict.fromkeys(start_loras))
                        loras_str = " ".join(all_loras)
                        new_prompt = f"{new_prompt_core} {loras_str}".strip()
                        
                        with state_lock:
                            current_prompt = new_prompt
                        logit(f"Visual Director: New base prompt set -> '{current_prompt}'")
                    else:
                        logit("Visual Director: Vision model returned no description, keeping previous prompt.")
                
                # Cleanup reference pointers to free memory
                del local_img 

                current_frame += 1
                save_state()
            except Exception as e:
                logit(f"Error processing frame {current_frame}: {e}")
                break

    except Exception as e:
        logit(f"Render loop error: {e}")
    finally:
        running = False

# ==============================================================================
# FLASK WEB APP & ENDPOINTS
# ==============================================================================
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
    """Renders the main FlaskArchitect dashboard UI."""
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
        temp_caption_bg_g=temp_caption_bg_g,
        temp_caption_bg_b=temp_caption_bg_b,
        temp_caption_bg_a=temp_caption_bg_a
    )

@app.route("/upload_logo", methods=["POST"])
def upload_logo():
    """Saves transparent PNG files uploaded from the watermark control panel."""
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
    """Saves drag coordinates, width, and opacity for watermarks to the JSON state."""
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
    logit(f"Logo position saved: {logo_filename} at ({logo_x}, {logo_y}) {logo_w}x{logo_h}")
    return jsonify({"status": "ok"})

def get_ollama_models():
    """Queries the local Ollama daemon for installed model tags."""
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
    """
    Submits a simple visual concept prompt to a text model (like Llama)
    to expand it with details like lighting, style, lens specs, etc.
    Immediately updates and saves state to prevent frontend overwrite races.
    """
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
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=1500)
        if r.status_code == 200:
            enhanced = r.json().get("response", "").strip()
            if enhanced.startswith('"') and enhanced.endswith('"'):
                enhanced = enhanced[1:-1]
            
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
    """Trigger endpoint to compile the current slide images into an MP4 video."""
    success = create_movie_from_frames()
    return jsonify({"status": "success" if success else "failed"})

@app.route("/control", methods=["POST"])
def control():
    """Handles Start, Resume, and Pause commands from the dashboard."""
    global current_prompt, paused
    d = request.json
    act = d.get("action")
    if act == "start":
        current_prompt = d.get("prompt", current_prompt)
        # Spin up render loop inside a background thread
        Thread(target=lambda: render_video(False), daemon=True).start()
    elif act == "resume":
        Thread(target=lambda: render_video(True), daemon=True).start()
    elif act == "pause": 
        paused = not paused
    return jsonify({"status": "ok"})

@app.route("/update_params", methods=["POST"])
def update_params():
    """Captures and saves all UI slider/checkbox adjustments to the server state."""
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
    model_name = d.get("model")
    negative_prompt = d.get("negative_prompt", negative_prompt)
    lora1_name = d.get("lora1")
    lora2_name = d.get("lora2")
    lora3_name = d.get("lora3", "None")
    current_seed = int(d.get("seed", current_seed))
    denoise_current = float(d.get("denoise", 0.30))
    frames_current = int(d.get("frames", 120))
    use_motion_zoom = bool(d.get("use_motion_zoom"))
    use_metadata_caption = bool(d.get("use_metadata_caption"))
    zoom_start = float(d.get("zoom_start", 1.0))
    zoom_end = float(d.get("zoom_end", 1.1))
    pan_start_x = float(d.get("pan_start_x", 0.5))
    pan_end_x = float(d.get("pan_end_x", 0.5))
    pan_start_y = float(d.get("pan_start_y", 0.5))
    pan_end_y = float(d.get("pan_end_y", 0.5))
    roll_mode = d.get("roll_mode", "none")
    default_steps = int(d.get("steps", 14))
    default_cfg = float(d.get("cfg", 5.4))
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
    
    save_state()
    return jsonify({"status": "ok"})

@app.route("/status")
def status_route():
    """Serves JSON object containing active server status parameters for UI syncing."""
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
    """Adds a keyframe checkpoint configuration to the memory array."""
    d = request.json
    f_idx = str(d.get("frame", 0))
    keyframes[f_idx] = {
        "prompt": d.get("prompt", "") or current_prompt, 
        "denoise": float(d.get("denoise", 0.5)), 
        "seed_offset": int(d.get("seed_offset", 0))
    }
    save_state()
    return jsonify({"status": "ok", "keyframes": keyframes})

@app.route("/latest_frame")
def latest():
    """Returns the latest compiled image frame from static/streamer/."""
    files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("frame_") and f.endswith(".png")])
    if not files: 
        return "none", 404
    return send_file(os.path.join(OUTPUT_DIR, files[-1]), max_age=0)

@app.route("/inject", methods=["POST"])
def inject():
    """Appends keywords dynamically into the render loop prompt prefixing."""
    t = request.json.get("text", "").strip()
    if t: 
        injection_lines.append(t)
        save_state()
    return jsonify({"status": "ok"})

@app.route("/set_caption", methods=["POST"])
def set_caption():
    """Arms a temporary subtitle overlay text for 5 frames."""
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
        logit(f"Caption set: {active_caption} (Font: {font}, Size: {temp_caption_font_size})")
    return jsonify({"status": "ok"})

@app.route("/teleport", methods=["POST"])
def teleport():
    """
    Saves an uploaded external image file, resizes it to base dimensions,
    and stages it to force-overwrite the next frame's input in the KSampler loop.
    """
    global teleport_image
    if "image" not in request.files:
        return jsonify({"status": "error", "message": "No image part"}), 400
    file = request.files["image"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "No selected file"}), 400
    
    try:
        img = Image.open(file).convert("RGB")
        if img.size != (DEFAULT_WIDTH, DEFAULT_HEIGHT):
            logit(f"Resizing teleport image from {img.size} to {(DEFAULT_WIDTH, DEFAULT_HEIGHT)}")
            img = img.resize((DEFAULT_WIDTH, DEFAULT_HEIGHT), Image.LANCZOS)
        
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        # Upload image directly into ComfyUI inputs folder
        files = {"image": ("teleport.png", buf)}
        resp = requests.post(f"{COMFY_URL}/upload/image", files=files, timeout=30).json()
        
        with state_lock:
            teleport_image = resp.get("name")
        
        logit(f"Teleport image set: {teleport_image}")
        return jsonify({"status": "ok", "filename": teleport_image})
    except Exception as e:
        logit(f"Run Teleport upload error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ==============================================================================
# SOUND STAGE SLIDESHOW COMPILER (MoviePy Integration)
# ==============================================================================
SHARED_PATH = os.path.join(BASE_DIR, "static", "streamer")
FRAME_PATH = os.path.join(BASE_DIR, "static", "assets", "transtalks.png")
UNIQUE = str(randint(100000, 999999))

@app.route("/media")
def media():
    """Renders the Audio slideshow compilation manager page."""
    files = os.listdir(SHARED_PATH)
    
    def get_mtime(filename):
        full_path = os.path.join(SHARED_PATH, filename)
        try:
            return os.path.getmtime(full_path)
        except:
            return 0

    # Filter files and sort newest first
    images = [f for f in files if f.lower().endswith((".jpg", ".png")) and not f.startswith("temp_clean_") and not f.startswith("clean_")]
    mp3s   = [f for f in files if f.lower().endswith(".mp3")]
    videos = [f for f in files if f.lower().endswith(".mp4")]

    images.sort(key=get_mtime, reverse=True)
    mp3s.sort(key=get_mtime, reverse=True)
    videos.sort(key=get_mtime, reverse=True)

    return render_template_string(HTML, images=images, mp3s=mp3s, videos=videos)

@app.route("/download_mp4", methods=["GET","POST"])
def download_mp4():
    """Allows uploading external MP4 videos to the workspace folder."""
    if request.method == "POST":
        if "file" not in request.files:
            return "No file part", 400
        file = request.files["file"]
        if file.filename == "":
            return "No selected file", 400
        filename = secure_filename(file.filename)
        out = os.path.join(SHARED_PATH, filename)
        file.save(out)
        return redirect("/")
    return """
    <h2>Upload MP4 from your computer or LAN</h2>
    <form method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".mp4"><br><br>
        <button>Upload</button>
    </form>
    """

def pad_audio(src, out, start_sec=None, end_sec=None):
    """
    Slices the input MP3 file between start_sec and end_sec,
    then adds 200ms of silence at the start and end of the segment.
    """
    audio = AudioSegment.from_mp3(src)
    
    start_ms = int(start_sec * 1000) if start_sec is not None else 0
    end_ms = int(end_sec * 1000) if end_sec is not None else len(audio)
    
    start_ms = max(0, min(start_ms, len(audio)))
    end_ms = max(start_ms, min(end_ms, len(audio)))
    
    trimmed_audio = audio[start_ms:end_ms]
    
    silence = AudioSegment.silent(duration=200)
    (silence + trimmed_audio + silence).export(out, format="mp3")

@app.route("/process_video", methods=["POST"])
def process_video():
    """
    Combines selected slideshow frames and audio track using moviepy.
    Adjusts video speed dynamically to match audio length or compiles
    staged frames into an even timed slideshow.
    """
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

    # CASE 1: SPEED STRETCH AN EXISTING RENDERED MP4 TO AUDIO LENGTH
    if selected_video:
        video_path = os.path.join(SHARED_PATH, selected_video)
        video = VideoFileClip(video_path)
        speed_factor = video.duration / audio_clip.duration
        new_video = video.fx(vfx.speedx, speed_factor)
        final = new_video.set_audio(audio_clip)

    # CASE 2: CONVERT INDIVIDUAL IMAGE SLIDESHOW FRAMES TO MATCH AUDIO DURATION
    else:
        clips = []
        duration = audio_clip.duration / len(selected_images)
        for img in selected_images:
            p = os.path.join(SHARED_PATH, img)
            clip = ImageClip(p).set_duration(duration)
            clips.append(clip)
        final = concatenate_videoclips(clips).set_audio(audio_clip)

    out = os.path.join(SHARED_PATH, f"{UNIQUE}_output.mp4")
    final.write_videofile(out, fps=24, codec="libx264", audio_codec="aac")
    return redirect(url_for("serve_video", filename=os.path.basename(out)))

@app.route("/video/<filename>")
def serve_video(filename):
    return send_file(os.path.join(SHARED_PATH, filename))

@app.route("/text_to_mp3", methods=["GET","POST"])
def text_to_mp3():
    """API wrapper to query local Kokoro TTS endpoint for speech MP3 generation."""
    if request.method == "POST":
        text = request.form.get("text")
        voice = request.form.get("voice")
        out = os.path.join(SHARED_PATH, text[:20].replace(" ","_")+".mp3")
        payload = {
            "model": "kokoro",
            "voice": voice,
            "input": text
        }
        r = requests.post("http://localhost:8880/v1/audio/speech", json=payload)
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

# ==============================================================================
# HTML / JS FRONTEND TEMPLATE (HTML_UI)
# ==============================================================================
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
        <label>Base Prompt</label><textarea id="prompt" rows="3">Highly detailed Centered Science fiction image of a star-gate with semi transparent space creatures similar to mythical sea monsters, swimming in space, surrounded with stars, planets, nebula, dust and space debris</textarea>
        
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
        // Shows real-time feedback notifications in the status panel
        function showFeedback(t){ const f=document.getElementById('feedback'); f.innerText=t; setTimeout(()=>f.innerText='',7000); }
        
        // Handles play/pause commands and autosaves parameters before starting a session
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

        // Handles dragging watermarks dynamically inside the preview box
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
            if (el && widthInput) {
                const newW = parseInt(widthInput.value) || 100;
                const parentRect = document.getElementById("overlay_logo_wrapper").getBoundingClientRect();
                if (parentRect.width > 0) {
                    const ratio = currentLogoH / currentLogoW;
                    currentLogoW = newW;
                    currentLogoH = Math.round(newW * ratio);
                    updateLogoOverlayDisplay();
                }
            }
        }

        function updateLogoOpacity(val) {
            currentLogoOpacity = parseFloat(val);
            const el = document.getElementById("draggable_logo");
            if (el) el.style.opacity = currentLogoOpacity;
        }

        function saveLogoPosition() {
            const el = document.getElementById("draggable_logo");
            const wrapper = document.getElementById("overlay_logo_wrapper");
            if (!el || !wrapper) return;
            const parentRect = wrapper.getBoundingClientRect();
            if (parentRect.width === 0 || parentRect.height === 0) return;
            
            const scaleX = currentFrameWidth / parentRect.width;
            const scaleY = currentFrameHeight / parentRect.height;
            const pixelX = Math.round(parseFloat(el.style.left) * scaleX);
            const pixelY = Math.round(parseFloat(el.style.top) * scaleY);
            const pixelW = Math.round(el.offsetWidth * scaleX);
            const pixelH = Math.round(el.offsetHeight * scaleY);
            
            fetch("/save_logo_position", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    logo_filename: currentLogoFilename,
                    x: pixelX,
                    y: pixelY,
                    w: pixelW,
                    h: pixelH,
                    opacity: currentLogoOpacity
                })
            })
            .then(r => r.json())
            .then(d => {
                if (d.status === "ok") {
                    showFeedback("Logo position saved!");
                } else {
                    alert("Failed to save logo position.");
                }
            });
        }

        function cancelLogoPlacement() {
            changeLogo("None");
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
                
                const widthInput = document.getElementById("logo_width_input");
                if (document.activeElement !== widthInput) {
                    widthInput.value = currentLogoW;
                }
            }
        }

        // Populates the dropdown lists with available models retrieved from Ollama
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
        });

        let initialSyncDone = false;

        // Periodic UI sync loop fetching current server state
        setInterval(()=>{
            fetch('/status').then(r=>r.json()).then(d=>{
                let statusMsg = d.running ? (d.paused ? "PAUSED" : "RENDERING") : "IDLE";
                
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
                
                // Synchronize DOM inputs if user is not actively editing and sync is not paused
                if (!pauseSync || !initialSyncDone) {
                    if (d.prompt !== undefined) safeSyncValue('prompt', d.prompt);
                    safeSyncChecked('use_caption', d.metadata_caption);
                    if (d.caption_font_size !== undefined) safeSyncValue('cap_font_size', d.caption_font_size);
                    if (d.caption_x !== undefined) safeSyncValue('cap_x', d.caption_x);
                    if (d.caption_y !== undefined) safeSyncValue('cap_y', d.caption_y);
                    if (d.caption_bg_r !== undefined) safeSyncValue('cap_bg_r', d.caption_bg_r);
                    if (d.caption_bg_g !== undefined) safeSyncValue('cap_bg_g', d.caption_bg_g);
                    if (d.caption_bg_b !== undefined) safeSyncValue('cap_bg_b', d.caption_bg_b);
                    if (d.caption_bg_a !== undefined) safeSyncValue('cap_bg_a', d.caption_bg_a);
                    
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
    </script>
</body></html>
"""

# ==============================================================================
# SOUND STAGE SLIDESHOW MEDIA LAYOUT (HTML)
# ==============================================================================
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

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    # Runs the Flask web application locally on Port 5002.
    # Note: debug=False and use_reloader=False prevents restarting background 
    # threads or duplicating socket connections.
    app.run(host="0.0.0.0", port=5004, debug=False, use_reloader=False)
