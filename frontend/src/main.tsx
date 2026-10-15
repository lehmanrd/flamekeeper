import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { BASE } from "./api/client";
import { AuthGate } from "./components/AuthGate";
import { JobsProvider } from "./components/JobsDrawer";
import { Layout } from "./components/Layout";
import { NewWorld } from "./routes/NewWorld";
import { NotFound } from "./routes/NotFound";
import { Overview } from "./routes/Overview";
import { Settings } from "./routes/Settings";
import { WorldBackups } from "./routes/WorldBackups";
import { WorldPlayers } from "./routes/WorldPlayers";
import { WorldUpdates } from "./routes/WorldUpdates";
import { WorldDifficulty } from "./routes/WorldDifficulty";
import { WorldLogs } from "./routes/WorldLogs";
import { WorldOverview } from "./routes/WorldOverview";
import { WorldRoles } from "./routes/WorldRoles";
import { WorldServer } from "./routes/WorldServer";
import "./styles/global.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

// A data router, so editors can block navigation while they have unsaved changes.
const router = createBrowserRouter(
  [
    {
      element: <Layout />,
      children: [
        { index: true, element: <Overview /> },
        { path: "new", element: <NewWorld /> },
        { path: "worlds/:slug", element: <WorldOverview /> },
        { path: "worlds/:slug/difficulty", element: <WorldDifficulty /> },
        { path: "worlds/:slug/server", element: <WorldServer /> },
        { path: "worlds/:slug/roles", element: <WorldRoles /> },
        { path: "worlds/:slug/backups", element: <WorldBackups /> },
        { path: "worlds/:slug/updates", element: <WorldUpdates /> },
        { path: "worlds/:slug/players", element: <WorldPlayers /> },
        { path: "worlds/:slug/logs", element: <WorldLogs /> },
        { path: "settings", element: <Settings /> },
        { path: "*", element: <NotFound /> },
      ],
    },
  ],
  { basename: BASE || "/" },
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthGate>
        <JobsProvider>
          <RouterProvider router={router} />
        </JobsProvider>
      </AuthGate>
    </QueryClientProvider>
  </StrictMode>,
);
