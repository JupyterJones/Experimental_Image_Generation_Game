# EpochStreamer Flight Control Systems

This document explains how the cinematic motion system works in the `EpochStreamer` engine, covering the user controls (Zooms, Pans, Rolls) and their underlying technical implementation.

## 1. The Motion Zoom System

The system simulates a camera moving through space by performing mathematical crops and resizes on every frame.

### How it works in UI:
*   **Zoom Start/End**: Sets the magnification. `1.0` is the original size. `1.1` is a 10% zoom.
*   **Pan X/Y S/E**: Sets the focus point of the "camera". `0.5` is perfectly centered. 
    *   **X**: 0.0 is far left, 1.0 is far right.
    *   **Y**: 0.0 is the top, 1.0 is the bottom.

### How it works in Code (`apply_pil_zoom`):
1.  **Interpolation**: The engine calculates the "Current Progress" (Current Frame / Total Frames).
2.  **Calculation**:
    *   `curr_zoom = zoom_start + (zoom_end - zoom_start) * progress`
    *   `curr_pan_x = pan_start_x + (pan_end_x - pan_start_x) * progress`
3.  **Cropping**: It calculates a crop rectangle based on these values and "cuts" that piece out of the high-res generated image.
4.  **Resizing**: It stretches that small piece back up to the full `340x512` resolution using `Image.LANCZOS` for maximum sharpness.

---

## 2. The Ship Roll (Banking)

The Roll adds a sense of weight and maneuvering to the space flight.

### How it works in UI:
*   **Level (Stop)**: No rotation.
*   **Roll Right**: Slow clockwise rotation.
*   **Roll Left**: Slow counter-clockwise rotation.

### How it works in Code:
*   **Incremental Rotation**: Inside the render loop, the engine multiplies the `current_frame` by a fixed step of `0.01 degrees`.
*   **Formula**: `angle = frame_index * 0.01 * direction`
*   **Layering**: This rotation is applied to the *Space View* ONLY. Because it happens before the Pilot Border is added, the cockpit stays level while the universe tilts.
*   **Safety**: Because the zoom is set to `1.05` or higher, the corners of the rotated space image stay hidden behind the frame, preventing black triangles.

---

## 3. The Local Overlay "Pilot" System

This is the secret to the engine's immersion.

### How it works in UI:
*   **static/border.png**: This is your "cockpit" or "window" frame.

### How it works in Code:
1.  **The Feedback Loop**: The engine sends a "clean" version of the zoom/pan/roll image to the AI server. This keeps the AI's "memory" sharp.
2.  **The Local Save**: ONLY when saving the image to your disk does it apply the Pilot Border (`Image.alpha_composite`).
3.  **Layering Order**:
    *   Bottom: AI Generated Space View (Zoomed, Panned, and Rolled).
    *   Middle: Periodic Logo Overlay (Optional).
    *   Top: Pilot Border (`static/border.png`).
    *   Overlay: Metadata Captions or Temporary Top Captions.

---

## 4. Director Tools (Inject & Caption)

*   **Inject**: Adds a keyword to the *AI's prompt* for the next frame. This changes what the AI "sees" outside the window.
*   **Temporary Caption**: Overlays a white-on-black bar at the top of the image for exactly 5 frames. This is handled by a countdown variable (`caption_remaining`) that stops itself automatically once it hits zero.
