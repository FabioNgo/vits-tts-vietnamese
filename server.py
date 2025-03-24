import base64

import tornado.ioloop
import tornado.web
from PIL import ImageFont

from validate import validate_query_params
from tts import text_to_speech
import hashlib
import os
import json
from pydub import AudioSegment  # Ensure this is at the top of your file
from moviepy import ImageClip, AudioFileClip, CompositeVideoClip, TextClip

# Define a JSON schema for your query parameters
query_param_schema = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "speed": {"type": "string", "maxLength": 9}
    },
    "required": ["text", "speed"]
}


class HomeHandler(tornado.web.RequestHandler):
    def get(self):
        self.render('web/index.html')


class MyHandler(tornado.web.RequestHandler):
    @validate_query_params(query_param_schema)
    def get(self):
        # Parameters are already validated here
        text: str = self.get_argument('text')
        speed: str = self.get_argument('speed')
        current_url: str = '{}://{}'.format(self.request.protocol, self.request.host)
        result, file_name = handle_tts_request(text, speed)
        result["audio_url"] = current_url + "/audio/" + file_name
        self.write(result)

    def post(self):
        try:
            data = json.loads(self.request.body.decode('utf-8'))
            texts: list = data.get('texts')
            speed: str = data.get('speed')
            pause: int = data.get('pause', 500)

            if not texts or not isinstance(texts, list) or not speed:
                self.set_status(400)  # Bad Request
                self.write(json.dumps({"error": "Missing 'texts' (array of strings) or 'speed' in request body."}))
                return

            results = []
            audio_files = []  # Keep track of individual audio file paths
            current_url: str = '{}://{}'.format(self.request.protocol, self.request.host)
            audio_dir = os.getcwd() + "/audio"

            combined_audio_text = "".join(texts)
            combined_audio_file_name = hashlib.sha1(combined_audio_text.encode('utf-8')).hexdigest() + ".wav"
            full_combined_audio_path = f"{audio_dir}/{combined_audio_file_name}"

            if os.path.isfile(full_combined_audio_path):
                results.append({
                    "combined_audio_url": f"{current_url}/audio/{combined_audio_file_name}"
                })
                self.write(json.dumps(results))
                return

            for text in texts:
                result, file_name = handle_tts_request(text, speed)
                result["audio_url"] = current_url + "/audio/" + file_name
                audio_files.append(file_name)  # Track audio file for combining

            # Join all texts and compute the hash of the resulting string

            combined_audio_file_name = combine_audio_files(audio_files, audio_dir, combined_audio_file_name,
                                                           pause=pause)

            # Add the combined audio file to the response
            results.append({
                "combined_audio_url": f"{current_url}/audio/{combined_audio_file_name}"
            })

            self.write(json.dumps(results))

        except json.JSONDecodeError:
            self.set_status(400)  # Bad Request
            self.write(json.dumps({"error": "Invalid JSON in request body."}))
        except Exception as e:
            self.set_status(500)  # Internal Server Error
            self.write(json.dumps({"error": str(e)}))

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "x-requested-with")
        self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')


class VideoHandler(tornado.web.RequestHandler):
    def post(self):
        try:
            data = json.loads(self.request.body.decode('utf-8'))
            audio_file_name = data.get('audio_file_name')
            title = data.get('title')
            image_data = data.get('image_data')

            if not audio_file_name or not title or not image_data:
                self.set_status(400)  # Bad Request
                self.write(json.dumps({"error": "Missing 'audio_file_name', 'title', or 'imageData' in request body."}))
                return

            video_file_name = create_video(audio_file_name, title, image_data)
            current_url = '{}://{}'.format(self.request.protocol, self.request.host)
            video_url = f"{current_url}/audio/{video_file_name}"

            self.write(json.dumps({"video_url": video_url}))
        except json.JSONDecodeError:
            self.set_status(400)  # Bad Request
            self.write(json.dumps({"error": "Invalid JSON in request body."}))
        except Exception as e:
            self.set_status(500)  # Internal Server Error
            self.write(json.dumps({"error": str(e)}))


def make_app():
    return tornado.web.Application([
        (r'/', HomeHandler),
        (r"/tts", MyHandler),
        (r"/video", VideoHandler),
        (r'/audio/(.*)', tornado.web.StaticFileHandler, {'path': os.getcwd() + "/audio/"}),
        (r'/assets/(.*)', tornado.web.StaticFileHandler, {'path': os.getcwd() + "/assets/"}),
    ])


def handle_tts_request(text, speed):
    text_hash: str = hashlib.sha1((text + speed).encode('utf-8')).hexdigest()
    file_name = text_hash + ".wav"
    file_path = os.getcwd() + "/audio/" + file_name
    if os.path.isfile(file_path):
        return ({
                    "hash": text_hash,
                    "speed": speed,
                }, file_name)
    else:
        # create new file 
        model_name = "pretrained_vi.onnx"
        audio_path = text_to_speech(text, speed, model_name, text_hash)
        return ({
                    "hash": text_hash,
                    "speed": speed,
                }, file_name)


def combine_audio_files(audio_files: list, output_dir: str, output_file: str, pause: int = 1000) -> str:
    combined_audio = None
    full_output_file = f"{output_dir}/{output_file}"
    pause = AudioSegment.silent(pause)  # duration is in milliseconds

    if os.path.isfile(full_output_file):  # Return existing file if already combined
        return output_file

    for audio_path in audio_files:
        audio = AudioSegment.from_file(f"{output_dir}/{audio_path}")

        if combined_audio is None:  # First audio file
            combined_audio = audio
        else:  # Append subsequent audio files
            combined_audio = combined_audio + pause + audio

    # Export the combined audio to the output file
    combined_audio.export(full_output_file, format="wav")
    # Delete individual audio files after combining
    for audio_path in audio_files:
        os.remove(f"{output_dir}/{audio_path}")

    return output_file


def create_video(audio_file_name: str, title: str, imageData: str):
    absolute_audio_file_name = os.getcwd() + "/audio/" + audio_file_name

    # Decode base64 image and save it as a temporary file
    temp_img_file = os.getcwd() + "/audio/temp_image.png"
    with open(temp_img_file, "wb") as image_file:
        image_file.write(base64.b64decode(imageData))

    # Create a temporary video file using the image and title
    output_video_file_name = audio_file_name.replace('.wav', '.mp4')
    output_video_path = os.path.join(os.getcwd(), "audio", output_video_file_name)

    # Load the audio to get its duration

    audio_clip = AudioFileClip(absolute_audio_file_name)
    audio_duration = audio_clip.duration

    # Create an ImageClip for the video
    image_clip = ImageClip(temp_img_file, duration=audio_duration)  # Match duration with audio

    # Add title text as a TextClip
    font_path = os.getcwd()+"/assets/font.ttf"
    if not os.path.exists(font_path):
        raise FileNotFoundError(f"Font file not found: {font_path}")

    text_clip = TextClip( font=font_path, text=title, font_size=50, color='white', method='caption',
                         size=image_clip.size, duration=audio_duration).with_position(('center', 50))


    # Overlay the text on the image
    video_clip = CompositeVideoClip([image_clip, text_clip])

    # Add audio to the video
    video_with_audio = video_clip.with_audio(audio_clip).with_duration(audio_duration)

    # Write the video file
    video_with_audio.write_videofile(output_video_path, fps=24, codec="libx264", audio_codec="aac")

    # Clean up temporary image file
    os.remove(temp_img_file)

    return output_video_file_name


if __name__ == "__main__":
    app = make_app()
    app.listen(8888)
    tornado.ioloop.IOLoop.current().start()
