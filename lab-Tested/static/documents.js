const documentList = document.getElementById("documentList");
const documentName = document.getElementById("documentName");
const documentContent = document.getElementById("documentContent");
const documentMessage = document.getElementById("documentMessage");
const documentPreview = document.getElementById("documentPreview");
const saveDocumentBtn = document.getElementById("saveDocumentBtn");
const deleteDocumentBtn = document.getElementById("deleteDocumentBtn");
const previewDocumentBtn = document.getElementById("previewDocumentBtn");

let selectedDocument = null;
let documents = [];

async function requestJson(url, options = {}) {
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
    throw new Error(data.error || `HTTP ${response.status}`);
  }

  return data;
}

function showMessage(message, type = "info") {
  documentMessage.textContent = message;
  documentMessage.className = `message ${type}`;
}

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;",
      })[character],
  );
}

function markdownToHtml(markdown) {
  let html = escapeHtml(markdown);
  const codeBlocks = [];

  html = html.replace(
    /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g,
    (_, language, code) => {
      const index = codeBlocks.length;
      codeBlocks.push(
        `<pre><code class="language-${escapeHtml(language || "text")}">${code}</code></pre>`,
      );
      return `@@CODEBLOCK${index}@@`;
    },
  );

  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/^\s*-\s+(.+)$/gm, "<li>$1</li>");
  html = html.replace(/(?:<li>.*<\/li>\n?)+/g, (match) => `<ul>${match}</ul>`);
  html = html.replace(/\n{2,}/g, "</p><p>");
  html = html.replace(/\n/g, "<br>");
  html = `<p>${html}</p>`;

  codeBlocks.forEach((block, index) => {
    html = html.replace(`@@CODEBLOCK${index}@@`, block);
  });

  return html;
}

function renderDocumentList() {
  documentList.replaceChildren();

  if (!documents.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No documents found.";
    documentList.appendChild(empty);
    return;
  }

  documents.forEach((documentItem) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "document-list-item";
    if (selectedDocument === documentItem.name) {
      button.classList.add("selected");
    }

    const title = document.createElement("strong");
    title.textContent = documentItem.title;

    const filename = document.createElement("span");
    filename.textContent = documentItem.name;

    button.append(title, filename);
    button.addEventListener("click", () => selectDocument(documentItem));
    documentList.appendChild(button);
  });
}

async function loadDocuments(preferredName = null) {
  documentList.innerHTML = '<p class="muted">Loading documents...</p>';

  try {
    const data = await requestJson("/api/documents");
    documents = data.documents || [];
    renderDocumentList();

    const preferred = preferredName
      ? documents.find((item) => item.name === preferredName)
      : null;

    if (preferred) {
      selectDocument(preferred);
    } else if (selectedDocument) {
      const existing = documents.find((item) => item.name === selectedDocument);
      if (existing) {
        selectDocument(existing);
      } else {
        clearEditor();
      }
    } else if (documents.length) {
      selectDocument(documents[0]);
    } else {
      clearEditor();
    }
  } catch (error) {
    documentList.innerHTML = "";
    const message = document.createElement("p");
    message.className = "muted";
    message.textContent = error.message;
    documentList.appendChild(message);
  }
}

function selectDocument(item) {
  selectedDocument = item.name;
  documentName.value = item.name.replace(/\.md$/i, "");
  documentContent.value = item.content;
  documentPreview.classList.add("hidden");
  documentPreview.replaceChildren();
  deleteDocumentBtn.disabled = false;
  showMessage(`Selected ${item.name}`, "info");
  renderDocumentList();
}

function clearEditor() {
  selectedDocument = null;
  documentName.value = "";
  documentContent.value = "";
  documentPreview.classList.add("hidden");
  documentPreview.replaceChildren();
  deleteDocumentBtn.disabled = true;
  renderDocumentList();
}

document.getElementById("newDocumentBtn").addEventListener("click", () => {
  clearEditor();
  documentName.focus();
  showMessage("Enter a name and content for the new document.", "info");
});

document
  .getElementById("reloadDocsBtn")
  .addEventListener("click", () => loadDocuments());

saveDocumentBtn.addEventListener("click", async () => {
  const name = documentName.value.trim();
  const content = documentContent.value;

  if (!name) {
    showMessage("Enter a document name.", "error");
    documentName.focus();
    return;
  }

  saveDocumentBtn.disabled = true;

  try {
    const data = await requestJson("/api/documents", {
      method: "POST",
      body: JSON.stringify({ name, content }),
    });

    selectedDocument = data.name;
    showMessage(`Saved ${data.name}`, "success");
    await loadDocuments(data.name);
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    saveDocumentBtn.disabled = false;
  }
});

deleteDocumentBtn.addEventListener("click", async () => {
  if (!selectedDocument) {
    showMessage("Select a saved document first.", "error");
    return;
  }

  if (!confirm(`Delete ${selectedDocument}?`)) return;

  deleteDocumentBtn.disabled = true;

  try {
    await requestJson(
      `/api/documents/${encodeURIComponent(selectedDocument)}`,
      {
        method: "DELETE",
      },
    );

    showMessage(`Deleted ${selectedDocument}`, "success");
    selectedDocument = null;
    await loadDocuments();
  } catch (error) {
    showMessage(error.message, "error");
    deleteDocumentBtn.disabled = false;
  }
});

previewDocumentBtn.addEventListener("click", () => {
  documentPreview.innerHTML = markdownToHtml(documentContent.value);
  documentPreview.classList.remove("hidden");
});

loadDocuments();
