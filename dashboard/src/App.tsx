import { createBrowserRouter, RouterProvider, useParams } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { Overview } from "@/pages/Overview";
import { NodesList } from "@/pages/NodesList";
import { NodeDetail } from "@/pages/NodeDetail";
import { JobsList } from "@/pages/JobsList";
import { JobDetail } from "@/pages/JobDetail";
import { Submit } from "@/pages/Submit";
import { Benchmarks } from "@/pages/Benchmarks";
import { NotFound } from "@/pages/NotFound";

/**
 * Keys JobDetail by the route's :jobId so navigating from one job straight to
 * another (without an intervening unmount) gets a genuinely fresh JobDetail
 * instance — its live-logs/metrics polling state starts clean for the new
 * job rather than needing manual "reset when this prop changes" bookkeeping.
 */
function JobDetailRoute() {
  const { jobId } = useParams<{ jobId: string }>();
  return <JobDetail key={jobId} />;
}

/**
 * React Router (data mode) owns navigation/path-matching only. Server state
 * (nodes, jobs) is owned entirely by TanStack Query inside each page — no
 * loaders/actions here, per ADR-011's routing decision.
 */
const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { index: true, element: <Overview /> },
      { path: "nodes", element: <NodesList /> },
      { path: "nodes/:nodeId", element: <NodeDetail /> },
      { path: "jobs", element: <JobsList /> },
      { path: "jobs/:jobId", element: <JobDetailRoute /> },
      { path: "submit", element: <Submit /> },
      { path: "benchmarks", element: <Benchmarks /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

function App() {
  return <RouterProvider router={router} />;
}

export default App;
