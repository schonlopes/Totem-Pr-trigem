//
// script.js (drop-in replacement with WebSocket weight listener)
// Keeps your Serial logic intact and adds a WS client to auto-fill screen 5 (PESO).
//
// =========================
// Navegação e progresso
// =========================
let currentScreen = 1;
const totalScreens = 17;

function updateProgress() {
  const progress = ((currentScreen - 1) / (totalScreens - 1)) * 100;
  const bar = document.getElementById('progress');
  if (bar) bar.style.width = progress + "%";
}

function showScreen(num) {
  const old = document.getElementById(`screen${currentScreen}`);
  const neu = document.getElementById(`screen${num}`);
  if (old) old.classList.remove('active');
  currentScreen = num;
  if (neu) neu.classList.add('active');
  updateProgress();
}

// Mantém compatível com seu HTML
function nextScreen(screenNumber) {
  showScreen(screenNumber);
  MeasurementController.onScreenChange(screenNumber);
}

// Inicializa barra de progresso
updateProgress();

// =========================
// Mapeamentos / UI
// =========================
const ScreenKeyMap = {
  5:  "PESO",   // sem comando por enquanto (balança BT depois)
  6:  "ALTURA",
  8:  "HR",
  9:  "SPO2",
  10: "TEMP",
  13: "GSR",
};

const KeySelectorMap = {
  "PESO":   "#pesoValue,   #screen5 .result",
  "ALTURA": "#alturaValue, #screen6 .result",
  "HR":     "#hrValue,     #screen8 .result",
  "SPO2":   "#spo2Value,   #screen9 .result",
  "TEMP":   "#tempValue,   #screen10 .result",
  "GSR":    "#gsrValue,    #screen13 .result",
};

const KeyRanges = {
  "HR":     [30, 220],
  "SPO2":   [50, 100],
  "TEMP":   [30, 43],
  "GSR":    [0, 4095],
  "ALTURA": [40, 250],
  "PESO":   [2, 300],
};

function clamp(n, min, max) { return Math.max(min, Math.min(n, max)); }

// =========================
// Medição com Web Serial
// =========================
const MeasurementController = (function(){
  let port, reader, serialConnected = false;
  let buffer = "";
  let measuringKey = null;       // chave aguardada (ex.: "ALTURA")
  let lastValueByKey = {};       // última leitura válida por chave
  let measuringActive = false;   // se atualiza a UI
  let lockOnFirstValid = true;   // trava na 1ª válida

  async function connectSerial() {
    try {
      if (!("serial" in navigator)) {
        setPortStatus("Seu navegador não suporta Web Serial. Use Chrome/Edge desktop.");
        return;
      }
      port = await navigator.serial.requestPort();
      await port.open({ baudRate: 115200 });

      const textDecoder = new TextDecoderStream();
      port.readable.pipeTo(textDecoder.writable);
      const inputStream = textDecoder.readable;
      reader = inputStream.getReader();

      serialConnected = true;
      setPortStatus("Conectado");
      readLoop();
    } catch (e) {
      console.error(e);
      setPortStatus("Falha na conexão");
    }
  }

  async function readLoop() {
    while (serialConnected) {
      try {
        const { value, done } = await reader.read();
        if (done) break;
        if (value) {
          buffer += value;
          let lines = buffer.split(/\r?\n/);
          buffer = lines.pop();
          for (const lineRaw of lines) {
            const line = lineRaw.trim();
            if (line) handleSensorLine(line);
          }
        }
      } catch (e) {
        console.error("Erro na leitura serial:", e);
        break;
      }
    }
  }

  function setPortStatus(txt) {
    const el = document.getElementById("portStatus");
    if (el) el.innerText = txt;
  }

  function setUIWaitingFor(key) {
    const sel = KeySelectorMap[key];
    if (!sel) return;
    const el = document.querySelector(sel);
    if (el) el.innerText = "Aguardando…";
  }

  function setUIValue(key, valText) {
    const sel = KeySelectorMap[key];
    if (!sel) return;
    const el = document.querySelector(sel);
    if (el) el.innerText = valText;
  }

  function validByRange(key, val) {
    const r = KeyRanges[key];
    if (!r) return true;
    return val >= r[0] && val <= r[1];
  }

  function formatValue(key, val) {
    if (!Number.isFinite(val)) return "—";
    if (key === "HR")     return `${Math.round(val)} bpm`;
    if (key === "SPO2")   return `${val.toFixed(0)} %`;
    if (key === "TEMP")   return `${val.toFixed(1)} °C`;
    if (key === "GSR")    return `${Math.round(val)}`;
    if (key === "ALTURA") return `${val.toFixed(1)} cm`;
    if (key === "PESO")   return `${val.toFixed(1)} kg`;
    return String(val);
  }

  // ---- ENVIO DE COMANDOS AO ARDUINO (novo) ----
  async function sendCommandRaw(s) {
    try {
      if (!port || !port.writable) return;
      const data = new TextEncoder().encode(s.endsWith("\n") ? s : s + "\n");
      const w = port.writable.getWriter();
      await w.write(data);
      w.releaseLock();
    } catch (e) {
      console.warn("Falha ao enviar comando:", s, e);
    }
  }

  async function sendCommandForKey(key) {
    if (key === "HR" || key === "SPO2") return sendCommandRaw("HR_SPO2");
    if (key === "ALTURA") return sendCommandRaw("ALTURA");
    if (key === "TEMP")   return sendCommandRaw("TEMP");
    if (key === "GSR")    return sendCommandRaw("GSR");
    // PESO: sem envio; virá via Bluetooth no futuro.
  }

  // Aceita números OU NA/OUT e trata re-tentativa
  function handleSensorLine(line) {
    const m = line.match(/^([A-ZÇÃÉÊÍÓÚ0-9_]+)\s*:\s*([\-0-9\.]+|NA|OUT)$/i);
    if (!m) return;

    const key = m[1].toUpperCase();
    const valueToken = m[2].toUpperCase();

    if (valueToken === "NA" || valueToken === "OUT") {
      if (measuringActive && measuringKey === key) {
        setUIValue(key, "—");
        setTimeout(() => sendCommandForKey(key), 1000); // tenta mais 1 vez
      }
      return;
    }

    const val = parseFloat(valueToken);
    if (!Number.isFinite(val)) return;

    if (validByRange(key, val)) {
      lastValueByKey[key] = val;
    }

    if (measuringActive && measuringKey === key && validByRange(key, val)) {
      setUIValue(key, formatValue(key, clamp(val, KeyRanges[key][0], KeyRanges[key][1])));
      if (lockOnFirstValid) measuringActive = false;
    }
  }

  function onScreenChange(screenNumber) {
    const key = ScreenKeyMap[screenNumber] || null;
    measuringKey = key;
    if (!key) { measuringActive = false; return; }

    setUIWaitingFor(key);
    measuringActive = true;

    // Dispara o comando certo ao entrar na tela (novo)
    sendCommandForKey(key);
  }

  function setLockOnFirstValid(flag) { lockOnFirstValid = !!flag; }

  return { connectSerial, onScreenChange, setLockOnFirstValid };
})();

// Botão "Conectar Arduino"
document.getElementById("btnConnect")?.addEventListener("click", () => {
  MeasurementController.connectSerial();
});

// Eventos de (des)conexão do navegador
navigator.serial?.addEventListener("connect", () => {
  const el = document.getElementById("portStatus");
  if (el) el.innerText = "Porta disponível – clique para conectar";
});
navigator.serial?.addEventListener("disconnect", () => {
  const el = document.getElementById("portStatus");
  if (el) el.innerText = "Desconectado";
});


;(() => {
  // =========================
  // WebSocket (peso S200)
  // =========================
  const WS_URL = "ws://127.0.0.1:8765";
  let ws, wsReady = false;
  let lastStableSent = null;

  function connectWS() {
    try {
      ws = new WebSocket(WS_URL);
      ws.addEventListener("open", () => { wsReady = true; console.log("[WS] conectado"); });
      ws.addEventListener("close", () => { wsReady = false; console.log("[WS] fechado"); setTimeout(connectWS, 1500); });
      ws.addEventListener("error", () => { wsReady = false; });
      ws.addEventListener("message", (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "status" && msg.msg === "reset") {
            if (currentScreen === 5) setPesoUI("Aguardando…");
            lastStableSent = null;
          }
          if (msg.type === "weight") {
            onWeightFromWS(msg.kg, !!msg.stable);
          }
        } catch {}
      });
    } catch (e) {
      console.warn("WS falhou:", e);
    }
  }

  function setPesoUI(text) {
    const el = document.querySelector("#pesoValue, #screen5 .result");
    if (el) el.innerText = text;
  }

  function onWeightFromWS(kg, stable) {
    if (currentScreen !== 5) return; // só atua na tela PESO
    // mostra leitura ao vivo
    if (!stable) setPesoUI(`${kg.toFixed(2)} kg`);

    // trava e avança quando estabilizar (1ª vez)
    if (stable && lastStableSent !== kg) {
      lastStableSent = kg;
      setPesoUI(`${kg.toFixed(2)} kg`);
      // opcional: aguarda 1s e avança
      setTimeout(() => {
        // mantém consistência com sua navegação
        if (currentScreen === 5) nextScreen(6);
      }, 1000);
    }
  }

  // inicia conexão imediatamente
  connectWS();

  // também re-mostra "Aguardando…" quando entrar na tela 5
  const _origOnScreenChange = MeasurementController.onScreenChange;
  MeasurementController.onScreenChange = (n) => {
    _origOnScreenChange(n);
    if (n === 5) {
      setPesoUI("Aguardando…");
    }
  };
})();
