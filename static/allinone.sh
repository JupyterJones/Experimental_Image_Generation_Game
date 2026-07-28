#!/bin/bash
ffmpeg -hide_banner  -framerate 6 -i frame_%03d.png -c:v libx265 -r 24 -pix_fmt yuv420p -y land6_3.mp4
ffmpeg -i land6_3.mp4 -vf "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:vsbmc=1,scale=768:512:flags=lanczos,unsharp=7:7:1.5:7:7:0.0" -c:v libx264 \
-crf 18 -preset slow -c:a copy -y land6_3_smooth60.mp4
vlc land6_3_smooth60.mp4
