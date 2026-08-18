const HOST = "com.yuki.dom_text";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.kind !== "yuki_dom_text") return false;
  chrome.runtime.sendNativeMessage(
    HOST,
    {
      title: message.title || "",
      url: message.url || sender.tab?.url || "",
      text: message.text || "",
      scroll_percent: message.scroll_percent,
      ts: message.ts || Date.now() / 1000
    },
    response => {
      sendResponse(response || { ok: false, error: chrome.runtime.lastError?.message || "no_response" });
    }
  );
  return true;
});
