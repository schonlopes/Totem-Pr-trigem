
# balanca_bridge.py
# Xiaomi Smart Scale S200 → (local) WebSocket → Browser
# Reqs: pip install bleak==0.22.3 pycryptodome websockets
#
# How it works:
# - Listens to FE95 (MiBeacon) from your S200 (same logic as your model code)
# - When a weight packet is decrypted, streams live readings over ws://127.0.0.1:8765
# - When the weight stabilizes, broadcasts {"type":"weight","kg":X,"stable":true}
#
# Open your index.html in the browser. The provided script.js patch auto-connects
# to the local WebSocket and fills the weight on screen 5 ("PESO") at the right time.
#
import asyncio
import json
from binascii import hexlify
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, Tuple, Set

from bleak import BleakScanner
from Crypto.Cipher import AES  # pycryptodome
import websockets

# ===================== CONFIG DO DISPOSITIVO =====================
TARGET_MAC  = "D0:7B:6F:30:C7:6A"                          # MAC da sua S200
BINDKEY_HEX = "23d2c50a6b924c310d598de1f9781fe2"           # bindkey que você extraiu
# ================================================================

# ===================== CONFIG DO WEBSOCKET =======================
WS_HOST = "127.0.0.1"
WS_PORT = 8765
# ================================================================

FE95_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
KEY = bytes.fromhex(BINDKEY_HEX.lower())

# janela de estabilização
WINDOW = deque(maxlen=3)
TOL_KG = 0.1
AUTO_RESET_SECS = 20
already_sent = False
last_seen = None

# Conexões WebSocket ativas
CLIENTS: Set[websockets.WebSocketServerProtocol] = set()

# -------------------- Utils BLE --------------------
def le_u16(b: bytes) -> int:
    return int.from_bytes(b, "little")

def pretty_hex(b: bytes) -> str:
    return hexlify(b).decode()

def has_embedded_mac(service: bytes, mac_rev: bytes) -> bool:
    return len(service) >= 11 and service[5:11] == mac_rev

def split_encrypted_block(service: bytes, mac_rev: bytes):
    if len(service) < 5 + 7:
        raise ValueError("service_data muito curto")
    enc_start = 11 if has_embedded_mac(service, mac_rev) else 5
    enc = service[enc_start:]
    if len(enc) < 7:
        raise ValueError("encrypted_payload muito curto")
    tag = enc[-4:]
    payload_counter = enc[-7:-4]
    cipherpayload = enc[:-7]
    return cipherpayload, tag, payload_counter, enc_start

def decrypt_mibeacon(service: bytes, mac_str: str) -> Optional[bytes]:
    if len(service) < 5:
        return None
    mac_rev = bytes.fromhex(mac_str.replace(":", ""))[::-1]
    pid_le = service[2:4]
    frame_cnt = service[4:5]
    try:
        cipherpayload, tag, payload_counter, _ = split_encrypted_block(service, mac_rev)
    except Exception:
        return None
    nonce = mac_rev + pid_le + frame_cnt + payload_counter
    try:
        cipher = AES.new(KEY, AES.MODE_CCM, nonce=nonce, mac_len=4)
        cipher.update(b"\x11")
        return cipher.decrypt_and_verify(cipherpayload, tag)
    except Exception:
        return None

def extract_weight_from_plain(plain: bytes) -> Optional[float]:
    if len(plain) < 6:
        return None
    raw = le_u16(plain[4:6])
    kg = raw / 100.0
    if 5.0 <= kg <= 150.0:
        return kg
    return None

def weights_stable(values: deque, tol=TOL_KG) -> bool:
    if len(values) < values.maxlen:
        return False
    return (max(values) - min(values)) <= tol

def maybe_autoreset():
    global already_sent, last_seen, WINDOW
    if last_seen and (datetime.now() - last_seen) > timedelta(seconds=AUTO_RESET_SECS):
        if already_sent or len(WINDOW) > 0:
            print("🔁 Janela resetada (novo ciclo de pesagem).")
        already_sent = False
        WINDOW.clear()
        last_seen = None
        # avisa browser para limpar UI de PESO
        asyncio.create_task(broadcast({"type":"status","msg":"reset"}))

# -------------------- WebSocket --------------------
async def broadcast(msg: dict):
    if not CLIENTS:
        return
    data = json.dumps(msg)
    dead = set()
    for ws in CLIENTS:
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    for ws in dead:
        CLIENTS.discard(ws)

async def ws_handler(websocket):
    CLIENTS.add(websocket)
    try:
        await websocket.send(json.dumps({"type":"status","msg":"connected"}))
        async for _ in websocket:
            # we don't expect messages from the browser; it's a push channel
            pass
    finally:
        CLIENTS.discard(websocket)

# -------------------- BLE pipeline --------------------
def handle_service_data(service: bytes, mac: str, rssi: int):
    global already_sent, last_seen

    if len(service) <= 11:
        return  # keepalive curto

    last_seen = datetime.now()
    product_id = le_u16(service[2:4]) if len(service) >= 4 else None
    frame_cnt = service[4] if len(service) >= 5 else None

    plain = decrypt_mibeacon(service, mac)
    print(f"📶 FE95 {mac} | RSSI {rssi} | len={len(service)} | {pretty_hex(service)}")
    if plain is None:
        print("  ↳ ❌ Não foi possível decifrar (provavelmente não é pacote de peso).")
        return

    print(f"   🔓 plain = {pretty_hex(plain)}  (pid=0x{product_id:04x}, cnt={frame_cnt})")
    w = extract_weight_from_plain(plain)
    if w is None:
        print("   ℹ️  Payload decifrado não contém peso no offset esperado.")
        return

    WINDOW.append(w)
    asyncio.create_task(broadcast({"type":"weight","kg":w,"stable":False}))
    print(f"   🔎 Leitura: {w:.2f} kg | últimas={list(WINDOW)}")

    if not already_sent and weights_stable(WINDOW):
        stable = round(sum(WINDOW) / len(WINDOW), 2)
        print(f"   ✅ Peso estabilizado: {stable:.2f} kg")
        already_sent = True
        asyncio.create_task(broadcast({"type":"weight","kg":stable,"stable":True}))

def on_detection(device, adv):
    if device.address.upper() != TARGET_MAC.upper():
        return
    maybe_autoreset()
    sd = adv.service_data or {}
    raw = sd.get(FE95_UUID)
    if not raw:
        return
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif not isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw)
        except Exception:
            return
    handle_service_data(bytes(raw), device.address, adv.rssi)

async def run_ble_scanner():
    print("🔍 Escutando FE95 da S200… (ignore pacotes len=11)")
    print("👣 Pise até travar o visor. Feche Mi Home/Zepp Life. Aproxime o PC da balança.")
    scanner = BleakScanner(on_detection)
    await scanner.start()
    try:
        await asyncio.Event().wait()
    finally:
        await scanner.stop()

async def main():
    ws_server = await websockets.serve(ws_handler, WS_HOST, WS_PORT)
    print(f"🌐 WebSocket em ws://{WS_HOST}:{WS_PORT}")
    try:
        await run_ble_scanner()
    finally:
        ws_server.close()
        await ws_server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Encerrado pelo usuário.")
