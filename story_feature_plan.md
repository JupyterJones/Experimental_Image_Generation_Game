# Implementation Plan: Cinematic Narrative & Audio Upgrades

We will implement four cinematic upgrades to [EpochDarkrooms.py](file:///home/jack/Desktop/epoch/EpochDarkrooms.py):
1. **"Story First" Preview Mode (Editable & Savable)**
2. **VHS / Lo-Fi Voice Inflection & Ambient Hum**
3. **Audio-Reactive Camera Shake (Visual Panic)**
4. **Smart Background Music (BGM) Auto-Ducking**

---

## 1. "Story First" Preview Mode (Editable & Savable)

### Backend changes
* Create a `/preview_story` POST endpoint. It reads the current user parameters (`frames_current`, `original_starting_prompt`, `keyframes`) and generates the story script entries *before* images are generated. It calculates the emotional states (`tension`, `sanity`, `inflection_hint`, `speed`) and queries the text LLM to write the diary script.
* Create a `/save_diary` POST endpoint that receives the user-edited diary list and overwrites `static/darkrooms/backrooms_diary.json`.
* Update `/generate_story`: It will check if `backrooms_diary.json` already exists and matches the expected frame count. If so, it preserves the existing story text (keeping the user's edits!) while updating the visual descriptions from Moondream and running Kokoro TTS.

### Frontend changes
* Add a **"Preview Story Script"** button in the UI.
* Clicking it calls `/preview_story` and displays each frame's script in an editable text box.
* Add a **"Save Story Script"** button that POSTs the text boxes back to `/save_diary`.

---

## 2. VHS / Lo-Fi Voice Inflection & Ambient Hum

Inside the TTS loop in `/generate_story`:
* Load the generated `narration_XXX.wav` file using `pydub.AudioSegment`.
* Apply a low-pass filter (cut off high frequencies at `2800 Hz`) and a high-pass filter (cut off low rumblings at `150 Hz`) to simulate the bandwidth limitation of a tape recorder.
* Dynamically generate a quiet 60Hz electrical ground hum (simulating fluorescent lights) and a soft tape hiss (white/brown noise) and overlay them beneath the voice track.
* Save the output back to disk.

---

## 3. Audio-Reactive Camera Shake (Visual Panic)

Inside [/compile_story_movie](file:///home/jack/Desktop/epoch/EpochDarkrooms.py#L4178-L4210):
* Read the `tension` level associated with each frame from `backrooms_diary.json`.
* If the frame's `tension > 7.0` (panic), apply a fast back-and-forth camera rotation using MoviePy's time-dependent rotation feature:
  `img_clip = img_clip.rotate(lambda t: shake_amp * math.sin(t * 25), resample="bicubic", expand=False)`
* If the `tension` is between `4.0` and `7.0` (unease), apply a slower, nervous sway.

---

## 4. Smart Background Music (BGM) Auto-Ducking

### UI update
* Scan `static/darkrooms/` for BGM candidates (`.mp3` or `.wav` files not starting with `narration_`).
* Add a dropdown in the UI: **"Background Ambient Track (BGM)"**.
* When the user clicks "Compile Synchronized Story Video," the selected BGM filename is passed as a parameter: `/compile_story_movie?bgm=filename`.

### Backend update
* If a BGM track is selected:
  * Load it via MoviePy's `AudioFileClip`.
  * For each segment, extract a sub-slice of the BGM corresponding to that segment's duration and **duck its volume** (multiply by `0.12` to make it a quiet background drone).
  * Combine the narration and ducked BGM using `CompositeAudioClip([audio_clip, bgm_slice])` and apply it to the video segment.
