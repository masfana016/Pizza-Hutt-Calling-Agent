import asyncio
import base64
import hmac
from twilio.request_validator import RequestValidator
import json
import os
from xml.sax.saxutils import quoteattr

import websockets
from aiohttp import WSMsgType, web
from dotenv import load_dotenv

load_dotenv()

from pizza_functions import FUNCTION_MAP, init_db  # noqa: E402

# =========================================================
# CONFIG
# =========================================================

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # https URL, no trailing slash
# STREAM_TOKEN = os.getenv("STREAM_TOKEN")  # any long random string
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

def validate_twilio_request(url, params, signature):
    if not signature:
        return False

    try:
        return validator.validate(url, params, signature)
    except Exception as e:
        print(f"Signature validation error: {e}")
        return False

if not TWILIO_AUTH_TOKEN:
    raise RuntimeError("TWILIO_AUTH_TOKEN not found in .env")

validator = RequestValidator(TWILIO_AUTH_TOKEN)

ORDER_TOOLS = {"place_order", "check_order_status", "cancel_order", "add_items_to_order"}


def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not found")
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
    )


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)

    with open("pizza_agent_config.json", "r") as f:
        pizza = json.load(f)

    agent = config["agent"]
    agent["greeting"] = pizza["greeting"]
    agent["think"]["prompt"] = pizza["prompt"]
    agent["think"]["functions"] = pizza["functions"]

    return config

# =========================================================
# BARGE-IN + FUNCTION CALLS
# =========================================================

async def handle_barge_in(decoded, twilio_ws_send, streamsid):
    if decoded.get("type") == "UserStartedSpeaking":
        await twilio_ws_send(json.dumps({"event": "clear", "streamSid": streamsid}))


def execute_function_call(func_name, arguments):
    func = FUNCTION_MAP.get(func_name)
    if not func:
        return {"error": f"Unknown function: {func_name}"}
    return func(**arguments)


async def handle_function_call_request(decoded, sts_ws, call_ctx):
    for fc in decoded.get("functions", []):
        if fc.get("client_side") is False:
            continue
        func_id = fc.get("id", "unknown")
        func_name = fc.get("name", "unknown")
        try:
            arguments = json.loads(fc.get("arguments") or "{}")
            if func_name in ORDER_TOOLS:
                # caller ID comes from Twilio, never from the model
                arguments["phone"] = call_ctx.get("caller", "")
            print(f"Function call: {func_name} ({func_id})")
            # DB work runs in a thread so it cannot freeze the audio loop
            result = await asyncio.to_thread(execute_function_call, func_name, arguments)
        except Exception as e:
            print(f"Function {func_name} failed: {e}")
            result = {"error": "The system could not complete that request."}

        print(
            f"Thought signature present: "
            f"{bool(fc.get('thought_signature'))}"
            )

        # await sts_ws.send(json.dumps({
        #     "type": "FunctionCallResponse",
        #     "id": func_id,
        #     "name": func_name,
        #     "content": json.dumps(result),
        # }))
        response = {
            "type": "FunctionCallResponse",
            "id": func_id,
            "name": func_name,
            "content": json.dumps(result),
        }

        # Preserve Gemini thought signature if Deepgram provides it
        thought_signature = fc.get("thought_signature")

        if thought_signature is not None:
            response["thought_signature"] = thought_signature

        await sts_ws.send(json.dumps(response))


async def handle_text_message(decoded, twilio_ws_send, sts_ws, streamsid, call_ctx):
    await handle_barge_in(decoded, twilio_ws_send, streamsid)
    if decoded.get("type") == "FunctionCallRequest":
        await handle_function_call_request(decoded, sts_ws, call_ctx)


# =========================================================
# AUDIO BRIDGE
# =========================================================

async def sts_sender(sts_ws, audio_queue):
    while True:
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)


async def sts_receiver(sts_ws, twilio_ws_send, streamsid_queue, call_ctx):
    streamsid = await streamsid_queue.get()
    async for message in sts_ws:
        if isinstance(message, str):
            decoded = json.loads(message)
            msg_type = decoded.get("type")
            if msg_type in ("Warning", "Error"):
                print(f"Deepgram {msg_type}: {decoded}")  # these hold error details, not customer data
            else:
                print(f"Deepgram: {msg_type}")  # types only: transcripts hold customer data
            await handle_text_message(decoded, twilio_ws_send, sts_ws, streamsid, call_ctx)
            continue

        await twilio_ws_send(json.dumps({
            "event": "media",
            "streamSid": streamsid,
            "media": {"payload": base64.b64encode(message).decode("ascii")},
        }))


async def twilio_receiver(ws, audio_queue, streamsid_queue, call_ctx):
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray()

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
            event = data.get("event")

            if event == "start":
                start = data["start"]
                params = start.get("customParameters", {})

                call_ctx["caller"] = params.get("from", "")
                streamsid_queue.put_nowait(start["streamSid"])

            elif event == "media":
                media = data["media"]
                if media.get("track") == "inbound":
                    inbuffer.extend(base64.b64decode(media["payload"]))
                    while len(inbuffer) >= BUFFER_SIZE:
                        audio_queue.put_nowait(bytes(inbuffer[:BUFFER_SIZE]))
                        del inbuffer[:BUFFER_SIZE]

            elif event == "stop":
                break
        except Exception as e:
            print(f"twilio_receiver error: {e}")
            break


async def twilio_ws_handler(request):

    # Reconstruct the exact public WebSocket URL
    signature = request.headers.get("X-Twilio-Signature", "")
    base = PUBLIC_BASE_URL + str(request.rel_url)
    wss = base.replace("https://", "wss://", 1)

    # Try every URL form Twilio might have signed (all use the same auth token)
    candidates = [wss, wss + "/", base, base + "/"]
    matched = next((u for u in candidates if validate_twilio_request(u, {}, signature)), None)

    if not matched:
        print(f"Rejected unauthorized WebSocket connection (signature present: {bool(signature)})")
        return web.Response(status=403, text="Forbidden")
    print(f"WebSocket signature OK with: {matched}")

    # Only authenticated connections reach this point
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    print("Authenticated Twilio WebSocket connected")

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()
    call_ctx = {}

    async def twilio_ws_send(text):
        await ws.send_str(text)

    async with sts_connect() as sts_ws:
        await sts_ws.send(json.dumps(load_config()))

        tasks = [
            asyncio.create_task(
                sts_sender(sts_ws, audio_queue)
            ),
            asyncio.create_task(
                sts_receiver(
                    sts_ws,
                    twilio_ws_send,
                    streamsid_queue,
                    call_ctx
                )
            ),
            asyncio.create_task(
                twilio_receiver(
                    ws,
                    audio_queue,
                    streamsid_queue,
                    call_ctx
                )
            ),
        ]

        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED
        )

        for t in pending:
            t.cancel()

        await asyncio.gather(*pending, return_exceptions=True)

        for t in done:
            if not t.cancelled() and t.exception():
                print(f"Task ended with error: {t.exception()}")

    print("Twilio WebSocket closed")
    return ws


# =========================================================
# INBOUND CALL: Twilio hits /twiml when a customer dials your number
# =========================================================

async def twiml_handler(request):

    # Only accept POST requests
    if request.method != "POST":
        return web.Response(status=405, text="Method Not Allowed")

    # Get all POST parameters
    data = await request.post()

    # Use the configured public URL, not the internal Railway URL
    signature_url = PUBLIC_BASE_URL + str(request.rel_url)

    signature = request.headers.get("X-Twilio-Signature", "")

    # Verify that the request genuinely came from Twilio
    if not validate_twilio_request(signature_url, data, signature):
        print("Rejected invalid Twilio webhook signature")
        return web.Response(status=403, text="Forbidden")

    caller = data.get("From", "")

    ws_url = PUBLIC_BASE_URL.replace(
        "https://", "wss://", 1
    ) + "/media-stream"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url={quoteattr(ws_url)}>
            <Parameter name="from" value={quoteattr(caller)} />
        </Stream>
    </Connect>
</Response>"""

    return web.Response(text=xml, content_type="text/xml")


async def health(request):
    return web.Response(text="ok")


def create_app():
    app = web.Application()
    app.router.add_get("/media-stream", twilio_ws_handler)
    app.router.add_post("/twiml", twiml_handler)
    # app.router.add_get("/twiml", twiml_handler)
    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    if not PUBLIC_BASE_URL:
        raise Exception("PUBLIC_BASE_URL not set in .env (https URL, no trailing slash)")
    if not PUBLIC_BASE_URL.startswith("https://"):
        raise Exception("PUBLIC_BASE_URL must use HTTPS")
    if not TWILIO_AUTH_TOKEN:
        raise Exception("TWILIO_AUTH_TOKEN not set in .env")

    init_db()  # creates tables and seeds the sample menu on first run

    port = int(os.getenv("PORT", 8080))
    print(f"Starting server on port {port}. Public URL: {PUBLIC_BASE_URL}")
    web.run_app(create_app(), host="0.0.0.0", port=port)