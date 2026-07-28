printf "file '%s'\n" *.mp3 > files.txt && ffmpeg -f concat -safe 0 -i files.txt -c copy joined.mp3
