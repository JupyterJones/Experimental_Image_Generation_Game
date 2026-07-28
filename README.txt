Epoch Streamer Suite Version 2 Narration Script.

Welcome to the Epoch Streamer Suite Version 2, an interactive, real-time image-to-image feedback loop system built on top of ComfyUI and local Ollama language models. This system is designed to generate continuous, evolving video streams from an initial text prompt. It recursively feeds each generated frame back into ComfyUI as the starting image for the next frame, applying zoom and pan movements, dynamic text captions, logo overlays, and closed-loop visual feedback.

Here is how the system works under the hood.

First, let us look at the System Architecture.
The Epoch Streamer connects multiple local servers to form a closed-loop creative cycle. The Web UI browser interface communicates with the Flask server running on port 5002. The Flask server coordinates the parameters, saves states, and sends workflow payloads to ComfyUI running on port 5001. Once ComfyUI finishes rendering a frame, it returns the raw image to the Flask server. The Python processing engine then applies post-processing, stabilization, and overlays. Finally, it uploads the modified frame back to ComfyUI for the next render cycle. Additionally, every few frames, the image is sent to Ollama running on port 11434, where a vision language model analyzes the scene and generates an updated base prompt to morph the video content.

Second, let us explore the Core Features.

Feature number one: The Autonomous Visual Director.
The Visual Director enables the stream to see what it is generating and redirect its prompt based on visual feedback. Every fifteen or twenty-five frames, the Python engine intercepts the generated image before overlays are stamped on top. It triggers a memory-clearing garbage collection to free up RAM, base64 encodes the image, and sends it to Ollama using a vision model like Moondream. The model describes what it sees in one sentence, and writes a new Stable Diffusion prompt under fifty words to evolve the scene slightly. The engine then sets this as the new base prompt.

Feature number two: Ollama Text Prompt Enhancer.
Before rendering, you can expand a simple prompt into a rich, detailed description. The frontend queries Ollama to load all installed models like Llama 3.2 or Dolphin 3 into a dropdown. When you click Enhance, the Flask server instructs the selected model to expand your prompt with atmospheric lighting, camera details, and high-fidelity modifiers.

Feature number three: Feedback Loop Stabilizer.
In long runs, image-to-image feedback loops experience VAE encoding degradation. Without correction, images lose contrast and drift towards white or muddy colors after thirty to fifty frames. To prevent this, the script processes the frame through PIL ImageEnhance modules before uploading. It applies a color saturation boost to combat natural color loss, a contrast boost to maintain deep blacks and bright highlights, and a sharpness boost to preserve edges.

Feature number four: Interactive Drag and Drop Logo Composition.
You can place a watermarked brand or logo anywhere on the generated video stream. Upload a transparent PNG logo, select it in the UI, and drag it anywhere on the preview frame. You can resize it via numerical inputs and adjust its opacity. Clicking Save Logo Position posts coordinates to the server, and the Python engine uses alpha compositing to bake the logo into the final disk-saved frames.

Feature number five: Dynamic Fonts and Temporary Captioning.
You can inject titles or dialogue lines on the fly. The Python script scans the fonts folder for any TrueType or OpenType fonts. When you click Insert Caption, the server stores the text, font, and size, and displays the caption on the stream for the next five frames with a clean drop shadow.

Feature number six: Smooth Video Interpolation.
By default, the generated frame sequence compiles at five frames per second, creating a stop-motion look. Version 2 includes an optional RIFE-style temporal interpolator via FFmpeg. When enabled, FFmpeg estimates motion vectors bidirectionally and generates intermediate optical-flow frames, upscaling the sequence into a smooth twenty-four frames per second video.

Feature number seven: Parameter Edit Protection.
To prevent the rapid status polling loop from overwriting input fields while you type, the UI implements a smart sync-block. Checking Pause Parameter Syncing pauses all input updates from the server. Additionally, the system automatically pauses syncing whenever the engine is idle or paused. When you trigger actions like starting, resuming, or pausing, the UI automatically autosaves your edits to the server.

Third, let us cover CPU and RAM Optimization.
Because Stable Diffusion and Ollama run concurrently on CPU, memory management is critical. Always choose Moondream for the Visual Director instead of LLaVA. LLaVA consumes over four gigabytes of RAM and causes severe system lag, whereas Moondream is under nine hundred megabytes and runs five times faster on CPU. The resolution is also locked to three hundred thirty-six by five hundred twelve to ensure fast CPU generations.
