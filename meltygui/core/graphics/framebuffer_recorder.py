import ctypes
import subprocess
import tempfile

import OpenGL.GL as gl
import numpy as np


class FramebufferRecorder:
    def __init__(self, width, height, fps=30, codec='h264', bitrate='20M', gpu_encoding=True):
        """
        Initialize recorder for OpenGL framebuffers

        Args:
            width (int): Width of the framebuffer
            height (int): Height of the framebuffer
            fps (int): Frames per second for recording
            codec (str): Codec to use for video ('h264', 'hevc', etc.)
            bitrate (str): Target bitrate for video
            gpu_encoding (bool): Whether to use GPU acceleration if available
        """
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.bitrate = bitrate
        self.gpu_encoding = gpu_encoding

        # Configuration
        self.recording = False
        self.frames = []
        self.frame_count = 0

        # Create a pixel buffer object (PBO) for faster GPU->CPU transfers
        self.pbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, self.pbo)
        gl.glBufferData(gl.GL_PIXEL_PACK_BUFFER, width * height * 4, None, gl.GL_STREAM_READ)
        gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, 0)

    def start_recording(self):
        """Start recording frames"""
        if self.recording:
            return

        self.recording = True
        self.frames = []
        self.frame_count = 0
        print("Recording started...")

    def stop_recording(self):
        """Stop recording"""
        if not self.recording:
            return

        self.recording = False
        print(f"Recording stopped. Captured {self.frame_count} frames.")

    def capture_frame(self, framebuffer=0, read_buffer=gl.GL_BACK):
        """
        Capture a frame from the specified framebuffer

        Args:
            framebuffer (int): OpenGL framebuffer object ID (0 for default framebuffer)
            read_buffer (GL enum): Buffer to read from (GL_BACK, GL_COLOR_ATTACHMENT0, etc.)

        Returns:
            bool: True if frame was captured successfully
        """
        if not self.recording:
            return False

        # Bind the framebuffer we want to read from
        current_fbo = gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, framebuffer)

        # Set the read buffer
        if framebuffer == 0:  # Default framebuffer
            gl.glReadBuffer(read_buffer)
        else:  # Custom framebuffer
            gl.glReadBuffer(read_buffer)

        # Use PBO for faster transfers
        gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, self.pbo)

        # Read pixels into the PBO (this is asynchronous)
        gl.glReadPixels(0, 0, self.width, self.height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)

        # Map the PBO and get a pointer to the data
        ptr = gl.glMapBuffer(gl.GL_PIXEL_PACK_BUFFER, gl.GL_READ_ONLY)

        result = False
        if ptr:
            # Copy data from the mapped buffer
            data = np.ctypeslib.as_array(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ubyte)),
                                         shape=(self.height, self.width, 4))
            # We need to copy because the PBO will be unmapped
            frame_copy = np.copy(data)
            # Flip the image vertically (OpenGL has origin at bottom-left)
            frame_copy = np.flipud(frame_copy)

            # Unmap the buffer
            gl.glUnmapBuffer(gl.GL_PIXEL_PACK_BUFFER)

            # Store the frame (removing alpha channel)
            self.frames.append(frame_copy[:, :, :3])
            self.frame_count += 1
            result = True

        # Unbind the PBO
        gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, 0)

        # Restore the original framebuffer
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, current_fbo)

        return result

    def save_video(self, output_file="output.mp4"):
        """
        Save the captured frames as a video file using FFmpeg

        Args:
            output_file (str): Path to the output video file

        Returns:
            bool: True if video was saved successfully
        """
        if not self.frames:
            print("No frames to save")
            return False

        print(f"Saving video to {output_file}...")

        if self.gpu_encoding and self._is_gpu_encoding_available():
            return self._save_video_gpu(output_file)
        else:
            return self._save_video_cpu(output_file)

    def _save_video_cpu(self, output_file):
        """Save video using CPU-based encoding with FFmpeg"""
        # Create a temporary directory for the output
        with tempfile.TemporaryDirectory() as temp_dir:
            # Set up FFmpeg command
            ffmpeg_cmd = [
                'ffmpeg',
                '-y',  # Overwrite output file if it exists
                '-f', 'rawvideo',
                '-vcodec', 'rawvideo',
                '-s', f'{self.width}x{self.height}',
                '-pix_fmt', 'rgb24',
                '-r', str(self.fps),
                '-i', '-',  # Read from stdin
                '-c:v', self.codec,
                '-b:v', self.bitrate,
                '-pix_fmt', 'yuv420p',  # Standard pixel format for compatibility
                output_file
            ]

            # Start FFmpeg process
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Feed frames to FFmpeg
            for frame in self.frames:
                process.stdin.write(frame.tobytes())

            # Close stdin and wait for FFmpeg to finish
            process.stdin.close()
            process.wait()

            if process.returncode != 0:
                stderr = process.stderr.read()
                print(f"FFmpeg error: {stderr.decode()}")
                return False

            print(f"Video saved successfully to {output_file}")
            return True

    def _save_video_gpu(self, output_file):
        """Save video using GPU-based encoding with FFmpeg (if available)"""
        # Determine which GPU encoder to use
        encoder = self._get_gpu_encoder()
        if not encoder:
            print("No suitable GPU encoder found, falling back to CPU encoding")
            return self._save_video_cpu(output_file)

        # Set up FFmpeg command with GPU acceleration
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-f', 'rawvideo',
            '-vcodec', 'rawvideo',
            '-s', f'{self.width}x{self.height}',
            '-pix_fmt', 'rgb24',
            '-r', str(self.fps),
            '-i', '-',
            '-c:v', encoder,
            '-b:v', self.bitrate,
            '-preset', 'fast',
            output_file
        ]

        # Additional options for specific encoders
        if 'nvenc' in encoder:
            ffmpeg_cmd.insert(-1, '-gpu', '0')
        elif 'qsv' in encoder:
            ffmpeg_cmd.insert(-1, '-hwaccel', 'qsv')
        elif 'amf' in encoder:
            ffmpeg_cmd.insert(-1, '-hwaccel', 'amf')

        # Start FFmpeg process
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Feed frames to FFmpeg
        for frame in self.frames:
            process.stdin.write(frame.tobytes())

        # Close stdin and wait for FFmpeg to finish
        process.stdin.close()
        process.wait()

        if process.returncode != 0:
            stderr = process.stderr.read()
            print(f"FFmpeg error: {stderr.decode()}")
            return False

        print(f"Video saved successfully to {output_file} using GPU acceleration")
        return True

    def _is_gpu_encoding_available(self):
        """Check if GPU encoding is available"""
        try:
            # Run FFmpeg to get available encoders
            result = subprocess.run(
                ['ffmpeg', '-encoders'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Check for GPU encoders
            output = result.stdout
            return any(encoder in output for encoder in ['nvenc', 'qsv', 'amf', 'videotoolbox'])
        except Exception as e:
            print(f"Error checking GPU encoders: {e}")
            return False

    def _get_gpu_encoder(self):
        """Get the appropriate GPU encoder based on the system"""
        try:
            # Run FFmpeg to get available encoders
            result = subprocess.run(
                ['ffmpeg', '-encoders'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            output = result.stdout

            # Check for NVIDIA encoders (NVENC)
            if 'h264_nvenc' in output:
                return 'h264_nvenc'
            elif 'hevc_nvenc' in output:
                return 'hevc_nvenc'

            # Check for Intel QuickSync
            if 'h264_qsv' in output:
                return 'h264_qsv'
            elif 'hevc_qsv' in output:
                return 'hevc_qsv'

            # Check for AMD encoders
            if 'h264_amf' in output:
                return 'h264_amf'
            elif 'hevc_amf' in output:
                return 'hevc_amf'

            # Check for Apple VideoToolbox (macOS)
            if 'h264_videotoolbox' in output:
                return 'h264_videotoolbox'

            return None
        except Exception as e:
            print(f"Error determining GPU encoder: {e}")
            return None

    def clear_frames(self):
        """Clear all captured frames without saving"""
        self.frames = []
        self.frame_count = 0
        print("Frames cleared.")

    def get_frame_count(self):
        """Get the number of frames captured"""
        return self.frame_count


# Example usage:
"""
# Initialize the recorder (typically in your setup code)
recorder = FramebufferRecorder(width=1920, height=1080, fps=30)

# Start recording
recorder.start_recording()

# In your render loop:
def render():
    # Render to your custom framebuffer
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, my_fbo)
    # ... render your scene ...

    # Capture the frame from your custom framebuffer
    if recorder.recording:
        recorder.capture_frame(
            framebuffer=my_fbo,
            read_buffer=gl.GL_COLOR_ATTACHMENT0
        )

    # Continue with your rendering...
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
    # ... render other things ...

# When done recording:
recorder.stop_recording()

# Save the video at any point after stopping recording
recorder.save_video("my_video.mp4")
"""