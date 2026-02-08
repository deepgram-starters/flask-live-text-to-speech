"""
Flask Live Text-to-Speech Starter - Backend Server

Simple WebSocket proxy to Deepgram's Live TTS API.
Forwards all messages (JSON and binary) bidirectionally between client and Deepgram.

WebSocket endpoint: /api/live-text-to-speech
"""

import os
import json
import threading
from flask import Flask, request, jsonify
from flask_sock import Sock
from flask_cors import CORS
from urllib.parse import urlencode
import websocket
import toml
from dotenv import load_dotenv

# Load .env file (won't override existing environment variables)
load_dotenv(override=False)

# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_MODEL = "aura-asteria-en"

# Server configuration
CONFIG = {
    "port": int(os.environ.get("PORT", 8081)),
    "host": os.environ.get("HOST", "0.0.0.0"),
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
CORS(app)

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

@sock.route('/api/live-text-to-speech')
def live_text_to_speech(ws):
    """
    WebSocket endpoint for live text-to-speech
    Simple bidirectional proxy to Deepgram's Live TTS API

    Query parameters:
    - model: Deepgram TTS model (default: aura-asteria-en)
    - encoding: Audio encoding (default: linear16)
    - sample_rate: Sample rate in Hz (default: 48000)
    - container: Audio container format (default: none)

    The client sends JSON text messages and receives binary audio data.
    """
    print("Client connected to /api/live-text-to-speech")

    # Get query parameters from request
    model = request.args.get('model', DEFAULT_MODEL)
    encoding = request.args.get('encoding', 'linear16')
    sample_rate = request.args.get('sample_rate', '48000')
    container = request.args.get('container', 'none')

    print(f"TTS Config - model: {model}, encoding: {encoding}, sample_rate: {sample_rate}, container: {container}")

    # Build Deepgram WebSocket URL with query parameters
    deepgram_params = {
        'model': model,
        'encoding': encoding,
        'sample_rate': sample_rate,
        'container': container
    }
    deepgram_url = f"wss://api.deepgram.com/v1/speak?{urlencode(deepgram_params)}"

    # Message counters for logging
    client_message_count = 0
    deepgram_message_count = 0
    stop_event = threading.Event()
    deepgram_ready = threading.Event()

    def on_deepgram_message(dg_ws, message):
        """Forward messages from Deepgram to client"""
        nonlocal deepgram_message_count

        # Wait for client to be ready before forwarding
        if not deepgram_ready.wait(timeout=5):
            print("Timeout waiting for client to be ready")
            stop_event.set()
            return

        deepgram_message_count += 1

        # Log non-binary messages and every 10th binary message
        if isinstance(message, str) or deepgram_message_count % 10 == 0:
            msg_type = "JSON" if isinstance(message, str) else "binary"
            print(f"← Deepgram {msg_type} message #{deepgram_message_count}")

        try:
            ws.send(message)
        except Exception as e:
            print(f"Error forwarding to client: {e}")
            stop_event.set()

    def on_deepgram_error(dg_ws, error):
        """Handle Deepgram errors"""
        print(f"Deepgram error: {error}")
        stop_event.set()

    def on_deepgram_close(dg_ws, close_status_code, close_msg):
        """Handle Deepgram connection close"""
        print(f"Deepgram connection closed: {close_status_code} {close_msg}")
        stop_event.set()

    def on_deepgram_open(dg_ws):
        """Handle Deepgram connection open - send Open event to client"""
        print("✓ Connected to Deepgram TTS API")
        try:
            # Notify client that connection is ready
            ws.send(json.dumps({'type': 'Open'}))
        except Exception as e:
            print(f"Error sending Open event: {e}")

    # Create WebSocket connection to Deepgram
    try:
        deepgram_ws = websocket.WebSocketApp(
            deepgram_url,
            header={
                'Authorization': f'Token {API_KEY}'
            },
            on_open=on_deepgram_open,
            on_message=on_deepgram_message,
            on_error=on_deepgram_error,
            on_close=on_deepgram_close
        )

        # Run Deepgram WebSocket in background thread
        dg_thread = threading.Thread(target=deepgram_ws.run_forever)
        dg_thread.daemon = True
        dg_thread.start()

        # Wait a moment for Deepgram connection to initialize
        import time
        time.sleep(0.1)

        # Signal that we're ready to receive Deepgram messages
        deepgram_ready.set()
        print("✓ Ready to forward messages")

        # Forward messages from client to Deepgram
        while not stop_event.is_set():
            try:
                # Receive message from client (with timeout)
                message = ws.receive(timeout=0.1)
                if message is None:
                    continue

                client_message_count += 1

                # Log JSON messages and every 100th binary message
                if isinstance(message, str) or client_message_count % 100 == 0:
                    msg_type = "JSON" if isinstance(message, str) else "binary"
                    print(f"→ Client {msg_type} message #{client_message_count}")

                # Forward to Deepgram
                if isinstance(message, bytes):
                    deepgram_ws.send(message, opcode=websocket.ABNF.OPCODE_BINARY)
                else:
                    deepgram_ws.send(message)

            except Exception as e:
                if "timeout" not in str(e).lower():
                    print(f"Error in client message loop: {e}")
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
        stop_event.set()
        try:
            deepgram_ws.close()
        except Exception as e:
            print(f"Error closing Deepgram connection: {e}")

        print("Client disconnected from /api/live-text-to-speech")

# ============================================================================
# SERVER START
# ============================================================================

if __name__ == "__main__":
    port = CONFIG["port"]
    host = CONFIG["host"]
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print("\n" + "=" * 70)
    print(f"🚀 Flask Live Text-to-Speech Server (Backend API)")
    print("=" * 70)
    print(f"Backend:  http://localhost:{port}")
    print("")
    print("📡 WS   /api/live-text-to-speech")
    print("📡 GET  /api/metadata")
    print("")
    print(f"CORS:     Enabled (wildcard)")
    print(f"Debug:    {'ON' if debug else 'OFF'}")
    print("=" * 70 + "\n")

    app.run(host=host, port=port, debug=debug)
