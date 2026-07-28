Welcome... to the Epoch Streamer. This is a real-time, interactive, image-to-image feedback loop system. It is built on top of ComfyUI—and powered by local Ollama language models. 

The core engine is designed to generate continuous, evolving video streams. How? By recursively feeding each generated frame back into ComfyUI as the starting image for the next cycle. Along the way, the system applies zoom, pan, and ship roll movements; it overlays dynamic text captions, stamps logo watermarks, and applies closed-loop visual feedback.

Let us look at the system architecture. 

The stream connects multiple local servers. First, the Web UI browser communicates with the Flask server, running on port 5002. Next, the Flask server coordinates parameters, saves current states, and delivers workflow payloads to ComfyUI, on port 5001. When ComfyUI finishes a frame, it returns the raw image to Flask. The Python engine then processes it—applying color, contrast, and sharpness adjustments. Finally, it uploads the stabilized frame back to ComfyUI. Every few frames, a copy is sent to Ollama, on port 11434. Here, a vision language model analyzes the scene—evolving the running prompt to morph the video content.

Now, let us explore the features.

First: The Visual Director. 
This is the vision loop. Every fifteen or twenty-five frames, the engine intercepts the clean generated image. It triggers a memory cleanup, base64 encodes the file, and queries Ollama using a model like Moondream. The model describes the image in one sentence, then invents a new Stable Diffusion prompt—under fifty words—to evolve the scene. This becomes the new base prompt.

Second: The Text Prompt Enhancer. 
Before rendering, you can expand simple prompts into rich, detailed descriptions. The UI loads your installed models—like Llama 3.2 or Dolphin 3. When you click Enhance, the model automatically adds atmospheric lighting, camera details, and high-fidelity modifiers.

Third: The Feedback Loop Stabilizer. 
In long loops, images suffer from VAE degradation—losing contrast, drifting towards muddy whites, or fading away. To prevent this, the script processes each frame before upload. It applies three enhancements: a color saturation boost, a contrast boost, and a sharpness boost to preserve clean edges.

Fourth: Interactive Logo Overlay. 
You can place a logo anywhere on the video. Simply upload a transparent PNG, select it in the UI, and drag it into position. You can set the size and opacity. When saved, the Python engine uses alpha compositing to bake the watermark directly into the output frames.

Fifth: Dynamic Captioning. 
Inject dialogue or titles on the fly! The script scans the fonts folder for TrueType or OpenType files. When armed, the caption draws directly onto the stream for five frames, featuring a clean drop shadow.

Sixth: Smooth Video Interpolation. 
Normally, the output compiles at a stepped five frames per second. But, by enabling video interpolation, FFmpeg calculates bidirectional motion vectors—interpolating intermediate frames to compile a butter-smooth, twenty-four frames per second movie.

Seventh: Parameter Edit Protection. 
To prevent status updates from resetting input fields while you type, the UI features a manual edit mode. Checking this box pauses parameter updates. Syncing is also paused automatically when the engine is idle or paused, and all parameters autosave when starting, resuming, or pausing a session.

Finally, a note on CPU optimizations. 
Because Stable Diffusion and Ollama run together on CPU, memory is precious. Always use Moondream for the visual director. LLaVA is simply too heavy—using over four gigabytes of RAM and causing severe lag. Moondream, at under nine hundred megabytes, runs five times faster. 

This concludes the system manual.
