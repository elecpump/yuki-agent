function visibleTextFromDocument() {
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  const chunks = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const parent = node.parentElement;
    if (!parent) continue;
    const style = window.getComputedStyle(parent);
    if (style.visibility === "hidden" || style.display === "none") continue;
    const rect = parent.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const text = node.textContent.replace(/\s+/g, " ").trim();
    if (text) chunks.push(text);
    if (chunks.join("\n").length > 50000) break;
  }
  return chunks.join("\n");
}

function sendSnapshot() {
  chrome.runtime.sendMessage({
    kind: "yuki_dom_text",
    title: document.title || "",
    url: location.href,
    text: visibleTextFromDocument(),
    scroll_percent: Math.round(
      (window.scrollY / Math.max(1, document.documentElement.scrollHeight - window.innerHeight)) * 100
    ),
    ts: Date.now() / 1000
  });
}

let timer = null;
function scheduleSnapshot() {
  clearTimeout(timer);
  timer = setTimeout(sendSnapshot, 150);
}

window.addEventListener("focus", scheduleSnapshot, true);
window.addEventListener("scroll", scheduleSnapshot, { passive: true });
document.addEventListener("visibilitychange", scheduleSnapshot);
scheduleSnapshot();
