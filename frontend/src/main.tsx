import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import "./styles.css";
import App from "./App";
import { applyAppearanceTheme, readAppearanceTheme } from "./lib/theme";

applyAppearanceTheme(readAppearanceTheme());

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
