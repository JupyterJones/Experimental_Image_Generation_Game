import os
from moviepy.editor import *
import subprocess

def make_movie(video_files, output_file):
    # Create a MovieClip object from the first video file
    clip = VideoFileClip(video_files[0])
    
    # Append the remaining video files to the clip
    for file in video_files[1:]:
        clip = clip.append(VideoFileClip(file))
        
    # Write the final clip to an output file
    clip.write_videofile(output_file)

def get_video_paths():
    # Create a CLI that waits for user input and passes it back to our script
    cli_args = ["python", "-c", "import sys; print('Enter the paths of the videos you want to combine (comma-separated): '); print(input())"]
    
    # Run the CLI and capture its output
    subprocess.run(cli_args)
    
    # Get the list of input video files from the user
    video_files_str = input()
    
    try:
        # Split the input string into a list of video paths
        video_files = [file.strip() for file in video_files_str.split(',')]
        
        return video_files
    except ValueError:
        print("Invalid input. Please enter comma-separated video paths.")

def get_output_file_name():
    # Get the output movie file name from the user
    while True:
        output_file = input("Enter the path and name of the output movie file: ")
        
        if os.path.exists(output_file):
            print("Output file already exists. Please choose a different name.")
        else:
            return output_file

def main():
    # Get the list of input video files
    video_files = get_video_paths()
    
    # Get the output movie file name
    output_file = get_output_file_name()
    
    # Create the movie by calling the make_movie function
    make_movie(video_files, output_file)

if __name__ == "__main__":
    main()
