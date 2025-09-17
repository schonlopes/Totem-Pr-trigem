# balanca_s200.py
# Xiaomi Smart Scale S200: decifra FE95 (MiBeacon), extrai peso de plain[4:6]/100 (kg)
# Reqs: pip install bleak==0.22.3 pycryptodome requests

import asyncio
from binascii import hexlify
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, Tuple

import requests
from bleak import BleakScanner
from Crypto.Cipher import AES  # pycryptodome

# ===================== CONFIG DO DISPOSITIVO =====================
TARGET_MAC  = "D0:7B:6F:30:C7:6A"                          # MAC da sua S200
BINDKEY_HEX = "23d2c50a6b924c310d598de1f9781fe2"           # bindkey que você extraiu
# ================================================================

# ======== ENVIO PARA PLANILHA (opcional) ========
SHEETS_URL = None  # coloque sua URL de webhook Apps Script aqui para habilitar
TURMA = "BALANCA"
NUMERO = "000"
# =================================================

FE95_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
KEY = bytes.fromhex(BINDKEY_HEX.lower())

# janela de estabilização
WINDOW = deque(maxlen=3)
TOL_KG = 0.1
AUTO_RESET_SECS = 20
already_sent = False
last_seen = None

def post_to_sheet(weight_kg: float):
    if not SHEETS_URL:
        return
    payload = {
        "turma": TURMA,
        "numero": NUMERO,
        "nota": f"{weight_kg:.2f}",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "tipo": "mi-scale-s200"
    }
    try:
        r = requests.post(SHEETS_URL, json=payload, timeout=6)
        r.raise_for_status()
        print(f"📤 Enviado para planilha: {payload}")
    except Exception as e:
        print("⚠️ Falha ao enviar para planilha:", e)

def weights_stable(values, tol=TOL_KG):
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

def le_u16(b: bytes) -> int:
    return int.from_bytes(b, "little")

def has_embedded_mac(service: bytes, mac_rev: bytes) -> bool:
    return len(service) >= 11 and service[5:11] == mac_rev

def split_encrypted_block(service: bytes, mac_rev: bytes) -> Tuple[bytes, bytes, bytes, int]:
    """
    Retorna (cipherpayload, tag, payload_counter, enc_start)
    Detecta automaticamente se há MAC embutida.
    Layout:
      [0:2]  frame_ctrl (LE)
      [2:4]  product_id (LE)
      [4]    frame_cnt
      [5:11] mac_rev? (se presente)
      [enc]  cipher ... + counter(3) + tag(4)
    """
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
    """
    Decifra service_data FE95 (MiBeacon) com KEY.
    Nonce = mac_rev(6) + product_id(2, LE) + frame_cnt(1) + payload_counter(3)
    AAD = 0x11
    """
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
    """
    Para a S200 do usuário, o peso está em plain[4:6] e divide por 100 (kg).
    Ex.: b8 29 -> 0x29b8 = 10680 -> 106.80 kg
    """
    if len(plain) < 6:
        return None
    raw = le_u16(plain[4:6])
    kg = raw / 100.0
    # sanity check: 20–300 kg
    if 5.0 <= kg <= 300.0:
        return round(kg, 2)
    return None

def pretty_hex(b: bytes) -> str:
    return hexlify(b).decode()

def handle_service_data(service: bytes, mac: str, rssi: int):
    global already_sent, last_seen

    if len(service) <= 11:
        # ignorar keepalive curto
        return

    last_seen = datetime.now()
    frame_ctrl = le_u16(service[0:2]) if len(service) >= 2 else None
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
    print(f"   🔎 Leitura: {w:.2f} kg | últimas={list(WINDOW)}")

    if not already_sent and weights_stable(WINDOW):
        stable = sum(WINDOW) / len(WINDOW)
        print(f"   ✅ Peso estabilizado: {stable:.2f} kg")
        post_to_sheet(stable)
        already_sent = True

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

async def main():
    print("🔍 Escutando FE95 da S200… (pacotes len=11 são ignorados)")
    print("👣 Pise até travar o visor. Feche Mi Home/Zepp Life. Aproxime o PC da balança.")
    scanner = BleakScanner(on_detection)
    await scanner.start()
    try:
        await asyncio.Event().wait()
    finally:
        await scanner.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Encerrado pelo usuário.")
