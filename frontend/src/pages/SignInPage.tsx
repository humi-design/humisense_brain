/**
 * Sign-in.
 *
 * Two modes, matching the backend:
 * - `dev` (development only): a subject plus organization slug. The server still requires
 *   an existing membership, so this cannot be used to grant access.
 * - `oidc` (production): the operator is redirected to the provider and the returned
 *   id_token is exchanged for a session. The token is passed straight through and is
 *   never stored.
 */

import { useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";
import { Activity, KeyRound } from "lucide-react";
import { useSession } from "../hooks/useSession";
import { ApiError } from "../api/client";
import { Alert, Button, Field, inputClasses } from "../components/ui";

type Mode = "dev" | "oidc";

export function SignInPage() {
  const { signIn } = useSession();
  const location = useLocation();
  const [mode, setMode] = useState<Mode>("dev");
  const [subject, setSubject] = useState("");
  const [organizationSlug, setOrganizationSlug] = useState("");
  const [email, setEmail] = useState("");
  const [idToken, setIdToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const from = (location.state as { from?: string } | null)?.from ?? "/";

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "dev") {
        await signIn({
          subject: subject.trim(),
          organization_slug: organizationSlug.trim(),
          email: email.trim() ? email.trim() : null,
        });
      } else {
        await signIn({ id_token: idToken.trim() });
      }
      window.location.assign(from);
    } catch (cause) {
      const apiError =
        cause instanceof ApiError ? cause : new ApiError(0, "unknown", "Sign-in failed.", null, null);
      setError(apiError.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center gap-3">
          <span className="rounded-xl bg-accent/15 p-3 text-accent" aria-hidden="true">
            <Activity size={22} />
          </span>
          <div>
            <h1 className="text-lg font-semibold text-ink">Humisense Brain / OS</h1>
            <p className="text-sm text-ink-muted">Layer 1 · Connector &amp; sensory platform</p>
          </div>
        </div>

        <form
          onSubmit={(event) => {
            void onSubmit(event);
          }}
          className="space-y-4 rounded-xl border border-border-subtle bg-surface p-6"
        >
          <div className="flex gap-1 rounded-lg bg-canvas p-1" role="tablist" aria-label="Sign-in method">
            {(["dev", "oidc"] as Mode[]).map((option) => (
              <button
                key={option}
                type="button"
                role="tab"
                aria-selected={mode === option}
                onClick={() => setMode(option)}
                className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  mode === option ? "bg-accent/15 text-accent" : "text-ink-muted hover:text-ink"
                }`}
              >
                {option === "dev" ? "Development" : "OpenID Connect"}
              </button>
            ))}
          </div>

          {error ? <Alert tone="error">{error}</Alert> : null}

          {mode === "dev" ? (
            <>
              <Alert tone="info">
                Development sign-in is refused when the backend runs with{" "}
                <code className="font-mono">AUTH_MODE=oidc</code>. It requires an existing
                membership and never grants one.
              </Alert>
              <Field label="Subject" htmlFor="subject" required hint="Your identity in the dev issuer.">
                <input
                  id="subject"
                  className={inputClasses}
                  value={subject}
                  onChange={(event) => setSubject(event.target.value)}
                  autoComplete="off"
                  required
                />
              </Field>
              <Field
                label="Organization slug"
                htmlFor="organization-slug"
                required
                hint="The tenant you want to act in."
              >
                <input
                  id="organization-slug"
                  className={inputClasses}
                  value={organizationSlug}
                  onChange={(event) => setOrganizationSlug(event.target.value)}
                  autoComplete="off"
                  required
                />
              </Field>
              <Field label="Email" htmlFor="email" hint="Optional.">
                <input
                  id="email"
                  type="email"
                  className={inputClasses}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  autoComplete="off"
                />
              </Field>
            </>
          ) : (
            <Field
              label="Identity token"
              htmlFor="id-token"
              required
              hint="The id_token returned by your provider. It is exchanged immediately and not stored."
            >
              <textarea
                id="id-token"
                className={`${inputClasses} h-24 font-mono text-xs`}
                value={idToken}
                onChange={(event) => setIdToken(event.target.value)}
                required
              />
            </Field>
          )}

          <Button type="submit" variant="primary" loading={busy} icon={KeyRound} className="w-full">
            Sign in
          </Button>
        </form>

        <p className="mt-4 text-center text-xs text-ink-faint">
          The session is an HttpOnly cookie. No access token is kept in browser storage.
        </p>
      </div>
    </div>
  );
}
