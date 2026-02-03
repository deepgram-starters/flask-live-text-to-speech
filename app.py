"""
Flask Live Text-to-Speech Starter - Backend Server

This Flask server provides a WebSocket endpoint for live text-to-speech
powered by Deepgram's Live TTS API. It streams audio data back to the client
in real-time as text is synthesized.

Key Features:
- WebSocket endpoint: /tts/stream
- Accepts JSON text messages from frontend
- Returns binary audio stream
- Serves built frontend from frontend/dist/
"""

import os
import json
import threading
import queue
from flask import Flask, request, jsonify
from flask_sock import Sock
from flask_cors import CORS
from deepgram import (
    DeepgramClient,
    SpeakWSOptions,
    SpeakWebSocketEvents,
)
from dotenv import load_dotenv
import toml

# Load .env file (won't override existing environment variables)
load_dotenv(override=False)

# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_MODEL = "aura-2-thalia-en"

# Server configuration
CONFIG = {
    "port": int(os.environ.get("PORT", 8081)),
    "host": os.environ.get("HOST", "0.0.0.0"),
    "frontend_port": int(os.environ.get("FRONTEND_PORT", 8080)),
}

# ============================================================================
# API KEY VALIDATION
# ============================================================================

def validate_api_key():
    """Validates that the Deepgram API key is configured"""
    api_key = os.environ.get("DEEPGRAM_API_KEY")

    if not api_key:
        print("\n" + "="*70)
        print("ERROR: Deepgram API key not found!")
        print("="*70)
        print("\nPlease set your API key using one of these methods:")
        print("\n1. Create a .env file (recommended):")
        print("   DEEPGRAM_API_KEY=your_api_key_here")
        print("\n2. Environment variable:")
        print("   export DEEPGRAM_API_KEY=your_api_key_here")
        print("\nGet your API key at: https://console.deepgram.com")
        print("="*70 + "\n")
        raise ValueError("DEEPGRAM_API_KEY environment variable is required")

    return api_key

# Validate on startup
API_KEY = validate_api_key()

# ============================================================================
# SETUP - Initialize Flask, WebSocket, and CORS
# ============================================================================

# Initialize Flask app (API server only)
app = Flask(__name__)

# Enable CORS for frontend communication
# Frontend runs on port 8080, backend on port 8081
CORS(app, origins=[
    f"http://localhost:{CONFIG['frontend_port']}",
    f"http://127.0.0.1:{CONFIG['frontend_port']}"
], supports_credentials=True)

# Initialize native WebSocket support
sock = Sock(app)

# ============================================================================
# HTTP ROUTES
# ============================================================================

@app.route("/api/metadata", methods=["GET"])
def get_metadata():
    """
    GET /api/metadata

    Returns metadata about this starter application from deepgram.toml
    Required for standardization compliance
    """
    try:
        with open('deepgram.toml', 'r') as f:
            config = toml.load(f)

        if 'meta' not in config:
            return jsonify({
                'error': 'INTERNAL_SERVER_ERROR',
                'message': 'Missing [meta] section in deepgram.toml'
            }), 500

        return jsonify(config['meta']), 200

    except FileNotFoundError:
        return jsonify({
            'error': 'INTERNAL_SERVER_ERROR',
            'message': 'deepgram.toml file not found'
        }), 500

    except Exception as e:
        print(f"Error reading metadata: {e}")
        return jsonify({
            'error': 'INTERNAL_SERVER_ERROR',
            'message': f'Failed to read metadata from deepgram.toml: {str(e)}'
        }), 500

# ============================================================================
# WEBSOCKET ENDPOINT
# ============================================================================

@sock.route('/tts/stream')
def live_tts(ws):
    """
    WebSocket endpoint for live text-to-speech

    Query parameters:
    - model: Deepgram TTS model (default: aura-asteria-en)
    - encoding: Audio encoding (default: linear16)
    - sample_rate: Sample rate in Hz (default: 48000)
    - container: Audio container format (default: none)

    The client sends JSON messages with "text" field and receives binary audio data.
    """
    print("Client connected to /tts/stream")

    # Get query parameters from request
    model = request.args.get('model', DEFAULT_MODEL)
    encoding = request.args.get('encoding', 'linear16')
    sample_rate = int(request.args.get('sample_rate', 48000))
    container = request.args.get('container', 'none')

    print(f"TTS Config - model: {model}, encoding: {encoding}, sample_rate: {sample_rate}")

    # Track connection state
    connected = False
    stop_event = threading.Event()
    audio_queue = queue.Queue()

    # Initialize Deepgram client
    try:
        deepgram = DeepgramClient(api_key=API_KEY)
        dg_connection = deepgram.speak.websocket.v("1")

        # Event handlers for Deepgram connection
        def on_open(self, open_event, **kwargs):
            print("✓ Connected to Deepgram TTS API")

        def on_binary_data(self, data, **kwargs):
            """Queue binary audio data from Deepgram for thread-safe forwarding to client"""
            try:
                audio_queue.put(data)
            except Exception as e:
                print(f"Error queueing audio data: {e}")
                stop_event.set()

        def on_flush(self, flushed, **kwargs):
            """Handle flush events from Deepgram"""
            print(f"Flushed: {flushed}")

        def on_close(self, close_event, **kwargs):
            """Handle Deepgram connection close"""
            print("Deepgram TTS connection closed")
            stop_event.set()

        def on_error(self, error, **kwargs):
            """Handle errors from Deepgram"""
            print(f"Deepgram TTS error: {error}")
            stop_event.set()

        # Register event handlers
        dg_connection.on(SpeakWebSocketEvents.Open, on_open)
        dg_connection.on(SpeakWebSocketEvents.AudioData, on_binary_data)
        dg_connection.on(SpeakWebSocketEvents.Flushed, on_flush)
        dg_connection.on(SpeakWebSocketEvents.Close, on_close)
        dg_connection.on(SpeakWebSocketEvents.Error, on_error)

        # Process messages from client and forward audio from Deepgram
        while not stop_event.is_set():
            try:
                # Check for audio data from Deepgram (non-blocking)
                try:
                    audio_data = audio_queue.get_nowait()
                    ws.send(audio_data)
                except queue.Empty:
                    pass
                except Exception as e:
                    print(f"Error sending audio data: {e}")
                    stop_event.set()
                    break

                # Receive message from client (with timeout)
                message = ws.receive(timeout=0.1)
                if message is None:
                    continue

                print(f"Received from client: {message[:100]}...")

                # Parse JSON message
                try:
                    data = json.loads(message)
                    text = data.get('text')
                    msg_model = data.get('model', model)

                    if not text:
                        print("No text provided in message")
                        continue

                    # Start connection if not already connected
                    if not connected:
                        options = SpeakWSOptions(
                            model=msg_model,
                            encoding=encoding,
                            sample_rate=sample_rate,
                        )

                        if not dg_connection.start(options):
                            print("Failed to start Deepgram TTS connection")
                            ws.close(1011, "Failed to connect to Deepgram")
                            break

                        connected = True
                        print(f"✓ Started Deepgram TTS connection with model: {msg_model}")

                    # Send text to Deepgram
                    dg_connection.send_text(text)
                    dg_connection.flush()

                except json.JSONDecodeError:
                    print(f"Invalid JSON received: {message}")
                    continue

            except Exception as e:
                if "timeout" not in str(e).lower():
                    print(f"Error in message loop: {e}")
                    break

    except Exception as e:
        print(f"Error setting up TTS connection: {e}")
        try:
            ws.close(1011, "Internal server error")
        except:
            pass
        return

    finally:
        # Cleanup
        print("Cleaning up TTS connection")
        try:
            if connected:
                dg_connection.finish()
        except Exception as e:
            print(f"Error finishing connection: {e}")

        print("Client disconnected from /tts/stream")

# ============================================================================
# SERVER START
# ============================================================================

if __name__ == "__main__":
    port = CONFIG["port"]
    host = CONFIG["host"]
    frontend_port = CONFIG["frontend_port"]
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print("\n" + "=" * 70)
    print(f"🚀 Flask Live Text To Speech Server (Backend API)")
    print("=" * 70)
    print(f"Backend:  http://localhost:{port}")
    print(f"Frontend: http://localhost:{frontend_port}")
    print(f"WebSocket: ws://localhost:{port}/tts/stream")
    print(f"CORS:     Enabled for frontend port {frontend_port}")
    print(f"Debug:    {'ON' if debug else 'OFF'}")
    print("=" * 70 + "\n")

    app.run(host=host, port=port, debug=debug)
