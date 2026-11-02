const socket = io({ transports: ["polling"] });
const CURRENT_LAB = window.CURRENT_LAB || "ansible";
const LAB_HOSTS = window.CURRENT_LAB_HOSTS || {
  ansible: ["ansible-controller", "ansible-client"],
  jenkins: ["jenkins-controller", "jenkins-agent"],
  bash: ["bash-server"],
};
const HOSTS = LAB_HOSTS[CURRENT_LAB] || LAB_HOSTS.ansible;
const terminalElement = document.getElementById("terminal");
const terminalState = document.getElementById("terminalState");
const hostStatus = document.getElementById("hostStatus");
let activeContainer = HOSTS[0];
const terminals = {};
const fitAddons = {};
const terminalSessions = {};
const pollTimers = {};
const inputQueues = {};
const inputFlushTimers = {};
const pollingInProgress = {};
const terminalOutputBuffers = {};
let resizing = false;
let expiryTimestamp = null;
let statusRefreshInProgress = false;
let labCreatedLocally = false;
const DEFAULT_LAB_DURATION_SECONDS = 3600;
const INPUT_FLUSH_DELAY_MS = 5;
const TERMINAL_POLL_INTERVAL_MS = 50;
const MAX_TERMINAL_OUTPUT_CHUNK = 65536;
const labEndpoint = (path) => `/api/${CURRENT_LAB}${path}`;
async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({
    success: false,
    error: `HTTP ${response.status}`,
  }));
  if (!response.ok || data.success === false) {
    throw new Error(
      data.error || data.stderr || data.stdout || `HTTP ${response.status}`,
    );
  }
  return data;
}
function setLoading(button, loading, text = "Working...") {
  if (!button) return;
  if (loading) {
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.textContent;
    }
    button.textContent = text;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    delete button.dataset.originalText;
    button.disabled = false;
  }
}
function createLocalTerminal(container) {
  if (terminals[container]) return;
  const term = new Terminal({
    cursorBlink: true,
    convertEol: true,
    scrollback: 5000,
    fontSize: 13,
    fontFamily: "Consolas, 'Courier New', monospace",
    allowProposedApi: true,
    theme: {
      background: "#080d18",
      foreground: "#e5edf7",
      cursor: "#82aaff",
    },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  terminals[container] = term;
  fitAddons[container] = fit;
  inputQueues[container] = "";
  terminalOutputBuffers[container] = "";
  pollingInProgress[container] = false;
  term.onData((data) => {
    if (!terminalSessions[container]) return;
    inputQueues[container] = (inputQueues[container] || "") + data;
    if (!inputFlushTimers[container]) {
      inputFlushTimers[container] = setTimeout(() => {
        inputFlushTimers[container] = null;
        flushTerminalInput(container);
      }, INPUT_FLUSH_DELAY_MS);
    }
  });
  term.onResize((size) => {
    const sessionId = terminalSessions[container];
    if (!sessionId) return;
    fetch(`/api/terminal/${sessionId}/resize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cols: size.cols,
        rows: size.rows,
      }),
    }).catch(() => {});
  });
}
async function flushTerminalInput(container) {
  const sessionId = terminalSessions[container];
  if (!sessionId || !inputQueues[container]) return;
  const data = inputQueues[container];
  inputQueues[container] = "";
  try {
    const response = await fetch(`/api/terminal/${sessionId}/write`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data }),
    });
    if (!response.ok) {
      throw new Error(`Terminal input failed: HTTP ${response.status}`);
    }
  } catch (_) {
    if (terminalSessions[container] === sessionId) {
      inputQueues[container] = data + (inputQueues[container] || "");
    }
  }
  if (inputQueues[container] && terminalSessions[container]) {
    if (!inputFlushTimers[container]) {
      inputFlushTimers[container] = setTimeout(() => {
        inputFlushTimers[container] = null;
        flushTerminalInput(container);
      }, INPUT_FLUSH_DELAY_MS);
    }
  }
}
function writeTerminalOutput(container, output) {
  const term = terminals[container];
  if (!term || !output) return;
  terminalOutputBuffers[container] =
    (terminalOutputBuffers[container] || "") + output;
  if (terminalOutputBuffers[container].length >= MAX_TERMINAL_OUTPUT_CHUNK) {
    const chunk = terminalOutputBuffers[container];
    terminalOutputBuffers[container] = "";
    term.write(chunk);
  } else {
    const buffer = terminalOutputBuffers[container];
    terminalOutputBuffers[container] = "";
    if (buffer) {
      term.write(buffer);
    }
  }
}
function showActiveTerminal() {
  if (!terminalElement) return;
  terminalElement.replaceChildren();
  createLocalTerminal(activeContainer);
  terminals[activeContainer].open(terminalElement);
  try {
    fitAddons[activeContainer].fit();
  } catch (_) {}
  if (terminalSessions[activeContainer]) {
    terminalState.textContent = "Connected";
    terminalState.className = "terminal-state online";
  } else {
    terminalState.textContent = "Not connected";
    terminalState.className = "terminal-state";
  }
}
async function connectTerminal(container) {
  createLocalTerminal(container);
  if (terminalSessions[container]) return;
  try {
    const data = await api(labEndpoint("/terminal/open"), {
      method: "POST",
      body: JSON.stringify({ container }),
    });
    terminalSessions[container] = data.session_id;
    if (container === activeContainer) {
      terminalState.textContent = "Connected";
      terminalState.className = "terminal-state online";
    }
    if (pollTimers[container]) {
      clearTimeout(pollTimers[container]);
    }
    pollTerminal(container);
    resizeActiveTerminal();
  } catch (_) {
    if (container === activeContainer) {
      terminalState.textContent = "Connection failed";
      terminalState.className = "terminal-state error";
    }
  }
}
async function pollTerminal(container) {
  const sessionId = terminalSessions[container];
  if (!sessionId || !terminals[container] || pollingInProgress[container]) {
    return;
  }
  pollingInProgress[container] = true;
  try {
    const response = await fetch(`/api/terminal/${sessionId}/read`, {
      cache: "no-store",
    });
    const data = await response.json();
    if (data.output) {
      writeTerminalOutput(container, data.output);
    }
    if (data.closed || !response.ok) {
      if (pollTimers[container]) {
        clearTimeout(pollTimers[container]);
        delete pollTimers[container];
      }
      delete terminalSessions[container];
      if (container === activeContainer) {
        terminalState.textContent = "Session closed";
        terminalState.className = "terminal-state";
      }
      return;
    }
  } catch (_) {
    // Retry after transient network errors.
  } finally {
    pollingInProgress[container] = false;
  }
  if (terminalSessions[container] === sessionId) {
    pollTimers[container] = setTimeout(
      () => pollTerminal(container),
      TERMINAL_POLL_INTERVAL_MS,
    );
  }
}
async function closeTerminalSession(container) {
  const sessionId = terminalSessions[container];
  if (pollTimers[container]) {
    clearTimeout(pollTimers[container]);
    delete pollTimers[container];
  }
  if (inputFlushTimers[container]) {
    clearTimeout(inputFlushTimers[container]);
    delete inputFlushTimers[container];
  }
  inputQueues[container] = "";
  terminalOutputBuffers[container] = "";
  if (sessionId) {
    try {
      await fetch(`/api/terminal/${sessionId}`, {
        method: "DELETE",
      });
    } catch (_) {}
  }
  delete terminalSessions[container];
}
function resizeActiveTerminal() {
  if (resizing) return;
  const fit = fitAddons[activeContainer];
  if (!fit) return;
  try {
    fit.fit();
    terminals[activeContainer].scrollToBottom();
  } catch (_) {}
}
function setActiveHost(container) {
  if (!HOSTS.includes(container)) return;
  activeContainer = container;
  document.querySelectorAll(".host-item").forEach((button) => {
    button.classList.toggle("selected", button.dataset.container === container);
  });
  const title = document.getElementById("activeHostTitle");
  const subtitle = document.getElementById("activeHostSubtitle");
  const description = document.getElementById("terminalDescription");
  if (title) {
    title.textContent = container;
  }
  if (subtitle) {
    if (CURRENT_LAB === "ansible") {
      subtitle.textContent =
        container === "ansible-controller"
          ? "Ansible controller terminal"
          : "Managed client terminal";
    } else {
      subtitle.textContent = `${CURRENT_LAB.charAt(0).toUpperCase() + CURRENT_LAB.slice(1)} lab terminal`;
    }
  }
  if (description) {
    description.textContent = `Interactive terminal for ${container}`;
  }
  showActiveTerminal();
  connectTerminal(container);
}
function startCountdown(seconds) {
  const duration = Number(seconds);
  if (!Number.isFinite(duration) || duration <= 0) {
    clearCountdown();
    return;
  }
  expiryTimestamp = Date.now() + Math.ceil(duration) * 1000;
  labCreatedLocally = true;
  renderTimer();
}
function clearCountdown() {
  expiryTimestamp = null;
  labCreatedLocally = false;
  renderTimer();
}
function updateTimerFromStatus(data) {
  const serverSeconds = Number(data.remaining_seconds);
  if (
    data.remaining_seconds !== null &&
    data.remaining_seconds !== undefined &&
    data.remaining_seconds !== "" &&
    Number.isFinite(serverSeconds) &&
    serverSeconds > 0
  ) {
    const serverExpiry = Date.now() + serverSeconds * 1000;
    if (
      expiryTimestamp === null ||
      Math.abs(expiryTimestamp - serverExpiry) > 3000
    ) {
      expiryTimestamp = serverExpiry;
    }
    labCreatedLocally = true;
    renderTimer();
    return;
  }
  if (labCreatedLocally && expiryTimestamp !== null) {
    renderTimer();
    return;
  }
  if (data.running && expiryTimestamp === null) {
    startCountdown(DEFAULT_LAB_DURATION_SECONDS);
    return;
  }
  if (!data.running && !data.creating) {
    clearCountdown();
  }
}
async function refreshStatus() {
  if (statusRefreshInProgress) return;
  statusRefreshInProgress = true;
  try {
    try {
      const health = await api("/api/health");
      if (hostStatus) {
        hostStatus.textContent = `Connected · Docker ${health.docker_version}`;
        hostStatus.className = "status-pill online";
      }
    } catch (_) {
      if (hostStatus) {
        hostStatus.textContent = "Connection failed";
        hostStatus.className = "status-pill";
      }
    }
    const data = await api(labEndpoint("/status"));
    HOSTS.forEach((name, index) => {
      const item = data.containers?.[name] || {};
      const statusId =
        index === 0
          ? "controllerStatus"
          : index === 1
            ? "clientStatus"
            : `${name}Status`;
      const statusElement = document.getElementById(statusId);
      const button = document.querySelector(
        `.host-item[data-container="${name}"]`,
      );
      const dot = button?.querySelector(".host-dot");
      const isRunning = item.state === "running";
      if (statusElement) {
        statusElement.textContent = isRunning
          ? "Running"
          : item.state === "exited" || item.state === "dead"
            ? "Stopped"
            : item.status || "Not created";
      }
      if (dot) {
        dot.classList.toggle("running", isRunning);
        dot.classList.toggle(
          "stopped",
          !isRunning &&
            ["exited", "dead", "missing", ""].includes(item.state || ""),
        );
      }
    });
    updateTimerFromStatus(data);
    const createButton = document.getElementById("createClusterBtn");
    const stopButton = document.getElementById("stopClusterBtn");
    const destroyButton = document.getElementById("destroyClusterBtn");
    if (createButton && !createButton.dataset.originalText) {
      createButton.disabled = Boolean(data.creating);
    }
    if (stopButton) {
      stopButton.disabled = !data.running;
    }
    if (destroyButton) {
      destroyButton.disabled = !data.has_containers;
    }
  } catch (_) {
    renderTimer();
  } finally {
    statusRefreshInProgress = false;
  }
}
function renderTimer() {
  const timer = document.getElementById("clusterTimer");
  if (!timer) return;
  if (expiryTimestamp === null) {
    timer.textContent = "--:--:--";
    return;
  }
  const seconds = Math.max(0, Math.ceil((expiryTimestamp - Date.now()) / 1000));
  const hours = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const secs = String(seconds % 60).padStart(2, "0");
  timer.textContent = `${hours}:${minutes}:${secs}`;
  if (seconds === 0) {
    expiryTimestamp = null;
    labCreatedLocally = false;
    refreshStatus();
  }
}
document.querySelectorAll(".host-item").forEach((button) => {
  button.addEventListener("click", () => {
    setActiveHost(button.dataset.container);
  });
});
const hostsButton = document.getElementById("hostsButton");
const documentsButton = document.getElementById("documentsButton");
if (hostsButton && documentsButton) {
  hostsButton.addEventListener("click", () => {
    hostsButton.classList.add("selected");
    documentsButton.classList.remove("selected");
  });
  documentsButton.addEventListener("click", async () => {
    documentsButton.classList.add("selected");
    hostsButton.classList.remove("selected");
    await loadDocuments();
    document.getElementById("documentsPanel")?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  });
}
async function loadDocuments() {
  const select = document.getElementById("documentSelect");
  const content = document.getElementById("documentContent");
  if (!select || !content) return;
  try {
    const data = await api("/api/documents");
    select.replaceChildren();
    if (!data.documents.length) {
      content.textContent = "No documentation files found.";
      return;
    }
    data.documents.forEach((doc) => {
      const option = document.createElement("option");
      option.value = doc.name;
      option.textContent = doc.title || doc.name;
      select.appendChild(option);
    });
    function displaySelected() {
      const doc = data.documents.find((item) => item.name === select.value);
      content.innerHTML = doc
        ? markdownToHtml(doc.content)
        : "Select a document.";
    }
    select.onchange = displaySelected;
    displaySelected();
  } catch (error) {
    content.textContent = `Unable to load documents: ${error.message}`;
  }
}
function escapeHtml(text) {
  return String(text).replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&",
        "<": "<",
        ">": ">",
        '"': """,
        "'": "'",
      })[char],
  );
}
function markdownToHtml(markdown) {
  let html = escapeHtml(markdown);
  const blocks = [];
  html = html.replace(/```([^\n]*)\n([\s\S]*?)```/g, (_, language, code) => {
    const index = blocks.length;
    blocks.push(`<pre><code>${code}</code></pre>`);
    return `@@BLOCK${index}@@`;
  });
  html = html.replace(/^### (.*)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.*)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.*)$/gm, "<h1>$1</h1>");
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/^\s*-\s+(.+)$/gm, "<li>$1</li>");
  html = html.replace(/(?:<li>.*<\/li>\n?)+/g, (match) => `<ul>${match}</ul>`);
  html = html.replace(/\n{2,}/g, "</p><p>");
  html = html.replace(/\n/g, "<br>");
  html = `<p>${html}</p>`;
  blocks.forEach((block, index) => {
    html = html.replace(`@@BLOCK${index}@@`, block);
  });
  return html;
}
async function clusterAction(action) {
  const endpoint = labEndpoint(`/${action}`);
  const button = document.getElementById(
    action === "create"
      ? "createClusterBtn"
      : action === "stop"
        ? "stopClusterBtn"
        : "destroyClusterBtn",
  );
  if (
    action === "destroy" &&
    !confirm(`Destroy all ${CURRENT_LAB} lab hosts and its network?`)
  ) {
    return;
  }
  setLoading(button, true, action === "create" ? "Creating..." : "Working...");
  try {
    const payload = action === "create" ? { sid: socket.id } : undefined;
    await api(endpoint, {
      method: "POST",
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });
    if (action === "create") {
      startCountdown(DEFAULT_LAB_DURATION_SECONDS);
    }
    if (action === "stop" || action === "destroy") {
      clearCountdown();
    }
    await refreshStatus();
  } catch (error) {
    if (action === "create") {
      clearCountdown();
    }
    alert(error.message);
  } finally {
    setLoading(button, false);
    await refreshStatus();
  }
}
document
  .getElementById("createClusterBtn")
  ?.addEventListener("click", () => clusterAction("create"));
document
  .getElementById("stopClusterBtn")
  ?.addEventListener("click", () => clusterAction("stop"));
document
  .getElementById("destroyClusterBtn")
  ?.addEventListener("click", () => clusterAction("destroy"));
document.getElementById("refreshBtn")?.addEventListener("click", refreshStatus);
document
  .getElementById("reloadDocsBtn")
  ?.addEventListener("click", loadDocuments);
document.getElementById("startBtn")?.addEventListener("click", async () => {
  try {
    await api(
      labEndpoint(`/containers/${encodeURIComponent(activeContainer)}/start`),
      { method: "POST" },
    );
    await refreshStatus();
    await connectTerminal(activeContainer);
  } catch (error) {
    alert(error.message);
  }
});
document.getElementById("stopBtn")?.addEventListener("click", async () => {
  try {
    await api(
      labEndpoint(`/containers/${encodeURIComponent(activeContainer)}/stop`),
      { method: "POST" },
    );
    await closeTerminalSession(activeContainer);
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  }
});
document.getElementById("deleteBtn")?.addEventListener("click", async () => {
  if (!confirm(`Destroy ${activeContainer}?`)) return;
  try {
    await api(
      labEndpoint(`/containers/${encodeURIComponent(activeContainer)}`),
      { method: "DELETE" },
    );
    await closeTerminalSession(activeContainer);
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  }
});
const splitter = document.getElementById("splitter");
const terminalSection = document.querySelector(".terminal-section");
let dragY = 0;
let dragHeight = 0;
if (splitter && terminalSection) {
  splitter.addEventListener("pointerdown", (event) => {
    resizing = true;
    dragY = event.clientY;
    dragHeight = terminalSection.getBoundingClientRect().height;
    splitter.classList.add("dragging");
    splitter.setPointerCapture(event.pointerId);
  });
  splitter.addEventListener("pointermove", (event) => {
    if (!resizing) return;
    const height = Math.max(
      200,
      Math.min(window.innerHeight - 220, dragHeight + event.clientY - dragY),
    );
    const terminal = document.querySelector(".terminal");
    if (terminal) terminal.style.height = `${height - 60}px`;
    resizeActiveTerminal();
  });
  function finishResize() {
    resizing = false;
    splitter.classList.remove("dragging");
    resizeActiveTerminal();
  }
  splitter.addEventListener("pointerup", finishResize);
  splitter.addEventListener("pointercancel", finishResize);
}
window.addEventListener("resize", resizeActiveTerminal);
socket.on("cluster_complete", async (data) => {
  if (data.lab && data.lab !== CURRENT_LAB) return;
  if (data.success) {
    await refreshStatus();
    await connectTerminal(activeContainer);
  } else {
    clearCountdown();
    await refreshStatus();
  }
  alert(data.message || (data.success ? "Lab created." : "Creation failed."));
});
socket.on("cluster_expired", async (data) => {
  if (data.lab && data.lab !== CURRENT_LAB) return;
  clearCountdown();
  await refreshStatus();
  alert(data.message || "The lab has expired.");
});
socket.on("connect", refreshStatus);
createLocalTerminal(activeContainer);
showActiveTerminal();
refreshStatus();
setInterval(renderTimer, 1000);
setInterval(refreshStatus, 10000);
