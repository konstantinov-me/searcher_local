// Минимальный UI: поиск + live-прогресс индексации по WebSocket.
const $ = (s) => document.querySelector(s);

async function search(e) {
  e.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return;
  const level = $("#level").value;
  $("#meta").textContent = "ищем…";
  const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&level=${level}&limit=50`);
  const data = await r.json();
  $("#meta").textContent = `${data.count} результатов · уровень ${data.level}`;
  const ul = $("#results");
  ul.innerHTML = "";
  for (const it of data.results) {
    const li = document.createElement("li");
    li.innerHTML = `<a href="#" class="open">${esc(it.name)}</a>
      <span class="src">${it.source.split(":").pop()}</span>
      <span class="score">${it.score}</span>
      <div class="path">${esc(it.path)}</div>
      <div class="snip">${it.snippet}</div>`;
    li.querySelector(".open").onclick = (ev) => { ev.preventDefault(); openFile(it.file_id); };
    ul.appendChild(li);
  }
}

async function openFile(id) {
  const d = await (await fetch(`/api/file/${id}`)).json();
  alert(`${d.path}\n\n${(d.preview || "").slice(0, 500)}`);
}

function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/progress`);
  ws.onmessage = (m) => {
    const p = JSON.parse(m.data);
    $("#progress").textContent = `индексация: ✔${p.indexed} ✖${p.errors} ⏭${p.skipped}`;
  };
  ws.onclose = () => setTimeout(connectWs, 3000);
}

async function loadStatus() {
  const s = await (await fetch("/api/status")).json();
  $("#backend").textContent = `бэкенд: ${s.backend}`;
}

$("#f").addEventListener("submit", search);
loadStatus();
connectWs();
