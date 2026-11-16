const byId = id => document.getElementById(id);
const status = byId("status");
const connect = byId("connect");
const form = byId("submit");
let token = "";
let selected = "";
let pending = false;

async function call(path, options = {}) {
  const result = await fetch("/api/" + path, {
    ...options, headers: { Authorization: "Bearer " + token, ...options.headers }
  });
  const body = await result.json();
  if (!result.ok) throw new Error(typeof body.detail === "string" ? body.detail.replaceAll("_", " ") : "Check the submitted fields.");
  return body;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function post(path, body, key) {
  return call(path, { method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) } });
}

function render(runs) {
  const runList = byId("runs");
  const detail = byId("detail");
  const run = runs.find(item => item.id === selected) || runs[0];
  runList.replaceChildren();
  for (const item of runs) {
    const action = element("button", item.payload.vendor_id + " · " + item.status.replaceAll("_", " "), "row");
    action.setAttribute("aria-pressed", String(item.id === run?.id));
    action.onclick = () => { selected = item.id; render(runs); };
    runList.append(action);
  }
  if (!run) {
    runList.append(element("p", "No submissions yet."));
    return;
  }
  detail.replaceChildren(element("p", "Status " + run.status.replaceAll("_", " ")),
    element("p", "Tool attempts " + run.state.tool_calls + " / " + run.payload.tool_budget));
  const stages = ["plan", "retrieve_vendor", "analyze_document", "validate", "approval"];
  detail.append(element("p", stages.map(stage => stage === run.state.stage ? "[" + stage + "]" : stage).join(" → ").replaceAll("_", " ")));
  if (run.state.outputs.validate) {
    detail.append(element("h3", "Review brief"), element("pre", JSON.stringify(run.state.outputs.validate, null, 2)));
  }
  const actions = run.status === "awaiting_approval" ? ["approve", "reject", "cancel"] :
    ["queued", "running"].includes(run.status) ? ["cancel"] : [];
  for (const action of actions) {
    const button = element("button", action[0].toUpperCase() + action.slice(1));
    button.onclick = () => void perform(async () => { await post("runs/" + run.id + "/decision", { decision: action }); });
    detail.append(button, " ");
  }
  const trace = element("ol", "", "trace");
  for (const entry of run.state.history) {
    trace.append(element("li", entry.stage.replaceAll("_", " ") + " · " + entry.event.replaceAll("_", " ") +
      (entry.code_detail ? " · " + entry.code_detail.replaceAll("_", " ") : "") + (entry.tool ? " · " + entry.tool : "")));
  }
  detail.append(element("h3", "Execution trace"), trace);
}

async function perform(action) {
  if (pending) return;
  pending = true;
  status.textContent = "";
  try { await action(); render(await call("runs")); }
  catch (error) { status.textContent = error.message; }
  finally { pending = false; }
}

async function login(value) {
  token = value;
  try {
    render(await call("runs"));
    byId("workspace").hidden = false;
    connect.hidden = true;
    status.textContent = "";
  } catch (error) {
    token = "";
    status.textContent = error.message;
  }
}

connect.onsubmit = event => {
  event.preventDefault();
  const value = new FormData(connect).get("token");
  connect.reset();
  void login(value);
};
form.onsubmit = event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  void perform(async () => {
    const run = await post("runs", { ...data, tool_budget: Number(data.tool_budget), fail_first: data.fail_first === "on" }, crypto.randomUUID());
    selected = run.id;
  });
};
const config = await call("config");
if (config.demo) {
  byId("simulate").hidden = false;
  await login("opspilot-local-demo");
}
setInterval(() => {
  if (token && !pending) void call("runs").then(runs => { if (!pending) render(runs); }).catch(() => undefined);
}, 1500);
