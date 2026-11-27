// Builds docs/Expense-Tracker-and-Finance-Agent.pptx  (run: node docs/build_deck.js)
const path = require("path");
const pptxgen = require("pptxgenjs");
const SKILL = process.env.PPTX_SKILL_DIR;
const { applyTheme } = SKILL ? require(path.join(SKILL, "scripts", "apply_theme.js")) : { applyTheme: null };

const NAVY = "12263F", TEAL = "0E7C86", TEAL_L = "E3F4F5", AMBER = "F2A33A", AMBER_L = "FDF1DC";
const INK = "1F2937", MUTED = "5B6B7B", LIGHT = "F4F7FA", WHITE = "FFFFFF", RED = "C0392B", GREEN = "2E8B57", LINE = "C9D3DD";
const HEAD = "Cambria", BODY = "Calibri";

const THEME = {
  name: "Expense Agents", headFontFace: HEAD, bodyFontFace: BODY,
  colors: { dk1: INK, lt1: WHITE, dk2: NAVY, lt2: LIGHT, accent1: TEAL, accent2: AMBER, accent3: GREEN, accent4: RED, accent5: "6C5CE7", accent6: MUTED, hlink: TEAL, folHlink: MUTED },
};

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.33 x 7.5
pres.theme = { headFontFace: HEAD, bodyFontFace: BODY };
pres.title = "Expense Tracker and Finance Agent";

pres.defineSlideMaster({
  title: "TITLE_SLIDE", background: { color: NAVY },
  objects: [],
  slideNumber: undefined,
});
pres.defineSlideMaster({
  title: "CONTENT", background: { color: WHITE },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.6, y: 0.35, w: 12.1, h: 0.9, fontFace: HEAD, fontSize: 32, bold: true, color: NAVY, align: "left", valign: "middle", margin: 0 }, text: "" } },
  ],
  slideNumber: { x: 12.3, y: 7.0, w: 0.6, h: 0.3, fontFace: BODY, fontSize: 11, color: MUTED, align: "right" },
});

// ---------- helpers ----------
function box(s, x, y, w, h, o = {}) {
  s.addShape(o.round === false ? pres.ShapeType.rect : pres.ShapeType.roundRect, {
    x, y, w, h, rectRadius: o.round === false ? undefined : (o.r ?? 0.12),
    fill: { color: o.fill ?? WHITE }, line: { color: o.line ?? o.fill ?? WHITE, width: o.lw ?? 1, dashType: o.dash },
    objectName: o.name,
  });
  if (o.text !== undefined) {
    s.addText(o.text, {
      x, y, w, h, isTextBox: true, align: o.align ?? "center", valign: o.valign ?? "middle",
      fontFace: o.font ?? BODY, fontSize: o.size ?? 16, bold: o.bold ?? false, color: o.color ?? INK, margin: o.margin ?? 0.08,
    });
  }
}
function arrow(s, x1, y1, x2, y2, o = {}) {
  const line = { color: o.color ?? MUTED, width: o.width ?? 2, endArrowType: "triangle" };
  if (o.dash) line.dashType = o.dash;
  if (o.both) line.beginArrowType = "triangle";
  s.addShape(pres.ShapeType.line, { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1), flipH: x2 < x1, flipV: y2 < y1, line });
}
function label(s, text, x, y, w, h, o = {}) {
  s.addText(text, { x, y, w, h, isTextBox: true, fontFace: BODY, fontSize: o.size ?? 13, color: o.color ?? MUTED, bold: o.bold ?? false, italic: o.italic ?? false, align: o.align ?? "center", valign: o.valign ?? "middle", margin: 0 });
}
function content(title, notes) {
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText(title, { placeholder: "title" });
  if (notes) s.addNotes(notes);
  return s;
}
function bullets(s, items, x, y, w, h, o = {}) {
  s.addText(items.map((t, i) => ({ text: t, options: { bullet: true, breakLine: i < items.length - 1, paraSpaceAfter: o.gap ?? 8 } })),
    { x, y, w, h, isTextBox: true, fontFace: BODY, fontSize: o.size ?? 16, color: o.color ?? INK, valign: "top", margin: 0 });
}

// ---------- 1. Title ----------
{
  const s = pres.addSlide({ masterName: "TITLE_SLIDE" });
  s.addText("Expense Tracker + Finance Agent", { x: 0.8, y: 2.2, w: 11.5, h: 1.2, isTextBox: true, fontFace: HEAD, fontSize: 48, bold: true, color: WHITE, margin: 0 });
  s.addText("Two cooperating AI agents that track spending in Telegram and handle company reimbursements", { x: 0.8, y: 3.5, w: 10.5, h: 1.0, isTextBox: true, fontFace: BODY, fontSize: 22, color: "CADCFC", margin: 0 });
  box(s, 0.8, 5.2, 4.6, 0.6, { fill: AMBER, text: "LangGraph  ·  MCP  ·  A2A", size: 18, bold: true, color: NAVY });
  s.addNotes("One sentence: you chat with a Telegram bot to track personal spending, and for work expenses a second agent checks the company policy and routes the claim to your manager.");
}

// ---------- 2. What it does ----------
{
  const s = content("What the product does", "Two worlds: personal money and work money. The two slash commands /personal and /work keep them apart.");
  const cards = [
    ["Personal money", "Say it in plain words: \"coffee 120\". Budgets, summaries, splitting bills with friends, trip groups, bank statement import.", TEAL],
    ["Work expenses", "\"/work flight 3000\". The bot checks the company policy up front and asks for the receipt right away.", AMBER],
    ["Reimbursement", "Send a claim, the manager approves expense by expense, and you are told exactly what was paid and why.", NAVY],
  ];
  cards.forEach(([h, t, c], i) => {
    const x = 0.6 + i * 4.1;
    box(s, x, 1.7, 3.9, 3.5, { fill: LIGHT, line: LINE });
    box(s, x + 0.3, 2.0, 0.7, 0.7, { fill: c, r: 0.35, text: String(i + 1), size: 22, bold: true, color: c === AMBER ? NAVY : WHITE });
    label(s, h, x + 0.3, 2.9, 3.3, 0.5, { size: 22, bold: true, color: NAVY, align: "left" });
    label(s, t, x + 0.3, 3.45, 3.3, 1.6, { size: 15, color: INK, align: "left", valign: "top" });
  });
  label(s, "Everything happens in Telegram. The employee and the manager each chat with their own bot.", 0.6, 5.7, 12.1, 0.5, { size: 17, color: TEAL, bold: true, align: "left" });
}

// ---------- 3. Architecture (overview, user-supplied diagram) ----------
{
  const s = content("Architecture: the big picture", "Employee chats with the Telegram bot and expense tracker agent. It uses the MCP tool server for data. The tool server talks to the reimbursement agent over A2A. The manager approves or rejects in their own bot.");
  const w = 12.1, h = w * 387 / 1056;
  s.addImage({ path: path.join(__dirname, "images", "architecture-overview.png"), x: 0.6, y: 1.6 + (5.2 - h) / 2, w, h, altText: "Architecture overview: employee, Telegram bot and expense tracker agent, MCP tool server, reimbursement agent, manager, friends, three databases" });
}

// ---------- 3b. Architecture (detailed, user-supplied diagram) ----------
{
  const s = content("Architecture: every component and protocol", "The detailed view. Left: Telegram gateway and LangGraph agent with its LLM and bot database. Centre: the MCP tool server with its database and the outbox. Right: the A2A server, agent card, skills, policy engine and webhook dispatcher of the reimbursement agent.");
  const h = 5.55, w = h * 2272 / 1366;
  s.addImage({ path: path.join(__dirname, "images", "architecture-detailed.png"), x: (13.33 - w) / 2, y: 1.4, w, h, altText: "Detailed architecture with Telegram gateway, LangGraph agent, MCP tool server, A2A server, agent card, skills, policy engine, webhook dispatcher and three databases" });
}

// ---------- 4. Tech stack ----------
{
  const s = content("What it is built with", "Everything runs in Docker Compose: six containers, three Postgres databases.");
  const items = [
    ["LangGraph", "the tracker agent's brain"], ["MCP", "tools the AI can call"], ["A2A (a2a-sdk)", "agent to agent protocol"],
    ["Postgres x3", "tracker, bot, finance"], ["JWT (RS256)", "signed identity"], ["Docker Compose", "6 containers"],
    ["Telegram", "employee + manager bots"], ["Decimal engine", "exact money maths"], ["240 tests", "run on a scratch DB"],
  ];
  items.forEach(([h, t], i) => {
    const x = 0.6 + (i % 3) * 4.1, y = 1.6 + Math.floor(i / 3) * 1.65;
    box(s, x, y, 3.9, 1.4, { fill: LIGHT, line: LINE });
    label(s, h, x + 0.25, y + 0.2, 3.4, 0.55, { size: 22, bold: true, color: TEAL, align: "left" });
    label(s, t, x + 0.25, y + 0.8, 3.4, 0.4, { size: 15, color: MUTED, align: "left" });
  });
}

// ---------- 5. State graph ----------
{
  const s = content("The tracker agent: its LangGraph", "Three nodes. The agent picks tools, tools run them on the MCP server, respond builds the reply from the tool output. A question from the server pauses the graph and nothing is written until the user answers.");
  const y = 3.3, h = 1.1;
  box(s, 0.5, y + 0.15, 1.2, 0.8, { fill: NAVY, r: 0.4, text: "START", size: 14, bold: true, color: WHITE });
  box(s, 2.3, y, 2.4, h, { fill: TEAL, text: "agent\nLLM picks a tool", size: 17, bold: true, color: WHITE });
  box(s, 5.6, y, 2.4, h, { fill: TEAL, text: "tools\nruns it on MCP", size: 17, bold: true, color: WHITE });
  box(s, 8.9, y, 2.4, h, { fill: TEAL, text: "respond\nbuilds the reply", size: 17, bold: true, color: WHITE });
  box(s, 11.8, y + 0.15, 1.0, 0.8, { fill: NAVY, r: 0.4, text: "END", size: 14, bold: true, color: WHITE });
  arrow(s, 1.72, y + 0.55, 2.28, y + 0.55, { color: NAVY });
  arrow(s, 4.72, y + 0.4, 5.58, y + 0.4, { color: NAVY });
  arrow(s, 8.02, y + 0.55, 8.88, y + 0.55, { color: NAVY });
  arrow(s, 11.32, y + 0.55, 11.78, y + 0.55, { color: NAVY });
  label(s, "tool call", 4.72, y + 0.03, 0.9, 0.3, { size: 12 });
  label(s, "all done OK", 8.02, y + 0.18, 0.9, 0.3, { size: 12 });
  // tools loop on itself
  label(s, "more tool calls waiting: stay in tools", 6.95, y + 1.25, 3.8, 0.35, { size: 13, color: TEAL, bold: true, align: "left" });
  // failure back to agent (elbow)
  s.addShape(pres.ShapeType.line, { x: 6.8, y: y + 1.1, w: 0, h: 0.95, line: { color: RED, width: 2 } });
  s.addShape(pres.ShapeType.line, { x: 3.5, y: y + 2.05, w: 3.3, h: 0, line: { color: RED, width: 2 } });
  arrow(s, 3.5, y + 2.05, 3.5, y + 1.12, { color: RED });
  label(s, "a tool failed: one retry", 3.6, y + 2.1, 3.0, 0.35, { size: 13, color: RED, bold: true });
  // pause box above tools
  box(s, 5.6, 1.55, 2.4, 0.8, { fill: AMBER_L, line: AMBER, text: "Pause: ask the user", size: 15, bold: true, color: INK });
  arrow(s, 6.4, y - 0.02, 6.4, 2.37, { color: AMBER, width: 2 });
  arrow(s, 7.2, 2.37, 7.2, y - 0.02, { color: AMBER, width: 2 });
  label(s, "needs an answer", 7.3, 2.65, 1.8, 0.3, { size: 12, align: "left" });
  // refusal above agent
  box(s, 2.3, 1.55, 2.4, 0.8, { fill: LIGHT, line: LINE, text: "Fixed refusal\n(off-topic text)", size: 14, color: INK });
  arrow(s, 3.5, y - 0.02, 3.5, 2.37, { color: MUTED });
  label(s, "Rules: the AI only acts through tools. Free text from the model is never sent to the user.", 0.6, 6.55, 12.1, 0.45, { size: 15, color: TEAL, bold: true, align: "left" });
}

// ---------- 6. Message routing ----------
{
  const s = content("Not every message needs the AI", "Commands, files and buttons are handled directly. Only plain text goes through the LLM graph.");
  box(s, 0.6, 3.0, 2.2, 1.2, { fill: NAVY, text: "Telegram\nmessage", size: 17, bold: true, color: WHITE });
  const rows = [
    ["Button tap", "Resume a paused question or run an action", LIGHT],
    ["Command (/work /join /workpdf)", "Handler runs directly, no guessing", LIGHT],
    ["PDF or photo", "A receipt if one is waiting, otherwise a bank statement", LIGHT],
    ["Plain text", "LangGraph: the LLM picks a tool", TEAL_L],
  ];
  rows.forEach(([a, b, f], i) => {
    const y = 1.6 + i * 1.3;
    box(s, 4.0, y, 8.7, 1.1, { fill: f, line: i === 3 ? TEAL : LINE });
    label(s, a, 4.25, y + 0.1, 3.9, 0.9, { size: 16, bold: true, color: NAVY, align: "left" });
    label(s, b, 8.3, y + 0.1, 4.3, 0.9, { size: 15, color: INK, align: "left" });
    arrow(s, 2.85, 3.6, 3.95, y + 0.55, { color: MUTED, width: 1.5 });
  });
}

// ---------- 7 & 8. A2A sequences ----------
function sequence(title, notes, steps, note) {
  const s = content(title, notes);
  const lanes = [["Employee", 1.5], ["Bot", 4.15], ["MCP server", 6.8], ["Finance Agent", 9.45], ["Manager", 12.0]];
  const idx = { E: 0, B: 1, M: 2, F: 3, G: 4 };
  lanes.forEach(([n, cx], i) => {
    const fill = i === 3 ? AMBER : i === 4 ? NAVY : TEAL;
    box(s, cx - 1.0, 1.45, 2.0, 0.55, { fill, text: n, size: 15, bold: true, color: i === 3 ? NAVY : WHITE });
    s.addShape(pres.ShapeType.line, { x: cx, y: 2.0, w: 0, h: 4.7, line: { color: LINE, width: 1.25, dashType: "dash" } });
  });
  const y0 = 2.4, dy = steps.length > 7 ? 0.52 : 0.6;
  steps.forEach(([f, t, text, kind], i) => {
    const y = y0 + i * dy, x1 = lanes[idx[f]][1], x2 = lanes[idx[t]][1];
    const dir = x2 > x1 ? 1 : -1;
    const color = kind === "a2a" ? "B26A00" : kind === "web" ? "B26A00" : NAVY;
    arrow(s, x1 + 0.06 * dir, y + 0.3, x2 - 0.06 * dir, y + 0.3, { color, width: kind ? 2.75 : 2, dash: kind === "web" ? "dash" : undefined });
    const lw = Math.max(Math.abs(x2 - x1) - 0.1, 2.4), cx = (x1 + x2) / 2;
    s.addText(`${i + 1}  ${text}`, { x: cx - lw / 2, y: y - 0.02, w: lw, h: 0.3, isTextBox: true, align: "center", valign: "middle", fontFace: BODY, fontSize: 13, bold: !!kind, color: kind ? "B26A00" : INK, margin: 0, fill: { color: WHITE } });
  });
  if (note) label(s, note, 0.6, 6.8, 11.6, 0.4, { size: 14, color: TEAL, bold: true, align: "left" });
  return s;
}
sequence("Example 1: \"2k on breakfast, for work\"",
  "The tool name is fixed in code. The finance agent's LLM only finds which policy rule fits, and the engine produces the numbers. Finance saves nothing during a check.",
  [
    ["E", "B", "I paid 2k on breakfast (work)"],
    ["B", "M", "add_work_expense tool"],
    ["M", "F", "A2A: check_expense", "a2a"],
    ["F", "M", "input-required: receipt?", "a2a"],
    ["M", "B", "question + policy limit"],
    ["B", "E", "Please send the receipt"],
    ["E", "B", "photo of the receipt"],
  ],
  "The bot's file handler stores the photo with that expense (attach_receipt). No AI involved.");
sequence("Example 2: send the claim, get the decision",
  "The manager decides expense by expense. Finance saves the decision and queues the webhook in the same transaction, so a crash never loses a decision.",
  [
    ["E", "B", "send my claim"],
    ["B", "M", "submit_claim tool"],
    ["M", "F", "A2A: submit_claim + receipts", "a2a"],
    ["F", "G", "approval card + receipts"],
    ["G", "F", "Approve"],
    ["F", "M", "signed webhook: decision", "web"],
    ["M", "B", "message queued (outbox)"],
    ["B", "E", "Approved Rs 350 of Rs 2,000"],
  ]);

// ---------- 9. Where the LLM is used ----------
{
  const s = content("The AI reads. The code decides.", "This is the key safety message. The LLM understands language at the edges; amounts, eligibility and status changes are ordinary code.");
  box(s, 0.6, 1.6, 5.9, 3.9, { fill: TEAL_L, line: TEAL });
  label(s, "The LLM only reads language", 0.9, 1.8, 5.3, 0.5, { size: 22, bold: true, color: TEAL, align: "left" });
  bullets(s, ["Tracker: pick which tool to call", "Finance: turn a policy PDF into rules", "Finance: match an expense to a policy rule", "Finance: understand a policy question", "Finance: short answers to the manager"], 0.9, 2.5, 5.3, 3.5, { size: 17 });
  box(s, 6.8, 1.6, 5.9, 3.9, { fill: AMBER_L, line: AMBER });
  label(s, "Code decides everything else", 7.1, 1.8, 5.3, 0.5, { size: 22, bold: true, color: "B26A00", align: "left" });
  bullets(s, ["Exact Decimal money maths: caps, percent, nights", "Which skill runs (fixed by name)", "Claim status changes", "Policy saved only after the manager taps Save", "The manager always makes the final decision"], 7.1, 2.5, 5.3, 3.5, { size: 17 });
  label(s, "Finance has no LangGraph: its steps are known, so it is plain, predictable code.", 0.6, 5.9, 12.1, 0.45, { size: 17, color: TEAL, bold: true, align: "left" });
}

// ---------- 10. Claim lifecycle ----------
{
  const s = content("A claim's journey", "Status names in the code: sending, needs_info, awaiting_manager, info_requested, approved, rejected, paid, failed.");
  const w = 2.3, h = 0.95, y = 3.4;
  const pos = { send: 0.6, mgr: 3.95, app: 7.3, paid: 10.45 };
  box(s, pos.send, y, w, h, { fill: TEAL, text: "Sending", size: 18, bold: true, color: WHITE });
  box(s, pos.mgr, y, w, h, { fill: AMBER, text: "With the manager", size: 18, bold: true, color: NAVY });
  box(s, pos.app, y, w, h, { fill: GREEN, text: "Approved", size: 18, bold: true, color: WHITE });
  box(s, pos.paid, y, 2.3, h, { fill: NAVY, text: "Paid", size: 18, bold: true, color: WHITE });
  arrow(s, pos.send + w + 0.03, y + h / 2, pos.mgr - 0.03, y + h / 2, { color: NAVY });
  arrow(s, pos.mgr + w + 0.03, y + h / 2, pos.app - 0.03, y + h / 2, { color: NAVY });
  arrow(s, pos.app + w + 0.03, y + h / 2, pos.paid - 0.03, y + h / 2, { color: NAVY });
  // side states
  box(s, pos.send, 1.6, w, 0.95, { fill: LIGHT, line: LINE, text: "Needs your details", size: 16, color: INK });
  arrow(s, pos.send + 0.8, 2.58, pos.send + 0.8, y - 0.03, { color: MUTED, width: 1.5 });
  arrow(s, pos.send + 1.5, y - 0.03, pos.send + 1.5, 2.58, { color: MUTED, width: 1.5 });
  box(s, pos.send, 5.2, w, 0.95, { fill: LIGHT, line: LINE, text: "Not sent (retry)", size: 16, color: INK });
  arrow(s, pos.send + 1.15, y + h + 0.03, pos.send + 1.15, 5.17, { color: MUTED, width: 1.5 });
  box(s, pos.mgr, 1.6, w, 0.95, { fill: LIGHT, line: LINE, text: "Manager asks you", size: 16, color: INK });
  arrow(s, pos.mgr + 0.8, 2.58, pos.mgr + 0.8, y - 0.03, { color: MUTED, width: 1.5 });
  arrow(s, pos.mgr + 1.5, y - 0.03, pos.mgr + 1.5, 2.58, { color: MUTED, width: 1.5 });
  box(s, pos.mgr, 5.2, w, 0.95, { fill: "FBE9E7", line: RED, text: "Rejected, with a reason", size: 15, color: INK });
  arrow(s, pos.mgr + 1.15, y + h + 0.03, pos.mgr + 1.15, 5.17, { color: RED, width: 1.5 });
  label(s, "Sent with every receipt. If the policy needs more, the employee is asked before it reaches the manager.", 6.5, 5.25, 6.2, 1.0, { size: 15, color: MUTED, align: "left" });
  label(s, "Partly approved? The manager decides expense by expense and the employee sees each one.", 6.5, 5.95, 6.2, 0.7, { size: 15, color: TEAL, bold: true, align: "left" });
}

// ---------- 11. Security ----------
{
  const s = content("Who trusts whom", "Every hop has its own proof of identity. Finance never sees the user's other data, only the ids it needs.");
  const rows = [
    ["Employee to Bot", "Telegram user id"],
    ["Bot to MCP server", "RS256 JWT for that person, revocable, audited"],
    ["Tracker to Finance", "2-minute RS256 JWT, only valid for Finance, checked against the tracker's public key"],
    ["Finance to Tracker", "HMAC-signed webhook (X-Signature)"],
    ["Manager", "Telegram id registered to the company"],
  ];
  rows.forEach(([a, b], i) => {
    const y = 1.6 + i * 1.0;
    box(s, 0.6, y, 3.8, 0.85, { fill: i === 2 || i === 3 ? AMBER : TEAL, text: a, size: 17, bold: true, color: i === 2 || i === 3 ? NAVY : WHITE });
    box(s, 4.5, y, 8.2, 0.85, { fill: LIGHT, line: LINE, text: b, size: 16, align: "left", margin: 14 });
  });
  label(s, "Company codes (FIN-XXXXX) decide who can join. Tokens and personal text never go into logs.", 0.6, 6.7, 12.1, 0.4, { size: 14, color: TEAL, bold: true, align: "left" });
}

// ---------- 12. Reliability ----------
{
  const s = content("Built so it does not lose or double things", "These are the answers to 'what if it crashes in the middle?'");
  const cards = [
    ["Outbox", "Messages and webhooks are saved in the same transaction as the change, then delivered with retries."],
    ["Idempotency", "A repeated request is a no-op, so nothing is written twice."],
    ["Soft delete", "Undoing an expense marks it as voided instead of deleting it, so history stays intact."],
    ["No open transactions", "No LLM or network call runs while a DB transaction is open."],
    ["Exact money", "Numeric(14,2) and Decimal, never floats."],
    ["Versioned policy", "Every saved policy is a new version, and each claim records which one it used."],
  ];
  cards.forEach(([h, t], i) => {
    const x = 0.6 + (i % 3) * 4.1, y = 1.6 + Math.floor(i / 3) * 2.5;
    box(s, x, y, 3.9, 2.25, { fill: LIGHT, line: LINE });
    label(s, h, x + 0.25, y + 0.2, 3.4, 0.5, { size: 20, bold: true, color: TEAL, align: "left" });
    label(s, t, x + 0.25, y + 0.8, 3.4, 1.3, { size: 15, color: INK, align: "left", valign: "top" });
  });
}

// ---------- 13. Data ----------
{
  const s = content("Three databases, linked only by ids", "No foreign keys cross services. Each service only holds the other's ids.");
  const dbs = [
    ["Tracker DB", "users, expenses, budgets, splits, work expenses, receipts, work claims", TEAL],
    ["Finance DB", "companies, policy versions, employees, claims, receipts, manager state", AMBER],
    ["Bot DB", "LangGraph chat memory", NAVY],
  ];
  dbs.forEach(([h, t, c], i) => {
    const x = 0.6 + i * 4.1;
    box(s, x, 1.6, 3.9, 2.2, { fill: LIGHT, line: c, lw: 2 });
    label(s, h, x + 0.25, 1.75, 3.4, 0.5, { size: 22, bold: true, color: c === AMBER ? "B26A00" : c, align: "left" });
    label(s, t, x + 0.25, 2.3, 3.4, 1.4, { size: 15, color: INK, align: "left", valign: "top" });
  });
  label(s, "How the two business databases connect (plain values, not database links)", 0.6, 4.1, 12.1, 0.4, { size: 17, bold: true, color: NAVY, align: "left" });
  const links = [["users.id", "employees.ref"], ["company_links.company_code", "companies.code"], ["work_claims.remote_claim_id", "claims.id"], ["work_expenses.id", "claim item ref"]];
  links.forEach(([a, b], i) => {
    const x = 0.6 + (i % 2) * 6.2, y = 4.65 + Math.floor(i / 2) * 0.9;
    box(s, x, y, 2.6, 0.65, { fill: TEAL_L, line: TEAL, text: a, size: 13, color: INK });
    arrow(s, x + 2.65, y + 0.33, x + 3.35, y + 0.33, { color: MUTED, both: true });
    box(s, x + 3.4, y, 2.5, 0.65, { fill: AMBER_L, line: AMBER, text: b, size: 13, color: INK });
  });
}

// ---------- 14. Honest limits ----------
{
  const s = content("Honest limits and next steps", "Say these before someone asks. It shows you know the system.");
  box(s, 0.6, 1.6, 5.9, 4.0, { fill: LIGHT, line: LINE });
  label(s, "Today", 0.9, 1.8, 5.3, 0.5, { size: 22, bold: true, color: NAVY, align: "left" });
  bullets(s, ["Receipts are not checked for authenticity", "Approval tiers, deadlines and per-year limits are not enforced", "One company per person; \"paid\" is marked by hand", "A2A is used in a controlled way: skills are chosen in code, and the decision returns by webhook"], 0.9, 2.5, 5.3, 3.8, { size: 16 });
  box(s, 6.8, 1.6, 5.9, 4.0, { fill: TEAL_L, line: TEAL });
  label(s, "Next", 7.1, 1.8, 5.3, 0.5, { size: 22, bold: true, color: TEAL, align: "left" });
  bullets(s, ["Use A2A push notifications instead of the custom webhook", "Let the tracker also be a discoverable A2A agent", "Track yearly limits across claims", "OAuth sign-in for users outside Telegram"], 7.1, 2.5, 5.3, 3.8, { size: 16 });
}

// ---------- 15. Q&A ----------
{
  const s = content("Questions you may get", "Short, honest answers.");
  const qa = [
    ["Why LangGraph for one agent and not the other?", "Free text needs an agent loop. Finance gets defined requests, so plain code is safer."],
    ["Does the AI calculate refunds?", "No. An exact Decimal engine does the maths. The LLM only reads text."],
    ["Why can't the bot call Finance?", "Only the MCP server holds Finance credentials, so every outside call is checked and logged in one place."],
    ["Why a webhook, not a reply?", "A manager may take days. The call returns fast and the decision arrives later."],
    ["What if a service crashes?", "Outbox tables retry delivery, and requests are idempotent."],
    ["Is it really agent to agent?", "Yes: separate services, own data, standard protocol. The routing is deliberately fixed."],
  ];
  qa.forEach(([q, a], i) => {
    const x = 0.6 + (i % 2) * 6.15, y = 1.5 + Math.floor(i / 2) * 1.75;
    box(s, x, y, 5.95, 1.55, { fill: LIGHT, line: LINE });
    label(s, q, x + 0.2, y + 0.1, 5.55, 0.55, { size: 15, bold: true, color: NAVY, align: "left" });
    label(s, a, x + 0.2, y + 0.65, 5.55, 0.8, { size: 14, color: INK, align: "left", valign: "top" });
  });
}

// ---------- 16. Closing ----------
{
  const s = pres.addSlide({ masterName: "TITLE_SLIDE" });
  s.addText("Demo time", { x: 0.8, y: 2.3, w: 11.5, h: 1.2, isTextBox: true, fontFace: HEAD, fontSize: 48, bold: true, color: WHITE, margin: 0 });
  s.addText("/join  ->  /work flight 3000  ->  send receipt  ->  send claim  ->  manager decides", { x: 0.8, y: 3.7, w: 11.5, h: 0.8, isTextBox: true, fontFace: BODY, fontSize: 22, color: "CADCFC", margin: 0 });
  s.addText("Thank you", { x: 0.8, y: 5.2, w: 6, h: 0.6, isTextBox: true, fontFace: BODY, fontSize: 20, color: AMBER, bold: true, margin: 0 });
}

(async () => {
  const out = process.env.DECK_OUT || path.join(__dirname, "Expense-Tracker-and-Finance-Agent.pptx");
  await pres.writeFile({ fileName: out });
  if (applyTheme) await applyTheme(out, THEME);
  console.log("wrote", out);
})();
