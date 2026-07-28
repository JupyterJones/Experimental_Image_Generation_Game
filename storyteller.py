import os
import sys
import json
import base64
import re
import requests

OLLAMA_URL = "http://localhost:11434"
KOKORO_URL = "http://localhost:8880/v1/audio/speech"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DARKROOMS_DIR = os.path.join(BASE_DIR, "static", "darkrooms")
CACHE_FILE = os.path.join(DARKROOMS_DIR, "descriptions_cache.json")
DIARY_FILE = os.path.join(DARKROOMS_DIR, "backrooms_diary.json")
DIARY_TXT = os.path.join(DARKROOMS_DIR, "backrooms_diary.txt")

def get_available_models():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=30)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except:
        pass
    return []

def query_vision(image_path, model, prompt):
    try:
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [img_data],
            "stream": False,
            "options": {
                "temperature": 0.4,
                "max_tokens": 100
            }
        }
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=300)
        if r.status_code == 200:
            return r.json().get("response", "").strip()
    except Exception as e:
        print(f"Error querying vision model {model}: {e}")
    return None

def query_text(model, prompt):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.7,
            "max_tokens": 2048
        }
    }
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=600)
        if r.status_code == 200:
            return r.json().get("response", "").strip()
    except Exception as e:
        print(f"Error querying text model {model}: {e}")
    return None

def generate_tts(text, voice, output_path):
    payload = {
        "model": "kokoro",
        "voice": voice,
        "input": text
    }
    try:
        r = requests.post(KOKORO_URL, json=payload, timeout=60)
        if r.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(r.content)
            return True
        else:
            print(f"TTS failed with status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Error connecting to TTS: {e}")
    return False

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision", type=str, default="LlaVa:latest", help="Vision model to use")
    parser.add_argument("--text", type=str, default="llama3.2:3b", help="Text model to use")
    args = parser.parse_args()

    print("==================================================")
    print("EPOCH Storyteller & Narration Pipeline")
    print("==================================================")

    if not os.path.exists(DARKROOMS_DIR):
        print(f"Error: Directory {DARKROOMS_DIR} does not exist.")
        return

    # Find clean frames (without border)
    files = sorted([
        f for f in os.listdir(DARKROOMS_DIR) 
        if f.startswith("clean_") and not f.startswith("clean_base_") and f.endswith(".png")
    ])
    
    if not files:
        # Fallback to clean base frames
        files = sorted([
            f for f in os.listdir(DARKROOMS_DIR) 
            if f.startswith("clean_base_") and f.endswith(".png")
        ])
        
    if not files:
        # Fallback to standard frame files if clean base frames not found
        files = sorted([
            f for f in os.listdir(DARKROOMS_DIR) 
            if f.startswith("frame_") and f.endswith(".png")
        ])
        
    if not files:
        print("No frames found in static/darkrooms/ to describe.")
        return
        
    print(f"Detected {len(files)} frames to process.")

    # Determine models
    available = get_available_models()
    print("Available Ollama models:", available)
    
    vision_model = args.vision
    if vision_model not in available:
        # Fallback to first available vision model or llava if present
        v_candidates = [m for m in available if "llava" in m or "moondream" in m]
        if v_candidates:
            vision_model = v_candidates[0]
        else:
            print(f"Warning: '{vision_model}' is not in available models. Attempting to use it anyway.")
            
    text_model = args.text
    if text_model not in available:
        preferred_text = ["llama3.2:3b", "deepseek-r1:1.5b", "dolphin3:8b", "llama2-uncensored:7b"]
        for p in preferred_text:
            if p in available:
                text_model = p
                break
        else:
            t_candidates = [m for m in available if any(x in m.lower() for x in ["llama", "dolphin", "deepseek", "qwen", "mistral"])]
            if t_candidates:
                text_model = t_candidates[0]
            else:
                print(f"Warning: '{text_model}' is not in available models. Attempting to use it anyway.")

    print(f"Using Vision Model: {vision_model}")
    print(f"Using Text Model: {text_model}")

    # Load cache
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cache = json.load(f)
        except:
            pass

    # Step 1: Describe frames
    print("\n--- Step 1: Generating Image Descriptions ---")
    descriptions = []
    
    for idx, filename in enumerate(files):
        # Extract frame index
        match = re.search(r'_(\d+)\.png$', filename)
        frame_num = int(match.group(1)) if match else idx
        
        file_path = os.path.join(DARKROOMS_DIR, filename)
        
        # Check cache
        if filename in cache:
            desc = cache[filename]
            print(f"Frame {frame_num} ({filename}): [Cached] {desc}")
        else:
            print(f"Frame {frame_num} ({filename}): Querying AI...", end="", flush=True)
            desc = query_vision(file_path, vision_model, "Describe this liminal space/backrooms image in one short sentence.")
            if desc:
                # Clean prompt
                desc = desc.replace('"', '').replace("'", "").strip()
                cache[filename] = desc
                # Save cache immediately
                with open(CACHE_FILE, "w") as f:
                    json.dump(cache, f, indent=2)
                print(f"\rFrame {frame_num} ({filename}): {desc}")
            else:
                desc = "An empty liminal corridor."
                print(f"\rFrame {frame_num} ({filename}): [Fallback] {desc}")
                
        descriptions.append({
            "frame": frame_num,
            "file": filename,
            "description": desc
        })

    # Step 2: Write Story
    print("\n--- Step 2: Generating Cohesive Horror Diary ---")
    
    desc_str = ""
    for item in descriptions:
        desc_str += f"Frame {item['frame']}: {item['description']}\n"
        
    prompt = (
        "You are an expert psychological horror writer. You will write a survival diary of a traveler lost in the Backrooms. "
        "Below is a sequence of visual descriptions representing the rooms/corridors the traveler wanders into frame-by-frame.\n\n"
        "Your task is to write a cohesive, continuous story diary matching this visual sequence. "
        f"You MUST write exactly {len(descriptions)} diary entries, one for each frame description. "
        "Keep the tone extremely tense, dread-filled, whispering, and psychological. Keep each entry brief (15-30 words).\n\n"
        "Format your output strictly using '[ENTRY]' as a separator before each frame entry, like this:\n"
        "[ENTRY] Entry 0 text...\n"
        "[ENTRY] Entry 1 text...\n\n"
        "Do not output any introductory or concluding text. Output ONLY the entry texts with their [ENTRY] separators.\n\n"
        f"Visual Sequence:\n{desc_str}"
    )

    story_raw = query_text(text_model, prompt)
    if not story_raw:
        print("Failed to generate story.")
        return

    # Parse entries
    entries = [e.strip() for e in story_raw.split("[ENTRY]") if e.strip()]
    
    # Pad or slice to match exactly descriptions length
    if len(entries) < len(descriptions):
        print(f"Warning: LLM generated {len(entries)} entries instead of {len(descriptions)}. Padding.")
        while len(entries) < len(descriptions):
            idx = len(entries)
            entries.append(f"Wandering deeper. The visual match points to: {descriptions[idx]['description']}.")
    elif len(entries) > len(descriptions):
        print(f"Warning: LLM generated {len(entries)} entries instead of {len(descriptions)}. Slicing.")
        entries = entries[:len(descriptions)]

    # Merge diary with descriptions
    diary_data = []
    diary_txt_lines = []
    for idx, item in enumerate(descriptions):
        item["story"] = entries[idx]
        diary_data.append(item)
        diary_txt_lines.append(f"Frame {item['frame']} [{item['file']}]:\n  Visual: {item['description']}\n  Diary: {item['story']}\n")

    # Save outputs
    with open(DIARY_FILE, "w") as f:
        json.dump(diary_data, f, indent=2)
        
    with open(DIARY_TXT, "w") as f:
        f.write("\n".join(diary_txt_lines))

    print(f"Saved story diary to {DIARY_FILE} and {DIARY_TXT}.")
    
    for item in diary_data:
        print(f"\nFrame {item['frame']}:")
        print(f"  Visual: {item['description']}")
        print(f"  Diary:  {item['story']}")

    # Step 3: Generate TTS Narrations
    print("\n--- Step 3: Generating Kokoro TTS Voiceovers ---")
    voice = "af_bella" # default female voice
    
    # Let user choose voice if running interactively
    print(f"Default TTS voice: {voice}")
    
    tts_count = 0
    for item in diary_data:
        frame_num = item["frame"]
        text = item["story"]
        output_name = f"narration_{frame_num:03d}.mp3"
        output_path = os.path.join(DARKROOMS_DIR, output_name)
        
        print(f"Synthesizing Frame {frame_num} narration -> {output_name}...", end="", flush=True)
        success = generate_tts(text, voice, output_path)
        if success:
            print(" Done.")
            tts_count += 1
        else:
            print(" Failed.")
            
    print(f"\nCompleted! Generated {tts_count} narration audio tracks in static/darkrooms/.")

if __name__ == "__main__":
    main()
