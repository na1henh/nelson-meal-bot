// "Ask" chat panel for the meal-plan page.
// Talks to Claude straight from the phone with the owner's own Anthropic API key, which is
// kept only in this browser's localStorage. The key is never part of the website or repo.
import Anthropic from "https://cdn.jsdelivr.net/npm/@anthropic-ai/sdk@1.3.0/+esm";

const KEY_STORE = "nelson-anthropic-key";
const MODEL = "claude-opus-5-5";

const SYSTEM = `You are the chat helper inside a personal meal-planning page for a college student at Ohio University. He eats lunch and dinner at the dining halls (Nelson Court, and The District in Boyd Hall for Tuesday lunch). His goal is a slow lean muscle gain ("maingain") with the best long-term health: high protein, plenty of fiber, fruit and vegetables, limited saturated fat, sodium, processed meat and fried food, and one dessert a day.

How to answer:
- Plain text only, no markdown, no headings. Keep it short: one to five sentences, or a short list with "- " bullets.
- When you suggest food, use only items from the menu given for that meal and that day, with the dining hall's posted serving sizes (for example "2 servings (8 oz)"), and use the nutrition numbers given. If the full menu isn't provided, say you can only see the planned items.
- When asked how he's doing, use the plan's daily targets and what he has ticked off as eaten.
- The nutrition numbers are the dining hall's approximate labels; say so if precision matters.
- Don't give medical advice. For medical questions, or questions about supplements and medications, suggest a doctor or the free OU Culinary Services dietitian (culinarydietitian@ohio.edu).
- If a question isn't about food, nutrition, training or this plan, answer briefly and steer back.`;

const STARTERS = [
  "If the chicken is gone at dinner, what should I get instead?",
  "How much protein do I still need today?",
  "What's a good snack to hit my calories?",
  "Which dessert fits best tonight?",
];

let client = null;
let history = [];        // messages sent to Claude (append-only for this day)
let historyDate = null;
let menuCache = {};      // date -> menu JSON (or null when not published)
let busy = false;

// ---------------------------------------------------------------- helpers

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function getKey() {
  try { return localStorage.getItem(KEY_STORE) || ""; } catch (e) { return ""; }
}
function setKey(v) {
  try { v ? localStorage.setItem(KEY_STORE, v) : localStorage.removeItem(KEY_STORE); } catch (e) {}
  client = null;
}
function getClient() {
  if (!client) client = new Anthropic({ apiKey: getKey(), dangerouslyAllowBrowser: true });
  return client;
}
async function loadMenu(date) {
  if (date in menuCache) return menuCache[date];
  try {
    const r = await fetch("menus/" + date + ".json", { cache: "no-store" });
    menuCache[date] = r.ok ? await r.json() : null;
  } catch (e) {
    menuCache[date] = null;
  }
  return menuCache[date];
}
function planForPrompt(plan) {
  // Only what the helper needs; drop UI-only fields.
  return {
    date: plan.date, label: plan.label, targets: plan.targets, totals: plan.totals,
    meals: plan.meals.map((m) => ({
      meal: m.meal, place: m.place, target: m.target, totals: m.totals,
      items: m.items.map((it) => ({ name: it.name, station: it.station, portion: it.portion,
        kcal: it.kcal, protein: it.protein, carbs: it.carbs, fat: it.fat, fiber: it.fiber, sodium: it.sodium })),
      backups: m.backups,
    })),
    notes: plan.notes,
  };
}

// ---------------------------------------------------------------- UI

const askBtn = el("button", "ask-btn", "Ask");
askBtn.type = "button";
askBtn.setAttribute("aria-label", "Ask a question about today's plan");
document.body.append(askBtn);

const panel = el("section", "chat");
panel.hidden = true;
panel.setAttribute("aria-label", "Meal plan chat");
const head = el("div", "chat-head");
const title = el("strong", null, "Ask about today");
const headBtns = el("div");
headBtns.style.display = "flex";
headBtns.style.gap = "6px";
const keyBtn = el("button", "icon-btn", "Key");
keyBtn.type = "button";
const closeBtn = el("button", "icon-btn", "Close");
closeBtn.type = "button";
headBtns.append(keyBtn, closeBtn);
head.append(title, headBtns);
const log = el("div", "chat-log");
log.setAttribute("aria-live", "polite");
const form = el("form", "chat-form");
const input = el("input");
input.id = "chat-input";
input.type = "text";
input.placeholder = "Ask about today's food...";
input.autocomplete = "off";
input.setAttribute("aria-label", "Your question");
const sendBtn = el("button", null, "Send");
sendBtn.type = "submit";
form.append(input, sendBtn);
panel.append(head, log, form);
document.body.append(panel);

function addMsg(kind, text) {
  const m = el("div", "msg " + kind, text);
  log.append(m);
  log.scrollTop = log.scrollHeight;
  return m;
}

function showKeyForm() {
  log.replaceChildren();
  form.hidden = true;
  const box = el("form", "key-form");
  box.append(el("p", null, "The chat uses your own Anthropic API key. It's saved only on this phone and sent only to Anthropic."));
  const p2 = el("p");
  p2.append("Create one at ");
  const a = el("a", null, "console.anthropic.com");
  a.href = "https://console.anthropic.com/settings/keys";
  a.target = "_blank";
  a.rel = "noopener";
  p2.append(a, " (pay-as-you-go: roughly a few cents per question with Claude Opus 5.5).");
  const row = el("div", "row");
  const keyInput = el("input");
  keyInput.id = "api-key-input";
  keyInput.type = "password";
  keyInput.placeholder = "sk-ant-...";
  keyInput.autocomplete = "off";
  keyInput.setAttribute("aria-label", "Anthropic API key");
  const save = el("button", null, "Save");
  save.type = "submit";
  row.append(keyInput, save);
  box.append(p2, row);
  if (getKey()) {
    const remove = el("button", "chip", "Remove the saved key from this phone");
    remove.type = "button";
    remove.addEventListener("click", () => { setKey(""); showKeyForm(); });
    box.append(remove);
  }
  box.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = keyInput.value.trim();
    if (!v) return;
    setKey(v);
    showChat();
  });
  log.append(box);
}

function showChat() {
  form.hidden = false;
  log.replaceChildren();
  const cur = window.nelsonCurrent;
  if (!cur) {
    addMsg("note", "The plan is still loading.");
    return;
  }
  if (historyDate !== cur.plan.date) { history = []; historyDate = cur.plan.date; }
  addMsg("note", "Answers use " + (cur.plan.label || cur.plan.date) + "'s plan and menu, and the foods you've ticked off.");
  if (!history.length) {
    const chips = el("div", "chips");
    STARTERS.forEach((q) => {
      const c = el("button", "chip", q);
      c.type = "button";
      c.addEventListener("click", () => ask(q));
      chips.append(c);
    });
    log.append(chips);
  } else {
    history.forEach((m) => {
      if (m.role === "user") addMsg("user", m.display || "");
      else addMsg("bot", (m.content || []).filter((b) => b.type === "text").map((b) => b.text).join(""));
    });
  }
  setTimeout(() => input.focus(), 50);
}

function open() {
  panel.hidden = false;
  askBtn.hidden = true;
  getKey() ? showChat() : showKeyForm();
}
function close() {
  panel.hidden = true;
  askBtn.hidden = false;
}
askBtn.addEventListener("click", open);
closeBtn.addEventListener("click", close);
keyBtn.addEventListener("click", showKeyForm);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });
window.addEventListener("nelson:plan", () => {
  const cur = window.nelsonCurrent;
  if (cur && historyDate && historyDate !== cur.plan.date && !panel.hidden && getKey()) showChat();
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (q) ask(q);
});

// ---------------------------------------------------------------- ask Claude

async function ask(question) {
  if (busy) return;
  const cur = window.nelsonCurrent;
  if (!cur || !getKey()) return;
  busy = true;
  sendBtn.disabled = true;
  input.value = "";
  log.querySelectorAll(".chips").forEach((c) => c.remove());
  addMsg("user", question);
  const reply = addMsg("bot", "...");

  const plan = cur.plan;
  const menu = await loadMenu(plan.date);
  const context =
    "Today's plan (" + plan.date + "):\n<plan>\n" + JSON.stringify(planForPrompt(plan)) + "\n</plan>\n\n" +
    (menu ? "Full menu with nutrition per posted serving:\n<menu>\n" + JSON.stringify(menu) + "\n</menu>"
          : "The full menu for this day isn't available; only the planned items are known.");

  // What he has ticked off changes during the day, so it goes with the question,
  // after the cached context.
  const eaten = cur.eaten || [];
  const eatenKcal = eaten.reduce((s, x) => s + x.kcal, 0);
  const eatenProtein = eaten.reduce((s, x) => s + x.protein, 0);
  const now = new Date();
  const status = "[Time now: " + now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) +
    ". Ticked off as eaten so far: " +
    (eaten.length ? eaten.map((x) => x.name + " (" + x.meal + ")").join(", ") +
      " = " + Math.round(eatenKcal) + " kcal, " + Math.round(eatenProtein) + " g protein" : "nothing yet") + ".]";

  history.push({ role: "user", content: status + "\n\n" + question, display: question });
  const messages = history.map((m) => ({ role: m.role, content: m.content }));

  try {
    const stream = getClient().beta.messages.stream({
      model: MODEL,
      max_tokens: 4000,
      betas: ["server-side-fallback-2026-07-01"],
      fallbacks: "default",
      output_config: { effort: "low" },
      system: [
        { type: "text", text: SYSTEM },
        { type: "text", text: context, cache_control: { type: "ephemeral" } },
      ],
      messages,
    });
    let text = "";
    stream.on("text", (delta) => {
      text += delta;
      reply.textContent = text;
      log.scrollTop = log.scrollHeight;
    });
    const final = await stream.finalMessage();
    if (final.stop_reason === "refusal") {
      reply.textContent = "Claude declined to answer that one. Try asking it a different way.";
      history.pop();
    } else {
      if (!text) reply.textContent = final.content.filter((b) => b.type === "text").map((b) => b.text).join("") || "(no answer)";
      // Keep the full content (thinking blocks included) so later turns stay valid.
      history.push({ role: "assistant", content: final.content });
    }
  } catch (err) {
    history.pop();
    reply.remove();
    let msg = "Something went wrong. Check your connection and try again.";
    if (err instanceof Anthropic.AuthenticationError) msg = "Anthropic rejected the saved API key. Tap Key to enter a new one.";
    else if (err instanceof Anthropic.PermissionDeniedError) msg = "This API key isn't allowed to use " + MODEL + ". Check the key's workspace in the Anthropic Console.";
    else if (err instanceof Anthropic.RateLimitError) msg = "Too many questions at once. Wait a minute and try again.";
    else if (err instanceof Anthropic.BadRequestError && /credit/i.test(err.message || "")) msg = "Your Anthropic account is out of credits. Add credits in the Anthropic Console.";
    else if (err instanceof Anthropic.APIConnectionError) msg = "Couldn't reach Anthropic. Check your connection and try again.";
    else if (err instanceof Anthropic.APIError) msg = "Anthropic returned an error (" + (err.status || "unknown") + "): " + (err.message || "");
    addMsg("error", msg);
  } finally {
    busy = false;
    sendBtn.disabled = false;
  }
}
