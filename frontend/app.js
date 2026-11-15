const $ = (id) => document.getElementById(id);

async function loadMode() {
  try {
    const res = await fetch("api/health");
    const h = await res.json();
    $("mode").textContent = h.llm === "mock" ? "mock LLM" : `LLM: ${h.model}`;
    $("mode").hidden = false;
  } catch { /* health badge is optional */ }
}

function renderTable(columns, rows) {
  const table = $("table");
  table.replaceChildren();
  const head = table.createTHead().insertRow();
  columns.forEach((c) => { const th = document.createElement("th"); th.textContent = c; head.appendChild(th); });
  const body = table.createTBody();
  rows.forEach((r) => {
    const tr = body.insertRow();
    r.forEach((v) => {
      const td = tr.insertCell();
      if (typeof v === "number") { td.className = "num"; td.textContent = v.toLocaleString(); }
      else td.textContent = v ?? "";
    });
  });
}

async function ask(question) {
  $("error").hidden = true;
  $("ask-btn").disabled = true;
  $("ask-btn").textContent = "Thinking…";
  try {
    // Relative URL so it works behind the CAI application path prefix.
    const res = await fetch("api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);

    $("route").textContent = data.route === "sql" ? "SQL agent" : "General agent";
    $("answer").textContent = data.answer;
    $("sql").textContent = data.sql || "";
    $("sql-box").hidden = !data.sql;
    const hasRows = data.columns.length > 0 && data.rows.length > 0;
    $("table-wrap").hidden = !hasRows;
    if (hasRows) renderTable(data.columns, data.rows);
    const notes = [];
    if (data.truncated) notes.push("Showing the first rows only (result was capped).");
    if (data.error) notes.push(`Last error: ${data.error}`);
    $("note").textContent = notes.join(" ");
    $("note").hidden = notes.length === 0;
    $("result").hidden = false;
  } catch (err) {
    $("error").textContent = err.message;
    $("error").hidden = false;
  } finally {
    $("ask-btn").disabled = false;
    $("ask-btn").textContent = "Ask";
  }
}

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (q) ask(q);
});
$("examples").addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  $("question").value = e.target.textContent;
  ask(e.target.textContent);
});
loadMode();
