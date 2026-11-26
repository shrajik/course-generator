"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, useState } from "react";
import { ArrowRight, BookOpen, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { AuthVisualPanel } from "@/components/auth/AuthVisualPanel";
import { ApiError } from "@/lib/api/client";
import { confirmPasswordReset } from "@/lib/api/auth";

function errorMessage(caught: unknown): string {
  if (!(caught instanceof ApiError)) return "Something went wrong. Please try again.";
  if (caught.status === 401) return "This reset link is invalid or has expired. Request a new one.";
  if (caught.status === 422) return "Use a password with at least 8 characters.";
  if (caught.status === 0) return caught.message;
  return caught.message || "Something went wrong. Please try again.";
}

export function ResetPasswordCard() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    if (password !== confirmPassword) {
      setError("Passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);

    try {
      await confirmPasswordReset({ token, password });
      setDone(true);
      setTimeout(() => router.replace("/login"), 2500);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-experience">
      <AuthVisualPanel />

      <section className="login-form-side">
        <div className="login-form-card">
          <div className="login-form-brand"><span className="login-form-brand-mark"><BookOpen size={18} /></span><strong>AI Course Creator</strong><span>LEARN ANYTHING. TEACH ANYONE.</span></div>
          <div className="login-form-heading"><h2>Choose a new password</h2><p>Make it at least 8 characters.</p></div>

          {!token ? (
            <p className="login-error" style={{ marginTop: 34 }}>
              This link is missing its reset code. Request a new one from the{" "}
              <Link href="/forgot-password">forgot password</Link> page.
            </p>
          ) : done ? (
            <p className="login-success" style={{ marginTop: 34 }}>
              Your password has been reset. Redirecting you to sign in…
            </p>
          ) : (
            <form className="login-form" onSubmit={handleSubmit}>
              <div>
                <label htmlFor="password">New password</label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  autoComplete="new-password"
                  className="login-input"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={busy}
                  minLength={8}
                  required
                />
              </div>
              <div>
                <label htmlFor="confirmPassword">Confirm new password</label>
                <input
                  id="confirmPassword"
                  name="confirmPassword"
                  type="password"
                  autoComplete="new-password"
                  className="login-input"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  disabled={busy}
                  minLength={8}
                  required
                />
              </div>
              {error ? <p className="login-error">{error}</p> : null}
              <Button type="submit" className="login-submit" disabled={busy}>
                {busy ? <><Loader2 size={15} className="animate-spin" /> Resetting</> : <>Reset password <ArrowRight size={15} /></>}
              </Button>
            </form>
          )}
          <p className="login-alternate"><Link href="/login">Back to sign in</Link></p>
        </div>
      </section>
    </main>
  );
}
