const count = document.querySelector("#count");
const button = document.querySelector("#increment");
const status = document.querySelector("#status");

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json();
}

async function loadCount() {
  try {
    const data = await request("/api/count");
    count.value = data.count;
  } catch {
    status.textContent = "The saved count could not be loaded.";
  }
}

button.addEventListener("click", async () => {
  button.disabled = true;
  status.textContent = "Saving…";
  try {
    const data = await request("/api/increment", { method: "POST" });
    count.value = data.count;
    status.textContent = "Saved locally.";
  } catch {
    status.textContent = "The count could not be saved. Try again.";
  } finally {
    button.disabled = false;
  }
});

loadCount();
