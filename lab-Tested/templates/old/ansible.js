"use strict";

const socket = io();
const CURRENT_LAB = "ansible";
const HOSTS = ["ansible-controller", "ansible-client"];

const terminalElement = document.getElementById("terminal");
const terminalState = document.getElementById("terminalState");
const hostStatus = document.getElementById("hostStatus");

let activeContainer = HOSTS[0];
let expiryTimestamp = null;
let statusRefreshInProgress = false;
let labCreatedLocally = false;
let resizing = false;
let labReadyForTerminal = false;
let labBusyMessageActive = false;

const terminals = {};
const fitAddons = {};
const terminalSessions = {};
const pollTimers = {};
const inputQueues = {};
const inputFlushTimers = {};
const pollingInProgress = {};
const connectingNow = {};
const busyBannerShown = {};

const DEFAULT_LAB_DURATION_SECONDS = 3600;
const TERMINAL_POLL_INTERVAL_MS = 50;
const INPUT_FLUSH_DELAY_MS = 5;

const STORAGE_PREFIX = `vm-lab-${CURRENT_LAB}`;
const TIMER_STORAGE_KEY = `${STORAGE_PREFIX}-expiry`;
const TERMINAL_SESSION_STORAGE_KEY = `${STORAGE_PREFIX}-terminal-sessions`;
const TERMINAL_OUTPUT_STORAGE_KEY = `${STORAGE_PREFIX}-terminal-output`;
const MAX_STORED_TERMINAL_OUTPUT = 300000;

const labEndpoint = (path) => `/api/${CURRENT_LAB}${path}`;

let loadedDocuments = [];
let selectedDocument = null;

// -------------------------------------------------------------------------
// Persistent browser state
// -------------------------------------------------------------------------

function readStorage(key, fallback = null) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : JSON.parse(value);
  } catch (_) {
    return fallback;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (_) {}
}

function removeStorage(key) {
  try {
    localStorage.removeItem(key);
  } catch (_) {}
}

function getSavedTerminalSessions() {
  return readStorage(TERMINAL_SESSION_STORAGE_KEY, {});
}

function saveTerminalSession(container, sessionId) {
  const sessions = getSavedTerminalSessions();
  sessions[container] = sessionId;
  writeStorage(TERMINAL_SESSION_STORAGE_KEY, sessions);
}

function removeSavedTerminalSession(container) {
  const sessions = getSavedTerminalSessions();
  delete sessions[container];
  writeStorage(TERMINAL_SESSION_STORAGE_KEY, sessions);
}

function getSavedTerminalOutput() {
  return readStorage(TERMINAL_OUTPUT_STORAGE_KEY, {});
}

function saveTerminalOutput(container, output) {
  const history = getSavedTerminalOutput();

  let previous = history[container] || "";
  previous += output;

  if (previous.length > MAX_STORED_TERMINAL_OUTPUT) {
    previous = previous.slice(-MAX_STORED_TERMINAL_OUTPUT);
  }

  history[container] = previous;
  writeStorage(TERMINAL_OUTPUT_STORAGE_KEY, history);
}

function restoreTerminalOutput(container) {
  const history = getSavedTerminalOutput();
  const output = history[container];

  if (output && terminals[container]) {
    terminals[container].write(output);
  }
}

function clearSavedTerminalOutput(container) {
  const history = getSavedTerminalOutput();
  delete history[container];
  writeStorage(TERMINAL_OUTPUT_STORAGE_KEY, history);
}

function saveTimerExpiry(timestamp) {
  if (timestamp === null) {
    removeStorage(TIMER_STORAGE_KEY);
    return;
  }

  writeStorage(TIMER_STORAGE_KEY, timestamp);
}

function restoreTimerExpiry() {
  const savedExpiry = readStorage(TIMER_STORAGE_KEY, null);

  if (Number.isFinite(Number(savedExpiry)) && Number(savedExpiry) > 0) {
    expiryTimestamp = Number(savedExpiry);
    labCreatedLocally = true;
  }
}

// -------------------------------------------------------------------------
// API
// -------------------------------------------------------------------------

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

// -------------------------------------------------------------------------
// Terminal
// -------------------------------------------------------------------------

function writeTerminal(container, output, persist = true) {
  if (!output) return;

  createLocalTerminal(container);
  terminals[container].write(output);

  if (persist) {
    saveTerminalOutput(container, output);
  }
}

function createLocalTerminal(container) {
  if (terminals[container]) return;

  const terminal = new Terminal({
    cursorBlink: true,
    convertEol: true,
    scrollback: 5000,
    fontSize: 13,
    fontFamily: "Consolas, 'Courier New', monospace",
    theme: {
      background: "#080d18",
      foreground: "#e5edf7",
      cursor: "#82aaff",
    },
  });

  const fit = new FitAddon.FitAddon();

  terminal.loadAddon(fit);

  terminals[container] = terminal;
  fitAddons[container] = fit;
  inputQueues[container] = "";
  pollingInProgress[container] = false;

  terminal.onData((data) => {
    if (!terminalSessions[container]) return;

    inputQueues[container] += data;

    if (!inputFlushTimers[container]) {
      inputFlushTimers[container] = setTimeout(() => {
        inputFlushTimers[container] = null;
        flushTerminalInput(container);
      }, INPUT_FLUSH_DELAY_MS);
    }
  });

  terminal.onResize((size) => {
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

    if (!response.ok) throw new Error("Terminal input failed.");
  } catch (_) {
    if (terminalSessions[container] === sessionId) {
      inputQueues[container] = data + inputQueues[container];
    }
  }

  if (inputQueues[container] && terminalSessions[container]) {
    inputFlushTimers[container] = setTimeout(() => {
      inputFlushTimers[container] = null;
      flushTerminalInput(container);
    }, INPUT_FLUSH_DELAY_MS);
  }
}

function updateTerminalStateLabel() {
  if (!terminalState) return;

  if (terminalSessions[activeContainer]) {
    terminalState.textContent = "Connected";
    terminalState.className = "terminal-state online";
  } else if (labBusyMessageActive) {
    terminalState.textContent = "Setting up...";
    terminalState.className = "terminal-state";
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

  restoreTerminalOutput(activeContainer);

  terminalState.textContent = terminalSessions[activeContainer]
    ? "Connected"
    : "Not connected";

  terminalState.className = terminalSessions[activeContainer]
    ? "terminal-state online"
    : "terminal-state";

  updateTerminalStateLabel();
}

function writeBusyBanner(kind) {
  const text =
    kind === "creating"
      ? "Creating Ansible lab containers..."
      : "Setting up Ansible lab (installing packages, configuring SSH and inventory). This can take around 1 minute and 45 seconds.";

  HOSTS.forEach((container) => {
    createLocalTerminal(container);

    if (busyBannerShown[container] === kind) return;

    busyBannerShown[container] = kind;

    writeTerminal(
      container,
      `\r\n\x1b[33m[lab] ${text}\x1b[0m\r\n` +
        "\x1b[33m[lab] The terminal will connect automatically when the lab is ready. Please do not use the hosts yet.\x1b[0m\r\n",
    );
  });

  labBusyMessageActive = true;
  updateTerminalStateLabel();
}

function writeReadyBanner() {
  HOSTS.forEach((container) => {
    if (!busyBannerShown[container]) return;

    createLocalTerminal(container);

    writeTerminal(
      container,
      "\r\n\x1b[32m[lab] Setup complete. Connecting...\x1b[0m\r\n",
    );

    delete busyBannerShown[container];
  });

  labBusyMessageActive = false;
}

function connectAllHosts() {
  const ordered = [
    activeContainer,
    ...HOSTS.filter((name) => name !== activeContainer),
  ];

  ordered.forEach((container) => {
    connectTerminal(container);
  });
}

async function connectTerminal(container) {
  createLocalTerminal(container);

  if (terminalSessions[container] || connectingNow[container]) return;

  if (!labReadyForTerminal) {
    if (container === activeContainer) {
      updateTerminalStateLabel();
    }

    return;
  }

  connectingNow[container] = true;

  try {
    const savedSessions = getSavedTerminalSessions();
    const savedSessionId = savedSessions[container];

    if (savedSessionId) {
      try {
        const response = await fetch(`/api/terminal/${savedSessionId}/read`, {
          cache: "no-store",
        });

        if (response.ok) {
          const data = await response.json();

          if (!data.closed) {
            terminalSessions[container] = savedSessionId;

            if (data.output) {
              writeTerminal(container, data.output);
            }

            if (container === activeContainer) {
              terminalState.textContent = "Connected";
              terminalState.className = "terminal-state online";
            }

            pollTerminal(container);
            resizeActiveTerminal();
            return;
          }
        }
      } catch (_) {}

      removeSavedTerminalSession(container);
    }

    const data = await api(labEndpoint("/terminal/open"), {
      method: "POST",
      body: JSON.stringify({ container }),
    });

    terminalSessions[container] = data.session_id;
    saveTerminalSession(container, data.session_id);

    if (container === activeContainer) {
      terminalState.textContent = "Connected";
      terminalState.className = "terminal-state online";
    }

    pollTerminal(container);
    resizeActiveTerminal();
  } catch (error) {
    if (container === activeContainer) {
      terminalState.textContent = "Connection failed";
      terminalState.className = "terminal-state error";
    }
  } finally {
    connectingNow[container] = false;
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
      writeTerminal(container, data.output);
    }

    if (data.closed || !response.ok) {
      clearTimeout(pollTimers[container]);
      delete pollTimers[container];
      delete terminalSessions[container];
      removeSavedTerminalSession(container);

      if (container === activeContainer) {
        terminalState.textContent = "Session closed";
        terminalState.className = "terminal-state";
      }

      return;
    }
  } catch (_) {
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

  clearTimeout(pollTimers[container]);
  clearTimeout(inputFlushTimers[container]);

  delete pollTimers[container];
  delete inputFlushTimers[container];

  inputQueues[container] = "";

  if (sessionId) {
    try {
      await fetch(`/api/terminal/${sessionId}`, {
        method: "DELETE",
      });
    } catch (_) {}
  }

  delete terminalSessions[container];
  removeSavedTerminalSession(container);
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

  if (title) title.textContent = container;

  if (subtitle) {
    subtitle.textContent =
      container === "ansible-controller"
        ? "Ansible controller terminal"
        : "Managed client terminal";
  }

  if (description) {
    description.textContent = `Interactive terminal for ${container}`;
  }

  showActiveTerminal();
  connectTerminal(container);
}

// -------------------------------------------------------------------------
// Persistent timer
// -------------------------------------------------------------------------

function clearCountdown() {
  expiryTimestamp = null;
  labCreatedLocally = false;
  saveTimerExpiry(null);
  renderTimer();
}

function startCountdown(seconds) {
  expiryTimestamp = Date.now() + Math.ceil(seconds) * 1000;
  labCreatedLocally = true;

  saveTimerExpiry(expiryTimestamp);
  renderTimer();
}

function updateTimerFromStatus(data) {
  const remaining = Number(data.remaining_seconds);

  if (
    data.remaining_seconds !== null &&
    data.remaining_seconds !== undefined &&
    Number.isFinite(remaining) &&
    remaining > 0
  ) {
    expiryTimestamp = Date.now() + remaining * 1000;
    labCreatedLocally = true;

    saveTimerExpiry(expiryTimestamp);
    renderTimer();
    return;
  }

  if (labCreatedLocally && expiryTimestamp !== null) {
    renderTimer();
    return;
  }

  if (data.running && expiryTimestamp === null) {
    restoreTimerExpiry();

    if (expiryTimestamp === null) {
      startCountdown(DEFAULT_LAB_DURATION_SECONDS);
    }
  } else if (!data.running && !data.creating) {
    clearCountdown();
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
    saveTimerExpiry(null);
    refreshStatus();
  }
}

// -------------------------------------------------------------------------
// Status
// -------------------------------------------------------------------------

async function refreshStatus() {
  if (statusRefreshInProgress) return;

  statusRefreshInProgress = true;

  try {
    try {
      await api("/api/health");

      if (hostStatus) {
        hostStatus.textContent = "Connected";
        hostStatus.className = "status-pill online";
      }
    } catch (_) {
      if (hostStatus) {
        hostStatus.textContent = "Connection failed";
        hostStatus.className = "status-pill";
      }
    }

    const data = await api(labEndpoint("/status"));

    HOSTS.forEach((name) => {
      const item = data.containers?.[name] || {};
      const statusId =
        name === "ansible-controller" ? "controllerStatus" : "clientStatus";

      const statusElement = document.getElementById(statusId);
      const button = document.querySelector(
        `.host-item[data-container="${name}"]`,
      );
      const dot = button?.querySelector(".host-dot");
      const running = item.state === "running";

      if (statusElement) {
        statusElement.textContent = running
          ? "Running"
          : ["exited", "dead"].includes(item.state)
            ? "Stopped"
            : item.status || "Not created";
      }

      if (dot) {
        dot.classList.toggle("running", running);
        dot.classList.toggle(
          "stopped",
          !running && ["exited", "dead", "missing"].includes(item.state),
        );
      }
    });

    updateTimerFromStatus(data);

    labReadyForTerminal = Boolean(
      data.running && !data.setting_up && !data.creating,
    );

    if (data.creating) {
      writeBusyBanner("creating");
    } else if (data.setting_up) {
      writeBusyBanner("setup");
    } else if (labReadyForTerminal) {
      if (labBusyMessageActive) writeReadyBanner();

      connectAllHosts();
    } else {
      labBusyMessageActive = false;
    }

    const createButton = document.getElementById("createClusterBtn");
    const stopButton = document.getElementById("stopClusterBtn");
    const destroyButton = document.getElementById("destroyClusterBtn");

    if (createButton && !createButton.dataset.originalText) {
      createButton.disabled = Boolean(data.creating);
    }

    if (stopButton) stopButton.disabled = !data.running;
    if (destroyButton) destroyButton.disabled = !data.has_containers;
  } catch (_) {
    renderTimer();
  } finally {
    statusRefreshInProgress = false;
  }
}

// -------------------------------------------------------------------------
// Documents
// -------------------------------------------------------------------------

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );
}

function markdownToHtml(markdown) {
  const lines = String(markdown).replace(/\r\n/g, "\n").split("\n");
  const output = [];
  let paragraph = [];
  let codeLines = [];
  let inCode = false;
  let codeLanguage = "";
  let listType = null;
  let listItems = [];

  function inlineMarkdown(value) {
    let text = escapeHtml(value);

    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");

    return text;
  }

  function flushParagraph() {
    if (paragraph.length) {
      output.push("<p>" + inlineMarkdown(paragraph.join(" ").trim()) + "</p>");
      paragraph = [];
    }
  }

  function flushCode() {
    output.push(
      '<pre><code class="language-' +
        escapeHtml(codeLanguage || "text") +
        '">' +
        escapeHtml(codeLines.join("\n")) +
        "</code></pre>",
    );

    codeLines = [];
    codeLanguage = "";
  }

  function flushList() {
    if (listType && listItems.length) {
      output.push(
        "<" +
          listType +
          ">" +
          listItems
            .map((item) => "<li>" + inlineMarkdown(item) + "</li>")
            .join("") +
          "</" +
          listType +
          ">",
      );
    }

    listType = null;
    listItems = [];
  }

  for (const line of lines) {
    const fence = line.match(/^\s*```([\w.+-]*)\s*$/);

    if (fence) {
      flushParagraph();
      flushList();

      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        inCode = true;
        codeLanguage = fence[1] || "text";
      }

      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);

    if (heading) {
      flushParagraph();
      flushList();

      const level = heading[1].length;

      output.push(
        "<h" + level + ">" + inlineMarkdown(heading[2]) + "</h" + level + ">",
      );

      continue;
    }

    if (/^\s*---+\s*$/.test(line)) {
      flushParagraph();
      flushList();
      output.push("<hr>");
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);

    if (quote) {
      flushParagraph();
      flushList();

      output.push(
        "<blockquote><p>" + inlineMarkdown(quote[1]) + "</p></blockquote>",
      );

      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);

    if (unordered) {
      flushParagraph();

      if (listType !== "ul") {
        flushList();
        listType = "ul";
      }

      listItems.push(unordered[1]);
      continue;
    }

    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);

    if (ordered) {
      flushParagraph();

      if (listType !== "ol") {
        flushList();
        listType = "ol";
      }

      listItems.push(ordered[1]);
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  if (inCode) {
    flushCode();
  }

  flushParagraph();
  flushList();

  return '<div class="markdown-body">' + output.join("\n") + "</div>";
}

function displayDocumentContent(doc) {
  const content = document.getElementById("documentContent");

  if (!content || !doc) return;

  const extension = doc.name.split(".").pop().toLowerCase();

  if (extension === "html" || extension === "htm") {
    const iframe = document.createElement("iframe");

    iframe.className = "document-html-frame";
    iframe.title = doc.title || doc.name;

    // YouTube requires JavaScript inside the embedded document.
    iframe.setAttribute(
      "sandbox",
      "allow-scripts allow-same-origin allow-presentation allow-forms allow-popups",
    );

    iframe.setAttribute("loading", "eager");
    iframe.srcdoc = String(doc.content || "");

    content.replaceChildren(iframe);
  } else if (extension === "md") {
    content.innerHTML = markdownToHtml(doc.content || "");
  } else {
    const pre = document.createElement("pre");
    pre.className = "document-text-source";
    pre.textContent = doc.content || "";
    content.replaceChildren(pre);
  }
}

async function loadDocuments() {
  const select = document.getElementById("documentSelect");
  const content = document.getElementById("documentContent");

  if (!select || !content) return;

  const previousSelection = select.value;

  try {
    const data = await api("/api/documents");

    if (!Array.isArray(data.documents)) {
      throw new Error("Invalid document response.");
    }

    loadedDocuments = data.documents;
    select.replaceChildren();

    if (!loadedDocuments.length) {
      selectedDocument = null;
      content.textContent = "No documentation files found.";
      return;
    }

    loadedDocuments.forEach((doc) => {
      const option = document.createElement("option");
      option.value = doc.name;
      option.textContent = doc.title || doc.name;
      select.appendChild(option);
    });

    const selectedExists = loadedDocuments.some(
      (doc) => doc.name === previousSelection,
    );

    select.value = selectedExists ? previousSelection : loadedDocuments[0].name;

    function displaySelected() {
      selectedDocument = loadedDocuments.find(
        (item) => item.name === select.value,
      );

      if (!selectedDocument) {
        content.textContent = "Select a document.";
        return;
      }

      displayDocumentContent(selectedDocument);
    }

    select.onchange = displaySelected;
    displaySelected();
  } catch (error) {
    content.textContent = `Unable to load documents: ${error.message}`;
  }
}

// -------------------------------------------------------------------------
// Cluster actions
// -------------------------------------------------------------------------

async function clusterAction(action) {
  const buttonId =
    action === "create"
      ? "createClusterBtn"
      : action === "stop"
        ? "stopClusterBtn"
        : "destroyClusterBtn";

  const button = document.getElementById(buttonId);

  if (
    action === "destroy" &&
    !confirm("Destroy all Ansible lab hosts and their network?")
  ) {
    return;
  }

  setLoading(button, true, action === "create" ? "Creating..." : "Working...");

  try {
    const payload = action === "create" ? { sid: socket.id } : undefined;

    await api(labEndpoint(`/${action}`), {
      method: "POST",
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });

    if (action === "create") {
      startCountdown(DEFAULT_LAB_DURATION_SECONDS);
      labReadyForTerminal = false;
      writeBusyBanner("creating");
    }

    if (action === "stop" || action === "destroy") {
      clearCountdown();
      labReadyForTerminal = false;
      labBusyMessageActive = false;

      HOSTS.forEach((name) => {
        delete busyBannerShown[name];
        closeTerminalSession(name);
      });
    }

    await refreshStatus();
  } catch (error) {
    alert(error.message);

    if (action === "create") clearCountdown();
  } finally {
    setLoading(button, false);
    await refreshStatus();
  }
}

// -------------------------------------------------------------------------
// Event listeners
// -------------------------------------------------------------------------

document.querySelectorAll(".host-item").forEach((button) => {
  button.addEventListener("click", () => {
    setActiveHost(button.dataset.container);
  });
});

document.getElementById("hostsButton")?.addEventListener("click", () => {
  document.getElementById("hostsButton").classList.add("selected");
  document.getElementById("documentsButton")?.classList.remove("selected");
});

document
  .getElementById("documentsButton")
  ?.addEventListener("click", async () => {
    document.getElementById("documentsButton").classList.add("selected");
    document.getElementById("hostsButton")?.classList.remove("selected");

    await loadDocuments();

    document.getElementById("documentsPanel")?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  });

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
    clearSavedTerminalOutput(activeContainer);
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  }
});

// -------------------------------------------------------------------------
// Existing terminal/document splitter
// -------------------------------------------------------------------------

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

// -------------------------------------------------------------------------
// Socket events
// -------------------------------------------------------------------------

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

socket.on("lab_ready", async (data) => {
  if (data.lab && data.lab !== CURRENT_LAB) return;

  await refreshStatus();

  if (data.success === false) {
    alert(data.message || "Lab setup failed.");
  }
});

socket.on("cluster_expired", async (data) => {
  if (data.lab && data.lab !== CURRENT_LAB) return;

  clearCountdown();
  labReadyForTerminal = false;
  labBusyMessageActive = false;

  HOSTS.forEach((name) => {
    delete busyBannerShown[name];
    closeTerminalSession(name);
    clearSavedTerminalOutput(name);
  });

  await refreshStatus();

  alert(data.message || "The lab has expired.");
});

socket.on("connect", refreshStatus);

// -------------------------------------------------------------------------
// Progress tracking
// -------------------------------------------------------------------------

let progressData = null;
let progressBusy = false;

function setProgressMessage(text, kind) {
  const message = document.getElementById("progressMessage");

  if (!message) return;

  message.textContent = text || "";
  message.className = `progress-message${kind ? ` ${kind}` : ""}`;
}

function renderProgress(data) {
  progressData = data;

  const bar = document.getElementById("progressBar");
  const percent = document.getElementById("progressPercent");
  const count = document.getElementById("progressCount");
  const list = document.getElementById("progressList");
  const checkButton = document.getElementById("progressCheckBtn");

  if (bar) bar.style.width = `${data.percent}%`;
  if (percent) percent.textContent = `${data.percent}%`;
  if (count) count.textContent = `${data.completed} / ${data.total} completed`;

  if (list) {
    list.replaceChildren();

    if (!data.documents.length) {
      const empty = document.createElement("li");
      empty.className = "progress-item locked";
      empty.textContent = "No documents found.";
      list.appendChild(empty);
    }

    data.documents.forEach((doc) => {
      const item = document.createElement("li");
      item.className = `progress-item ${doc.status}`;

      const icon = document.createElement("span");
      icon.className = "progress-icon";
      icon.textContent =
        doc.status === "completed"
          ? "✓"
          : doc.status === "current"
            ? "●"
            : "🔒";

      const label = document.createElement("span");
      label.className = "progress-label";
      label.textContent = doc.title;

      item.appendChild(icon);
      item.appendChild(label);
      list.appendChild(item);
    });
  }

  if (checkButton) {
    const currentDoc = data.documents.find((doc) => doc.name === data.current);

    checkButton.disabled = progressBusy || !currentDoc;
    checkButton.textContent = data.complete
      ? "All documents completed"
      : currentDoc && currentDoc.has_check
        ? "Run check"
        : "Mark complete";
  }
}

async function loadProgress() {
  try {
    const data = await api(labEndpoint("/progress"));
    renderProgress(data);
  } catch (_) {}
}

async function completeCurrentDocument() {
  if (progressBusy || !progressData || !progressData.current) return;

  const button = document.getElementById("progressCheckBtn");
  const name = progressData.current;

  progressBusy = true;
  setProgressMessage("", "");
  setLoading(button, true, "Checking...");

  try {
    const data = await api(labEndpoint("/progress/complete"), {
      method: "POST",
      body: JSON.stringify({ name }),
    });

    progressBusy = false;
    setLoading(button, false);
    renderProgress(data);

    setProgressMessage(
      data.complete
        ? "All documents completed."
        : "Document completed. Next document unlocked.",
      "success",
    );

    document.getElementById("reloadDocsBtn")?.click();
  } catch (error) {
    progressBusy = false;
    setLoading(button, false);
    setProgressMessage(error.message, "error");
    if (progressData) renderProgress(progressData);
  }
}

async function resetProgress() {
  if (!confirm("Reset progress and lock all documents except the first?")) {
    return;
  }

  try {
    const data = await api(labEndpoint("/progress/reset"), {
      method: "POST",
    });

    renderProgress(data);
    setProgressMessage("Progress reset.", "success");
    document.getElementById("reloadDocsBtn")?.click();
  } catch (error) {
    setProgressMessage(error.message, "error");
  }
}

document
  .getElementById("progressCheckBtn")
  ?.addEventListener("click", completeCurrentDocument);

document
  .getElementById("progressResetBtn")
  ?.addEventListener("click", resetProgress);

// -------------------------------------------------------------------------
// Initialization
// -------------------------------------------------------------------------

restoreTimerExpiry();

createLocalTerminal(activeContainer);
showActiveTerminal();

refreshStatus();
loadDocuments();
loadProgress();

setInterval(renderTimer, 1000);
setInterval(refreshStatus, 10000);
setInterval(loadProgress, 5000);
