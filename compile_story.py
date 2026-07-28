import os
import re
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DARKROOMS_DIR = os.path.join(BASE_DIR, "static", "darkrooms")
OUTPUT_FILE = os.path.join(DARKROOMS_DIR, "story_production.mp4")

def main():
    print("==================================================")
    print("   EPOCH Story Video Compiler (Frame + Audio)     ")
    print("==================================================")

    # Find all narration MP3 files
    mp3_files = sorted([f for f in os.listdir(DARKROOMS_DIR) if f.startswith("narration_") and f.endswith(".mp3")])
    if not mp3_files:
        print("No narration MP3 files found in static/darkrooms/.")
        return

    print(f"Found {len(mp3_files)} narration MP3 files.")

    clips = []
    for mp3_name in mp3_files:
        # Extract index
        match = re.search(r'narration_(\d+)\.mp3$', mp3_name)
        if not match:
            continue
        idx = int(match.group(1))

        # Check for corresponding frame image (prefer clean_base, clean, or frame)
        img_candidates = [
            f"clean_base_{idx:03d}.png",
            f"frame_{idx:03d}.png",
            f"clean_{idx:03d}.png"
        ]
        
        img_path = None
        for cand in img_candidates:
            cand_path = os.path.join(DARKROOMS_DIR, cand)
            if os.path.exists(cand_path):
                img_path = cand_path
                break
                
        if not img_path:
            print(f"Warning: No frame image found for index {idx} (checked {img_candidates})")
            continue

        print(f"Matching index {idx:03d}: Image={os.path.basename(img_path)} <-> Audio={mp3_name}")

        try:
            # Create audio clip
            audio_clip = AudioFileClip(os.path.join(DARKROOMS_DIR, mp3_name))
            # Create image clip with same duration as audio
            img_clip = ImageClip(img_path).set_duration(audio_clip.duration)
            # Combine image and audio
            video_segment = img_clip.set_audio(audio_clip)
            clips.append(video_segment)
        except Exception as e:
            print(f"Error processing index {idx:03d}: {e}")

    if not clips:
        print("No valid video segments could be created.")
        return

    print(f"\nConcatenating {len(clips)} video segments...")
    try:
        final_video = concatenate_videoclips(clips, method="compose")
        print(f"Writing final video to {OUTPUT_FILE}...")
        final_video.write_videofile(
            OUTPUT_FILE,
            fps=24,
            codec="libx264",
            audio_codec="aac"
        )
        print("\nSuccess! Story video successfully created.")
        print(f"File saved to: {OUTPUT_FILE}")
    except Exception as e:
        print(f"Error during video compilation: {e}")

if __name__ == "__main__":
    main()
