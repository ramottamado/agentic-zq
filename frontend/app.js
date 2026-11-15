const byId = (id) => document.getElementById(id);

const SESSION_EXPIRED =
  "Could not reach the server. Your session may have expired: reload the page to sign in again.";

async function loadMode() {
  try {
    const res = await fetch("api/health");
    const health = await res.json();
    byId("mode").textContent = health.llm === "mock" ? "mock LLM" : `LLM: ${health.model}`;
    byId("mode").hidden = false;
  } catch { /* health badge is optional */ }
}

// The CAI proxy answers with an HTML login page (or a redirect that fetch cannot follow) when the
// session has expired. Reading that as JSON would show the user a cryptic parse error.
async function readJson(res) {
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  if (!isJson) {
    throw new Error(res.ok ? SESSION_EXPIRED : `The server returned an unexpected response (${res.status}).`);
  }
  return res.json();
}

function renderTable(columns, rows) {
  const table = byId("table");
  table.replaceChildren();
  const head = table.createTHead().insertRow();
  columns.forEach((name) => {
    const th = document.createElement("th");
    th.textContent = name;
    head.appendChild(th);
  });
  const body = table.createTBody();
  rows.forEach((row) => {
    const tr = body.insertRow();
    row.forEach((value) => {
      const td = tr.insertCell();
      if (typeof value === "number") {
        td.className = "num";
        td.textContent = value.toLocaleString();
      } else {
        td.textContent = value ?? "";
      }
    });
  });
}

function showResult(data) {
  byId("route").textContent = data.route === "sql" ? "SQL agent" : "General agent";
  byId("answer").textContent = data.answer;
  byId("sql").textContent = data.sql || "";
  byId("sql-box").hidden = !data.sql;
  const hasRows = data.columns.length > 0 && data.rows.length > 0;
  byId("table-wrap").hidden = !hasRows;
  if (hasRows) renderTable(data.columns, data.rows);
  const notes = [];
  if (data.truncated) notes.push("Showing the first rows only (result was capped).");
  if (data.error) notes.push(`Last error: ${data.error}`);
  byId("note").textContent = notes.join(" ");
  byId("note").hidden = notes.length === 0;
  byId("result").hidden = false;
}

async function ask(question) {
  byId("error").hidden = true;
  byId("ask-btn").disabled = true;
  byId("ask-btn").textContent = "Thinking…";
  try {
    // Relative URL so it works behind the CAI application path prefix.
    const res = await fetch("api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await readJson(res);
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
    showResult(data);
  } catch (err) {
    // fetch rejects with a TypeError when the network fails or a login redirect is blocked.
    byId("error").textContent = err instanceof TypeError ? SESSION_EXPIRED : err.message;
    byId("error").hidden = false;
  } finally {
    byId("ask-btn").disabled = false;
    byId("ask-btn").textContent = "Ask";
  }
}

byId("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = byId("question").value.trim();
  if (question) ask(question);
});
byId("examples").addEventListener("click", (event) => {
  if (event.target.tagName !== "BUTTON") return;
  const question = event.target.textContent;
  byId("question").value = question;
  ask(question);
});
loadMode();
