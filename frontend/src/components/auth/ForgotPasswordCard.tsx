"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { ArrowRight, BookOpen, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { AuthVisualPanel } from "@/components/auth/AuthVisualPanel";
import { ApiError } from "@/lib/api/client";
import { requestPasswordReset } from "@/lib/api/auth";

export function ForgotPasswordCard() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);

    try {
      await requestPasswordReset({ email: email.trim() });
      // The backend always returns the same response whether or not the
      // email belongs to an account (see AuthService.request_password_
      // reset) - the UI must not distinguish either, so this always shows
      // the same confirmation regardless of the outcome.
      setSent(true);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message || "Something went wrong. Please try again." : "Something went wrong. Please try again.");
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
          <div className="login-form-heading"><h2>Reset your password</h2><p>Enter your email and we’ll send you a reset link.</p></div>

          {sent ? (
            <>
              <p className="login-success" style={{ marginTop: 34 }}>
                If an account exists for {email.trim() || "that email"}, a password reset link is on its way.
                Check your inbox.
              </p>
              <p className="login-alternate"><Link href="/login">Back to sign in</Link></p>
            </>
          ) : (
            <>
              <form className="login-form" onSubmit={handleSubmit}>
                <div>
                  <label htmlFor="email">Email</label>
                  <input
                    id="email"
                    name="email"
                    type="email"
                    autoComplete="email"
                    className="login-input"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    disabled={busy}
                    required
                  />
                </div>
                {error ? <p className="login-error">{error}</p> : null}
                <Button type="submit" className="login-submit" disabled={busy}>
                  {busy ? <><Loader2 size={15} className="animate-spin" /> Sending</> : <>Send reset link <ArrowRight size={15} /></>}
                </Button>
              </form>
              <p className="login-alternate"><Link href="/login">Back to sign in</Link></p>
            </>
          )}
        </div>
      </section>
    </main>
  );
}
