import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "./index.css";
import { Shell } from "./components/Shell";
import { Home } from "./pages/Home";
import { TaskPage } from "./pages/TaskPage";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Shell>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/tasks/:taskId" element={<TaskPage />} />
          <Route path="/runs/:taskId" element={<TaskPage />} />
          <Route path="*" element={<Home />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  </StrictMode>,
);
