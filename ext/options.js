// The one setting: the bridge token (chrome.storage.local). Saving it makes the background worker connect at once.
// Below it, the link to the bridge as the worker last saw it.
const input = document.getElementById("token");
const status = document.getElementById("status");
const link = document.getElementById("link");

function showLink(state) {
  link.textContent = state ? `Bridge: ${state.link} (${new Date(state.at).toLocaleTimeString()})` : "";
}

chrome.storage.local.get(["token", "bridge_state"]).then(({ token, bridge_state }) => {
  if (token) input.value = token;
  showLink(bridge_state);
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.bridge_state) showLink(changes.bridge_state.newValue);
});

document.getElementById("save").addEventListener("click", async () => {
  const token = input.value.trim();
  await chrome.storage.local.set({ token });
  status.textContent = token ? "Saved — connecting to the bridge." : "Cleared.";
  chrome.runtime.sendMessage({ type: "token-saved" }).catch(() => {});
});
