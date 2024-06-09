from flask import Flask, request, jsonify, send_file
#from flask_socketio import SocketIO, emit
import os
import logging
from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv
from io import BytesIO
from pydub import AudioSegment
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import base64
import numpy as np
import sounddevice as sd
import soundfile as sf
from memoize_audio_processing import memoizeAudioProccessing

load_dotenv()

class Server:
    def __init__(self):
        self.app = Flask(__name__)
        self.app.config['UPLOAD_FOLDER'] = 'uploads'
        self.app.config['ALLOWED_EXTENSIONS'] = {'mp3', 'enc'}
        self.setupLogging()
        self.setupUploadFolder()
        self.setupRoutes()
        self.encryption_key = base64.urlsafe_b64decode(
            os.getenv('ENCRYPTION_KEY') + '===')
        self.processor = memoizeAudioProccessing()
        #self.socketio = SocketIO(self.app, cors_allowed_origins="*")
        

    def setupLogging(self):
        logging.basicConfig(level=logging.DEBUG)

    def setupUploadFolder(self):
        if not os.path.exists(self.app.config['UPLOAD_FOLDER']):
            os.makedirs(self.app.config['UPLOAD_FOLDER'])

    def setupRoutes(self):
        self.app.add_url_rule('/process', 'processRequest',
                              self.processRequest, methods=['POST'])
        self.app.add_url_rule('/audio', 'audioStream', self.audioStream, methods=['POST'])

    def allowedFile(self, filename):
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in self.app.config['ALLOWED_EXTENSIONS']

    def authenticateApiKey(self, request):
        provided_api_key = request.headers.get('key')
        valid_api_keys = os.getenv('FLASK_API_KEYS').split(',')
        self.app.logger.debug(f"Provided API key: {provided_api_key}")
        self.app.logger.debug(f"Valid API keys: {valid_api_keys}")
        return provided_api_key in valid_api_keys

    def getAllVoices(self, client):
        try:
            response = client.voices.get_all()
            return response.voices
        except Exception as e:
            self.app.logger.error(f"Error getting voices: {str(e)}")
            return []

    def decrypt_file(self, file_path, encryption_key):
        with open(file_path, 'rb') as file:
            encrypted_data = file.read()

        iv = encrypted_data[:16]
        ciphertext = encrypted_data[16:]
        cipher = Cipher(algorithms.AES(encryption_key),
                        modes.CFB(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted_data = decryptor.update(ciphertext) + decryptor.finalize()

        decrypted_file_path = file_path.rstrip('.enc')
        with open(decrypted_file_path, 'wb') as file:
            file.write(decrypted_data)

        return decrypted_file_path

    def processRequest(self):
        if not self.authenticateApiKey(request):
            return jsonify({"error": "Unauthorized"}), 401

        request_type = request.headers.get('request-type')

        if request_type == 'ADD_USER':
            return self.addUser(request)
        elif request_type == 'INPUT':
            return self.generateSpeech(request)
        else:
            return jsonify({"error": "Invalid request type"}), 400

    def addUser(self, request):
        if 'files[]' not in request.files:
            self.app.logger.error("No files part in the request")
            return jsonify({"error": "No files part in the request"}), 400

        files = request.files.getlist('files[]')

        if len(files) == 0:
            self.app.logger.error("No files uploaded")
            return jsonify({"error": "No files uploaded"}), 400

        file_paths = []
        total_duration = 0
        files_encrypted = request.form.get(
            'files_encrypted', 'false') == 'true'

        for file in files:
            if file and self.allowedFile(file.filename):
                filename = file.filename
                filepath = os.path.join(
                    self.app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                if files_encrypted:
                    filepath = self.decrypt_file(filepath, self.encryption_key)

                file_paths.append(filepath)

                try:
                    audio = AudioSegment.from_file(filepath)
                    duration = len(audio) / 1000  # Convert to seconds
                    total_duration += duration

                    self.app.logger.debug(
                        f"File {filename} duration: {duration} seconds")
                except Exception as e:
                    self.app.logger.error(
                        f"File {filename} is corrupted or invalid: {str(e)}")
                    return jsonify({"error": f"File {filename} is corrupted or invalid"}), 400
            else:
                self.app.logger.error(f"File {file.filename} is not allowed")
                return jsonify({"error": f"File {file.filename} is not allowed"}), 400

        if total_duration < 30:
            self.app.logger.error(
                "Total duration of files is less than 30 seconds")
            return jsonify({"error": "Total duration of files must be at least 30 seconds"}), 400

        try:
            api_key = os.getenv('ELEVEN_API_KEY')
            client = ElevenLabs(api_key=api_key)
            provided_api_key = request.headers.get('key')
            provided_name = request.form.get('voice')
            encrypted_voice_name = self.encrypt(
                f"{provided_api_key}~{provided_name}")
            self.app.logger.debug(
                f"Requested encrypted voice name: {encrypted_voice_name}")

            provided_voice_description = request.headers.get(
                'voice_description')

            voices = self.getAllVoices(client)
            for voice in voices:
                if voice.name == encrypted_voice_name:
                    return jsonify({"message": "Voice already exists", "voice": voice})

            voice = client.clone(
                name=encrypted_voice_name,
                description=provided_voice_description,
                files=file_paths,
            )
            return jsonify({"message": "Voice cloned successfully", "voice": voice})
        except Exception as e:
            self.app.logger.error(f"Error cloning voice: {str(e)}")
            return jsonify({"error": f"Error cloning voice: {str(e)}"}), 500

    def generateSpeech(self, request):
        try:
            api_key = os.getenv('ELEVEN_API_KEY')
            client = ElevenLabs(api_key=api_key)

            text = request.form.get('text')
            submitted_api_key = request.headers.get('key')
            self.app.logger.debug(f"Submitted API key: {submitted_api_key}")
            provided_name = request.form.get('voice')
            encrypted_voice_name = self.encrypt(
                f"{submitted_api_key}~{provided_name}")
            self.app.logger.debug(
                f"Requested encrypted voice name: {encrypted_voice_name}")

            if not text:
                return jsonify({"error": "Text is required"}), 400

            audio_generator = client.generate(
                text=text,
                voice=encrypted_voice_name,
                model='eleven_multilingual_v2'
            )

            audio = b''.join(audio_generator)

            audio_io = BytesIO(audio)
            audio_io.seek(0)

            return send_file(audio_io, mimetype='audio/mpeg', as_attachment=True, download_name='speech.mp3')
        except Exception as e:
            self.app.logger.error(f"Error generating speech: {str(e)}")
            return jsonify({"error": f"Error generating speech: {str(e)}"}), 500

    def audioStream(self):
        if not self.authenticateApiKey(request):
            return jsonify({"error": "Unauthorized"}), 401

        try:
            capture_duration = 3  # Capture time in seconds - currently configured to save to a file

            self.app.logger.debug("Recording audio...")
            audio_data = sd.rec(int(capture_duration * 44100), samplerate=44100, channels=1, dtype='int16')
            sd.wait() 
            self.app.logger.debug("Audio recording finished.")

            # Saves audio to wav file in upload folder
            upload_folder = self.app.config['UPLOAD_FOLDER']
            os.makedirs(upload_folder, exist_ok=True)
            audio_file_path = os.path.join(upload_folder, 'captured_audio.wav')
            sf.write(audio_file_path, audio_data, samplerate=44100)

            processor = memoizeAudioProccessing()
            transcription = processor.transcribe(audio_file_path)

            return jsonify({"message": transcription, "audio_file_path": audio_file_path}), 200

        except Exception as e:
            self.app.logger.error(f"Error receiving or saving audio: {str(e)}")
            return jsonify({"error": f"Error receiving or saving audio: {str(e)}"}), 500
        
    def run(self):
        self.app.run(debug=False)


if __name__ == '__main__':
    server = Server()
    server.run()