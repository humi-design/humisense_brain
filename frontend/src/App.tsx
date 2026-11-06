/**
 * Application shell: routing, layout, and the sign-in gate.
 *
 * Route guards here are navigational only. The API authorizes every request, so a user
 * who types a URL directly still cannot read another tenant's data.
 */

import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import {
  Activity,
  BellRing,
  Boxes,
  Building2,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Plug,
  ScrollText,
  ShieldAlert,
  ShieldCheck,
  UserCheck,
  Workflow,
} from "lucide-react";
import { useSession } from "./hooks/useSession";
import { Button, Spinner } from "./components/ui";
import { SignInPage } from "./pages/SignInPage";
import { OverviewPage } from "./pages/OverviewPage";
import { CatalogPage } from "./pages/CatalogPage";
import { ConnectionsPage } from "./pages/ConnectionsPage";
import { ConnectionDetailPage } from "./pages/ConnectionDetailPage";
import { AddConnectionPage } from "./pages/AddConnectionPage";
import { AuditPage } from "./pages/AuditPage";
import { MyWorkPage } from "./pages/MyWorkPage";
import { WorkListPage } from "./pages/WorkListPage";
import { WorkDetailPage } from "./pages/WorkDetailPage";
import { DepartmentPage } from "./pages/DepartmentPage";
import { FollowUpPage } from "./pages/FollowUpPage";
import { WorkflowPage } from "./pages/WorkflowPage";
import { ExecutivePage } from "./pages/ExecutivePage";
import { NotFoundPage } from "./pages/NotFoundPage";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true, permission: null },
  { to: "/my-work", label: "My work", icon: UserCheck, end: false, permission: "work:read" },
  { to: "/work", label: "Work", icon: ListChecks, end: false, permission: "work:read" },
  { to: "/departments", label: "Departments", icon: Building2, end: false, permission: "work:read" },
  { to: "/follow-ups", label: "Follow-ups", icon: BellRing, end: false, permission: "work:read" },
  { to: "/workflows", label: "Workflows", icon: Workflow, end: false, permission: "work:read" },
  { to: "/executive", label: "Executive", icon: ShieldAlert, end: false, permission: "work:read" },
  { to: "/catalog", label: "Connector catalog", icon: Boxes, end: false, permission: "connectors:read" },
  { to: "/connections", label: "Connections", icon: Plug, end: false, permission: "connections:read" },
  { to: "/audit", label: "Audit trail", icon: ScrollText, end: false, permission: "audit:read" },
] as const;

function Sidebar() {
  const { user, can, signOut } = useSession();
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-border-subtle bg-surface">
      <div className="flex items-center gap-2.5 border-b border-border-subtle px-5 py-4">
        <span className="rounded-lg bg-accent/15 p-2 text-accent" aria-hidden="true">
          <Activity size={18} />
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">Humisense Brain</p>
          <p className="truncate text-xs text-ink-faint">Layer 1 · Sensory platform</p>
        </div>
      </div>

      <nav aria-label="Primary" className="flex-1 space-y-1 p-3">
        {NAV.filter((item) => item.permission === null || can(item.permission)).map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                isActive
                  ? "bg-accent/15 font-medium text-accent"
                  : "text-ink-muted hover:bg-surface-raised hover:text-ink"
              }`
            }
          >
            <item.icon size={17} aria-hidden="true" />
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-border-subtle p-3">
        <div className="mb-2 rounded-lg bg-surface-raised px-3 py-2.5">
          <p className="truncate text-sm font-medium text-ink">
            {user?.display_name ?? user?.email ?? "Signed in"}
          </p>
          <p className="truncate text-xs text-ink-faint">{user?.organization_name}</p>
          <p className="mt-1 inline-flex items-center gap-1 text-xs text-ink-faint">
            <ShieldCheck size={12} aria-hidden="true" />
            {user?.role}
          </p>
        </div>
        <Button
          variant="ghost"
          icon={LogOut}
          className="w-full"
          onClick={() => {
            void signOut();
          }}
        >
          Sign out
        </Button>
      </div>
    </aside>
  );
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useSession();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner label="Checking session…" />
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/sign-in" state={{ from: location.pathname }} replace />;
  }
  return <>{children}</>;
}

function RequirePermission({
  permission,
  children,
}: {
  permission: string;
  children: React.ReactNode;
}) {
  const { can } = useSession();
  if (!can(permission)) {
    return (
      <div className="p-8">
        <div className="rounded-xl border border-warn/40 bg-warn/10 p-6 text-sm text-warn">
          <p className="font-semibold">You do not have access to this area.</p>
          <p className="mt-1 text-warn/90">
            Your role does not include the <code className="font-mono">{permission}</code>{" "}
            permission. Ask an administrator if you need it.
          </p>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

export function App() {
  const { user, loading } = useSession();

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner label="Loading Humisense Brain…" />
      </div>
    );
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/sign-in" element={<SignInPage />} />
        <Route path="*" element={<Navigate to="/sign-in" replace />} />
      </Routes>
    );
  }

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="min-w-0 flex-1 overflow-y-auto">
        <Routes>
          <Route path="/sign-in" element={<Navigate to="/" replace />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <OverviewPage />
              </RequireAuth>
            }
          />
          <Route
            path="/my-work"
            element={
              <RequirePermission permission="work:read">
                <MyWorkPage />
              </RequirePermission>
            }
          />
          <Route
            path="/work"
            element={
              <RequirePermission permission="work:read">
                <WorkListPage />
              </RequirePermission>
            }
          />
          <Route
            path="/work/:workId"
            element={
              <RequirePermission permission="work:read">
                <WorkDetailPage />
              </RequirePermission>
            }
          />
          <Route
            path="/departments"
            element={
              <RequirePermission permission="work:read">
                <DepartmentPage />
              </RequirePermission>
            }
          />
          <Route
            path="/follow-ups"
            element={
              <RequirePermission permission="work:read">
                <FollowUpPage />
              </RequirePermission>
            }
          />
          <Route
            path="/workflows"
            element={
              <RequirePermission permission="work:read">
                <WorkflowPage />
              </RequirePermission>
            }
          />
          <Route
            path="/executive"
            element={
              <RequirePermission permission="work:read">
                <ExecutivePage />
              </RequirePermission>
            }
          />
          <Route
            path="/catalog"
            element={
              <RequirePermission permission="connectors:read">
                <CatalogPage />
              </RequirePermission>
            }
          />
          <Route
            path="/connections"
            element={
              <RequirePermission permission="connections:read">
                <ConnectionsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/connections/new"
            element={
              <RequirePermission permission="connections:create">
                <AddConnectionPage />
              </RequirePermission>
            }
          />
          <Route
            path="/connections/:connectionId"
            element={
              <RequirePermission permission="connections:read">
                <ConnectionDetailPage />
              </RequirePermission>
            }
          />
          <Route
            path="/audit"
            element={
              <RequirePermission permission="audit:read">
                <AuditPage />
              </RequirePermission>
            }
          />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
    </div>
  );
}
