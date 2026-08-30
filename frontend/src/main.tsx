import "@ant-design/v5-patch-for-react-19";
import { ConfigProvider, theme } from "antd";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles/global.css";
import { yukiTheme } from "./styles/theme";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider theme={{ ...yukiTheme, algorithm: theme.darkAlgorithm }}>
      <App />
    </ConfigProvider>
  </StrictMode>,
);
