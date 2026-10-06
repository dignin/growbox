// Kept as an external file so the Content-Security-Policy can forbid inline scripts.
(() => {
  const img = document.getElementById("video");
  const overlay = document.getElementById("overlay");
  const state = document.getElementById("state");
  const dot = document.getElementById("dot");
  const pauseBtn = document.getElementById("pause");
  const streamUrl = img.getAttribute("src");
  let paused = false;
  let cameraOnline = null;
  let retryTimer = null;

  function setState(text, live) {
    state.textContent = text;
    dot.classList.toggle("live", live);
  }

  function showOverlay(text) {
    overlay.textContent = text;
    overlay.hidden = !text;
  }

  function connect() {
    clearTimeout(retryTimer);
    img.src = streamUrl + "?t=" + Date.now();
  }

  // The stream ends when the home camera goes offline; /status tells us when to reconnect.
  async function checkStatus() {
    try {
      const r = await fetch("status", { redirect: "manual", cache: "no-store" });
      if (r.type === "opaqueredirect") { location.href = "login"; return; } // session expired
      const s = await r.json();
      if (s.camera_online && cameraOnline === false && !paused) connect();
      cameraOnline = s.camera_online;
    } catch {
      cameraOnline = false;
    }
    if (paused) return;
    if (cameraOnline) {
      showOverlay("");
      setState("live", true);
    } else {
      showOverlay("Camera offline — waiting for the home connection…");
      setState("offline", false);
    }
  }

  img.addEventListener("error", () => {
    if (paused) return;
    retryTimer = setTimeout(connect, 3000);
  });

  pauseBtn.addEventListener("click", () => {
    paused = !paused;
    if (paused) {
      img.removeAttribute("src"); // closes the connection; camera turns off if nobody else watches
      setState("paused", false);
      pauseBtn.textContent = "Resume";
    } else {
      pauseBtn.textContent = "Pause";
      connect();
      checkStatus();
    }
  });

  document.getElementById("fullscreen").addEventListener("click", () => {
    const el = document.getElementById("frame");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (el.requestFullscreen) el.requestFullscreen();
    else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
  });

  const clock = document.getElementById("clock");
  setInterval(() => { clock.textContent = new Date().toLocaleString(); }, 1000);
  setInterval(checkStatus, 3000);
  checkStatus();
})();
