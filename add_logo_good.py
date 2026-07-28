#!/usr/bin/env python3
"""
Flask app: upload base images and transparent PNG overlays,
drag & drop overlay on top of an image, save positions, re-edit later.

Usage:
    pip install flask pillow icecream
    python app.py

Files/folders created relative to the script:
    ./uploads/         - base images uploaded by user
    ./overlays/        - transparent PNG overlays (logos)
    ./composed/        - final composed images
    overlays_meta.json - metadata with saved overlay positions
"""

import os
import io
import uuid
import json
from datetime import datetime
from PIL import Image
from flask import Flask, request, redirect, url_for, send_from_directory, render_template_string, jsonify
from icecream import ic

# ---------------------------
# Configuration (change as needed)
# ---------------------------
UPLOAD_FOLDER = "./uploads"
OVERLAY_FOLDER = "./overlays"
COMPOSED_FOLDER = "./composed"
META_FILE = "./overlays_meta.json"
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg"}
ALLOWED_OVERLAY_EXT = {"png"}  # overlays should be transparent PNGs

# Ensure directories exist
for d in (UPLOAD_FOLDER, OVERLAY_FOLDER, COMPOSED_FOLDER):
    os.makedirs(d, exist_ok=True)

# Load or initialize metadata
if os.path.exists(META_FILE):
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            overlays_meta = json.load(f)
            ic("Loaded metadata entries:", len(overlays_meta))
    except Exception as e:
        ic("Error reading meta file, starting fresh:", e)
        overlays_meta = {}
else:
    overlays_meta = {}
    ic("Meta file not found, starting with empty metadata")

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["OVERLAY_FOLDER"] = OVERLAY_FOLDER
app.config["COMPOSED_FOLDER"] = COMPOSED_FOLDER

# ---------------------------
# Helpers
# ---------------------------
def allowed_file(filename, allowed_set):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_set

def save_meta():
    """Persist overlays_meta to disk."""
    try:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(overlays_meta, f, indent=2)
        ic("Saved metadata to", META_FILE)
    except Exception as e:
        ic("Failed to save metadata:", e)

def compose_images(base_path, overlay_path, pos_x, pos_y, overlay_display_w=None, overlay_display_h=None):
    """
    Compose overlay PNG onto base image using Pillow.
    pos_x, pos_y are pixel coordinates relative to top-left of base image.
    If overlay_display_w/h passed, the overlay image will be resized to those display sizes
    before compositing (maintains alpha).
    Returns the path of saved composed image.
    """
    ic(f"Composing: base={base_path}, overlay={overlay_path}, x={pos_x}, y={pos_y}, w={overlay_display_w}, h={overlay_display_h}")
    base = Image.open(base_path).convert("RGBA")
    overlay = Image.open(overlay_path).convert("RGBA")

    # Optionally resize overlay to requested display size
    if overlay_display_w and overlay_display_h:
        try:
            overlay = overlay.resize((int(overlay_display_w), int(overlay_display_h)), resample=Image.LANCZOS)
            ic("Resized overlay to", overlay.size)
        except Exception as e:
            ic("Error resizing overlay:", e)

    # Create a new image with same size as base and paste overlay
    composed = Image.new("RGBA", base.size)
    composed.paste(base, (0, 0))

    # If overlay extends beyond base boundaries, it will be cropped automatically
    composed.paste(overlay, (int(pos_x), int(pos_y)), overlay)

    # Convert to RGB for saving as JPG (or keep PNG if transparency desired)
    out_filename = f"{uuid.uuid4().hex}_composed.png"
    out_path = os.path.join(COMPOSED_FOLDER, out_filename)
    composed.save(out_path)
    ic("Saved composed image to", out_path)
    return out_path

# ---------------------------
# Routes
# ---------------------------

# Home: list base images and overlays, upload forms
@app.route("/")
def index():
    base_images = sorted(os.listdir(UPLOAD_FOLDER))
    overlays = sorted(os.listdir(OVERLAY_FOLDER))
    ic("Index requested. base_images:", len(base_images), "overlays:", len(overlays))
    template = """
    <!doctype html>
    <html>
    <head>
      <title>Image Overlay Editor</title>
      <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .column { float: left; width: 48%; margin-right: 2%; }
        .box { border: 1px solid #ddd; padding: 12px; margin-bottom: 12px; border-radius: 6px; background: #fafafa; }
        .thumb { max-width: 100%; height: auto; display:block; margin-bottom:8px; }
        .clear { clear:both; }
        .small { font-size:0.9rem; color:#555; }
      </style>
    </head>
    <body>
      <h2>Image Overlay Editor</h2>
      <div class="column">
        <div class="box">
          <h3>Upload Base Image</h3>
          <p class="small">Allowed: jpg, jpeg, png</p>
          <form method="post" action="{{ url_for('upload_base') }}" enctype="multipart/form-data">
            <input type="file" name="base_image" accept="image/*" required>
            <br><br>
            <button type="submit">Upload Base Image</button>
          </form>
        </div>

        <div class="box">
          <h3>Uploaded Base Images</h3>
          {% if base_images %}
            <ul>
            {% for f in base_images %}
              <li>
                <img src="{{ url_for('uploaded_file', filename=f) }}" class="thumb" />
                <b>{{ f }}</b>
                <br>
                <a href="{{ url_for('edit_image', filename=f) }}">Edit / Add Overlay</a>
                {% if f in overlays_meta %}
                  &nbsp;|&nbsp;<a href="{{ url_for('view_metadata', filename=f) }}">View saved overlays</a>
                {% endif %}
              </li>
            {% endfor %}
            </ul>
          {% else %}
            <p>No base images uploaded yet.</p>
          {% endif %}
        </div>
      </div>

      <div class="column">
        <div class="box">
          <h3>Upload Overlay (transparent PNG)</h3>
          <p class="small">Allowed: png only (transparent preferred)</p>
          <form method="post" action="{{ url_for('upload_overlay') }}" enctype="multipart/form-data">
            <input type="file" name="overlay_image" accept="image/png" required>
            <br><br>
            <button type="submit">Upload Overlay</button>
          </form>
        </div>

        <div class="box">
          <h3>Available Overlays</h3>
          {% if overlays %}
            <ul>
            {% for o in overlays %}
              <li>
                <img src="{{ url_for('overlay_file', filename=o) }}" style="max-width:150px; display:block;" />
                <b>{{ o }}</b>
              </li>
            {% endfor %}
            </ul>
          {% else %}
            <p>No overlays uploaded.</p>
          {% endif %}
        </div>

        <div class="box">
          <h3>Composed Images</h3>
          <p class="small">Saved compositions are here.</p>
          {% set composed = namespace(list=[]) %}
          {% for c in os.listdir(compose_folder) %}
            {% set composed.list = composed.list + [c] %}
          {% endfor %}
          {% if composed.list %}
            <ul>
            {% for c in composed.list %}
              <li><a href="{{ url_for('composed_file', filename=c) }}">{{ c }}</a></li>
            {% endfor %}
            </ul>
          {% else %}
            <p>No composed images yet.</p>
          {% endif %}
        </div>
      </div>

      <div class="clear"></div>
      <hr>
      <p class="small">Tips: Click "Edit / Add Overlay" on a base image. On the editor page you can pick an overlay from the right panel and drag it over the image. Click "Save Position" to persist and produce a composed image.</p>
    </body>
    </html>
    """
    return render_template_string(template, base_images=base_images, overlays=overlays, overlays_meta=overlays_meta, os=os, compose_folder=COMPOSED_FOLDER)

# Upload a base image
@app.route("/upload_base", methods=["POST"])
def upload_base():
    file = request.files.get("base_image")
    if not file:
        ic("No base file provided")
        return redirect(url_for("index"))

    filename = file.filename
    if not allowed_file(filename, ALLOWED_IMAGE_EXT):
        ic("Disallowed extension for base image:", filename)
        return "Disallowed file type", 400

    # sanitize filename by using uuid
    ext = filename.rsplit(".", 1)[1].lower()
    safe_name = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
    file.save(path)
    ic("Saved base image to", path)
    return redirect(url_for("index"))

# Upload overlay PNG
@app.route("/upload_overlay", methods=["POST"])
def upload_overlay():
    file = request.files.get("overlay_image")
    if not file:
        ic("No overlay file provided")
        return redirect(url_for("index"))

    filename = file.filename
    if not allowed_file(filename, ALLOWED_OVERLAY_EXT):
        ic("Disallowed extension for overlay:", filename)
        return "Overlay must be a PNG", 400

    safe_name = f"{uuid.uuid4().hex}.png"
    path = os.path.join(app.config["OVERLAY_FOLDER"], safe_name)
    file.save(path)
    ic("Saved overlay to", path)
    return redirect(url_for("index"))

# Serve uploaded base images
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# Serve overlay images
@app.route("/overlays/<path:filename>")
def overlay_file(filename):
    return send_from_directory(app.config["OVERLAY_FOLDER"], filename)

# Serve composed images
@app.route("/composed/<path:filename>")
def composed_file(filename):
    return send_from_directory(app.config["COMPOSED_FOLDER"], filename)

# Editor page for a base image
@app.route("/edit/<path:filename>")
def edit_image(filename):
    base_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if not os.path.exists(base_path):
        ic("Base image not found:", base_path)
        return "Base image not found", 404

    overlays = sorted(os.listdir(app.config["OVERLAY_FOLDER"]))
    meta_for_image = overlays_meta.get(filename, [])

    # Template: interactive drag & drop editor
    template = """
    <!doctype html>
    <html>
    <head>
      <title>Edit {{ filename }}</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <style>
        body{ font-family: Arial, sans-serif; margin: 12px; }
        .container { display:flex; gap:12px; }
        .editor { flex: 1; border: 1px solid #ddd; padding: 8px; border-radius:6px; background:#fff; }
        .sidebar { width: 300px; border: 1px solid #ddd; padding:8px; border-radius:6px; background:#fafafa; }
        #canvasWrap { position: relative; display:inline-block; border:1px solid #ccc; }
        #baseImg { display:block; max-width:100%; height:auto; }
        .draggable { position: absolute; touch-action: none; cursor: move; user-select:none; }
        .overlayItem { margin-bottom:8px; cursor:pointer; }
        .controls { margin-top:8px; }
        button { padding:6px 10px; margin-right:6px; }
        .metaList { margin-top:10px; font-size:0.9rem; color:#333; }
      </style>
    </head>
    <body>
      <h2>Edit: {{ filename }}</h2>
      <div class="container">
        <div class="editor">
          <div id="canvasWrap">
            <img id="baseImg" src="{{ url_for('uploaded_file', filename=filename) }}" alt="base image">
            <!-- Overlay markup will be inserted dynamically -->
            <div id="overlayLayer"></div>
          </div>
        </div>

        <div class="sidebar">
          <h3>Overlays</h3>
          <p class="small">Click an overlay to add it; drag to move.</p>
          {% if overlays %}
            {% for ov in overlays %}
              <div class="overlayItem">
                <img src="{{ url_for('overlay_file', filename=ov) }}" style="max-width:100%; display:block;" />
                <button onclick="addOverlay('{{ ov }}')">Add</button>
              </div>
            {% endfor %}
          {% else %}
            <p>No overlays uploaded yet.</p>
          {% endif %}

          <div class="controls">
            <button id="saveBtn" onclick="savePosition()">Save Position</button>
            <button onclick="exportComposed()">Compose & Download</button>
            <a href="{{ url_for('index') }}">Back to index</a>
          </div>

          <div class="metaList">
            <h4>Saved overlays for this image</h4>
            <div id="savedMeta">
              {% if meta_for_image %}
                <ul>
                  {% for m in meta_for_image %}
                    <li>
                      Overlay: {{ m.overlay_filename }} at ({{ m.x }}, {{ m.y }}) size {{ m.w }}x{{ m.h }} - saved {{ m.saved_at }}
                      <br>
                      <button onclick="loadMeta({{ loop.index0 }})">Load</button>
                      <button onclick="deleteMeta({{ loop.index0 }})">Delete</button>
                    </li>
                  {% endfor %}
                </ul>
              {% else %}
                <p>No saved overlays for this image.</p>
              {% endif %}
            </div>
          </div>
        </div>
      </div>

      <script>
        const baseImg = document.getElementById("baseImg");
        const overlayLayer = document.getElementById("overlayLayer");
        let activeElem = null;
        let offsetX = 0, offsetY = 0;
        let startW = 0, startH = 0;
        let resizing = false;
        let currentMeta = {}; // holds current overlay properties

        // Add an overlay element to the canvas
        function addOverlay(filename, x=20, y=20, w=null, h=null) {
          const img = document.createElement("img");
          img.src = "{{ url_for('overlay_file', filename='') }}" + filename;
          img.className = "draggable";
          img.dataset.filename = filename;
          img.style.left = x + "px";
          img.style.top = y + "px";
          img.style.zIndex = 1000;
          img.onload = () => {
            const scale = baseImg.getBoundingClientRect().width / baseImg.naturalWidth;
            // If width/height not provided, use natural size scaled to the displayed base image
            let displayW = w || (img.naturalWidth * scale);
            let displayH = h || (img.naturalHeight * scale);
            img.style.width = displayW + "px";
            img.style.height = displayH + "px";
          }
          makeDraggable(img);
          overlayLayer.appendChild(img);
          selectElement(img);
        }

        // Make an element draggable (mouse + touch)
        function makeDraggable(el) {
          el.addEventListener("pointerdown", (e) => {
            e.preventDefault();
            activeElem = el;
            activeElem.setPointerCapture(e.pointerId);
            const rect = el.getBoundingClientRect();
            offsetX = e.clientX - rect.left;
            offsetY = e.clientY - rect.top;
            startW = rect.width;
            startH = rect.height;
            resizing = false; // Resizing disabled
          });
          el.addEventListener("pointermove", (e) => {
            if (!activeElem || activeElem !== el) return;
            e.preventDefault();
            // Move
            const baseRect = baseImg.getBoundingClientRect();
            // calculate coordinates relative to base image top-left
            let x = e.clientX - baseRect.left - offsetX;
            let y = e.clientY - baseRect.top - offsetY;
            // clamp inside base image
            x = Math.max(0, Math.min(x, baseRect.width - 5));
            y = Math.max(0, Math.min(y, baseRect.height - 5));
            activeElem.style.left = x + "px";
            activeElem.style.top = y + "px";
          });
          el.addEventListener("pointerup", (e) => {
            if (activeElem === el) {
              activeElem.releasePointerCapture(e.pointerId);
              activeElem = null;
              resizing = false;
            }
          });
        }

        function selectElement(el) {
          // highlight - simple visual effect
          Array.from(document.querySelectorAll(".draggable")).forEach(x => x.style.outline = "none");
          el.style.outline = "2px dashed #1a73e8";
        }

        // Save position: gather first overlay element only (for simplicity)
        // Note: we persist the first overlay's data. You can extend to support multiple.
        async function savePosition() {
          const el = overlayLayer.querySelector(".draggable");
          if (!el) { alert("No overlay placed."); return; }
          const baseRect = baseImg.getBoundingClientRect();
          const elRect = el.getBoundingClientRect();

          // We need coordinates relative to the natural pixel size of the base image.
          // Compute scale from displayed to natural.
          const scaleX = baseImg.naturalWidth / baseRect.width;
          const scaleY = baseImg.naturalHeight / baseRect.height;

          const x = Math.round((elRect.left - baseRect.left) * scaleX);
          const y = Math.round((elRect.top - baseRect.top) * scaleY);
          const w = Math.round(elRect.width * scaleX);
          const h = Math.round(elRect.height * scaleY);

          const payload = {
            base_filename: "{{ filename }}",
            overlay_filename: el.dataset.filename,
            x: x, y: y, w: w, h: h
          };

          console.log("Saving payload:", payload);
          const resp = await fetch("{{ url_for('save_position') }}", {
            method: "POST",
            headers: { "Content-Type":"application/json" },
            body: JSON.stringify(payload)
          });
          const data = await resp.json();
          if (resp.ok) {
            alert("Saved. Composed image: " + data.composed_url);
            // reload to see meta list updated
            location.reload();
          } else {
            alert("Save failed: " + data.error);
          }
        }

        // Compose & download: we call the server, which composes and returns file URL
        async function exportComposed() {
          const el = overlayLayer.querySelector(".draggable");
          if (!el) { alert("No overlay placed."); return; }
          const baseRect = baseImg.getBoundingClientRect();
          const elRect = el.getBoundingClientRect();
          const scaleX = baseImg.naturalWidth / baseRect.width;
          const scaleY = baseImg.naturalHeight / baseRect.height;
          const payload = {
            base_filename: "{{ filename }}",
            overlay_filename: el.dataset.filename,
            x: Math.round((elRect.left - baseRect.left) * scaleX),
            y: Math.round((elRect.top - baseRect.top) * scaleY),
            w: Math.round(elRect.width * scaleX),
            h: Math.round(elRect.height * scaleY)
          };
          const resp = await fetch("{{ url_for('compose_once') }}", {
            method: "POST",
            headers: { "Content-Type":"application/json" },
            body: JSON.stringify(payload)
          });
          const data = await resp.json();
          if (resp.ok) {
            window.open(data.composed_url, "_blank");
          } else {
            alert("Compose failed: " + data.error);
          }
        }

        // Load saved meta by index
        function loadMeta(index) {
          fetch("{{ url_for('get_meta', filename=filename) }}")
            .then(r => r.json())
            .then(data => {
              if (!data || !data.length) { alert("No saved metadata"); return; }
              const m = data[index];
              // clear existing overlay elements
              overlayLayer.innerHTML = "";
              addOverlay(m.overlay_filename, 20, 20, 10, 10); // add, will be resized after load
              // wait a bit for the element to exist and base image to be ready
              setTimeout(() => {
                const el = overlayLayer.querySelector(".draggable");
                const baseRect = baseImg.getBoundingClientRect();
                // convert stored natural coords to displayed coords
                const scaleX = baseRect.width / baseImg.naturalWidth;
                const scaleY = baseRect.height / baseImg.naturalHeight;
                el.style.left = Math.round(m.x * scaleX) + "px";
                el.style.top = Math.round(m.y * scaleY) + "px";
                el.style.width = Math.round(m.w * scaleX) + "px";
                el.style.height = Math.round(m.h * scaleY) + "px";
              }, 150);
            });
        }

        async function deleteMeta(index) {
          const resp = await fetch("{{ url_for('delete_meta', filename=filename) }}", {
            method: "POST",
            headers: { "Content-Type":"application/json" },
            body: JSON.stringify({ index: index })
          });
          if (resp.ok) {
            alert("Deleted.");
            location.reload();
          } else {
            alert("Delete failed.");
          }
        }

        // On load, if there is any saved meta, automatically load the first one (optional)
        window.addEventListener("load", () => {
          // If there are pre-saved meta items, do nothing automatically; user may load explicitly.
        });
      </script>
    </body>
    </html>
    """
    return render_template_string(template, filename=filename, overlays=overlays, meta_for_image=meta_for_image)

# Endpoint to get metadata list for a base image (JSON)
@app.route("/meta/<path:filename>", methods=["GET"])
def get_meta(filename):
    ic("get_meta for", filename)
    return jsonify(overlays_meta.get(filename, []))

# Save overlay position: accept JSON {base_filename, overlay_filename, x, y, w, h}
@app.route("/save_position", methods=["POST"])
def save_position():
    try:
        data = request.get_json()
        ic("Received save_position payload:", data)
        base_filename = data["base_filename"]
        overlay_filename = data["overlay_filename"]
        x = int(data["x"]); y = int(data["y"])
        w = int(data["w"]); h = int(data["h"])

        # Validate files exist
        base_path = os.path.join(app.config["UPLOAD_FOLDER"], base_filename)
        overlay_path = os.path.join(app.config["OVERLAY_FOLDER"], overlay_filename)
        if not os.path.exists(base_path) or not os.path.exists(overlay_path):
            ic("File missing when saving position", base_path, overlay_path)
            return jsonify({"error":"base or overlay file missing"}), 400

        entry = {
            "overlay_filename": overlay_filename,
            "x": x, "y": y, "w": w, "h": h,
            "saved_at": datetime.utcnow().isoformat() + "Z"
        }

        overlays_meta.setdefault(base_filename, []).append(entry)
        save_meta()

        # Compose immediately and return url
        composed_path = compose_images(base_path, overlay_path, x, y, overlay_display_w=w, overlay_display_h=h)
        # Return URL for composed file
        composed_name = os.path.basename(composed_path)
        composed_url = url_for("composed_file", filename=composed_name)
        ic("Returning composed_url", composed_url)
        return jsonify({"status":"ok", "composed_url": composed_url})
    except Exception as e:
        ic("Error in save_position:", e)
        return jsonify({"error": str(e)}), 500

# Compose once (no metadata saving) and return URL
@app.route("/compose_once", methods=["POST"])
def compose_once():
    try:
        data = request.get_json()
        ic("compose_once payload:", data)
        base_filename = data["base_filename"]
        overlay_filename = data["overlay_filename"]
        x = int(data["x"]); y = int(data["y"])
        w = int(data["w"]); h = int(data["h"])
        base_path = os.path.join(app.config["UPLOAD_FOLDER"], base_filename)
        overlay_path = os.path.join(app.config["OVERLAY_FOLDER"], overlay_filename)
        if not os.path.exists(base_path) or not os.path.exists(overlay_path):
            ic("File missing for compose_once", base_path, overlay_path)
            return jsonify({"error":"file missing"}), 400
        composed_path = compose_images(base_path, overlay_path, x, y, overlay_display_w=w, overlay_display_h=h)
        composed_name = os.path.basename(composed_path)
        composed_url = url_for("composed_file", filename=composed_name)
        return jsonify({"status":"ok", "composed_url": composed_url})
    except Exception as e:
        ic("Error compose_once:", e)
        return jsonify({"error": str(e)}), 500

# View metadata page (simple JSON view)
@app.route("/view_meta/<path:filename>")
def view_metadata(filename):
    return jsonify(overlays_meta.get(filename, []))

# Delete a saved metadata item by index for given base image
@app.route("/delete_meta/<path:filename>", methods=["POST"])
def delete_meta(filename):
    try:
        data = request.get_json()
        idx = int(data.get("index"))
        ic("delete_meta", filename, idx)
        if filename not in overlays_meta:
            return jsonify({"error":"no metadata for file"}), 404
        if idx < 0 or idx >= len(overlays_meta[filename]):
            return jsonify({"error":"index out of range"}), 400
        overlays_meta[filename].pop(idx)
        # remove key if empty
        if not overlays_meta[filename]:
            overlays_meta.pop(filename, None)
        save_meta()
        return jsonify({"status":"ok"})
    except Exception as e:
        ic("Error delete_meta:", e)
        return jsonify({"error": str(e)}), 500

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    ic("Starting Flask overlay editor. Upload folder:", UPLOAD_FOLDER, "Overlay folder:", OVERLAY_FOLDER)
    app.run(host="0.0.0.0", port=5202, debug=True)
