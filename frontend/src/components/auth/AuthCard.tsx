"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { ArrowRight, BookOpen, FileText, Image as ImageIcon, Lightbulb, Loader2, Users } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/auth-provider";

type Mode = "login" | "register";

function errorMessage(caught: unknown, mode: Mode): string {
  if (!(caught instanceof ApiError)) return "Something went wrong. Please try again.";
  if (caught.status === 409) return "An account already exists for that email.";
  if (caught.status === 401) return "Invalid email or password.";
  if (caught.status === 422) {
    return mode === "register"
      ? "Use a valid email and a password with at least 8 characters."
      : "Enter your email and password.";
  }
  if (caught.status === 0) return caught.message;
  return caught.message || "Something went wrong. Please try again.";
}

export function AuthCard({ mode, immersive = false }: { mode: Mode; immersive?: boolean }) {
  const router = useRouter();
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRegister = mode === "register";
  const title = isRegister ? "Create Account" : "Sign In";
  const subtitle = isRegister
    ? "Start creating AI-authored courses."
    : "Continue to your course workspace.";
  const alternateHref = isRegister ? "/login" : "/register";
  const alternateText = isRegister ? "Already have an account?" : "Need an account?";
  const alternateAction = isRegister ? "Sign in" : "Create one";

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);

    try {
      const payload = { email: email.trim(), password };
      if (isRegister) {
        await auth.register(payload);
      } else {
        await auth.login(payload);
      }
      router.replace("/");
    } catch (caught) {
      setError(errorMessage(caught, mode));
    } finally {
      setBusy(false);
    }
  };

  if (immersive) {
    return (
      <main className="login-experience">
        <section className="login-visual" aria-label="AI Course Creator overview">
          <div className="login-visual-content">
            <div className="login-brand"><span className="login-brand-mark"><BookOpen size={19} /></span><span>AI Course Creator</span></div>
            <div className="login-visual-copy">
              <p className="login-kicker">Learn · Create · Grow</p>
              <h1>Turn your ideas<br />into complete<br /><em>courses with AI.</em></h1>
              <p className="login-lede">Research, structure, write and design<br />beautiful courses — in minutes, not months.</p>
              <div className="login-features">
                <div><span><Lightbulb size={17} /></span><p><strong>AI-Powered Content</strong><small>From idea to full course</small></p></div>
                <div><span><FileText size={17} /></span><p><strong>Structured &amp; Comprehensive</strong><small>Well-organized chapters</small></p></div>
                <div><span><ImageIcon size={17} /></span><p><strong>Rich Media Support</strong><small>Images, examples and more</small></p></div>
                <div><span><Users size={17} /></span><p><strong>For Educators, Creators &amp; Teams</strong><small>Share knowledge, make an impact</small></p></div>
              </div>
            </div>
          </div>
          <div className="login-visual-quote">“Knowledge grows<br />when it’s shared.”</div>
        </section>

        <section className="login-form-side">
          <div className="login-form-card">
            <div className="login-form-brand"><span className="login-form-brand-mark"><BookOpen size={18} /></span><strong>AI Course Creator</strong><span>LEARN ANYTHING. TEACH ANYONE.</span></div>
            <div className="login-form-heading"><h2>Sign In</h2><p>{subtitle}</p></div>
            <form className="login-form" onSubmit={handleSubmit}>
              <div><label htmlFor="email">Email</label><input id="email" name="email" type="email" autoComplete="email" className="login-input" value={email} onChange={(event) => setEmail(event.target.value)} disabled={busy} required /></div>
              <div><label htmlFor="password">Password</label><input id="password" name="password" type="password" autoComplete="current-password" className="login-input" value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} minLength={1} required /></div>
              {error ? <p className="login-error">{error}</p> : null}
              <Button type="submit" className="login-submit" disabled={busy}>{busy ? <><Loader2 size={15} className="animate-spin" /> Signing in</> : <>Sign In <ArrowRight size={15} /></>}</Button>
            </form>
            <p className="login-alternate">Need an account? <Link href={alternateHref}>{alternateAction}</Link></p>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-[480px] items-center px-4 py-8">
      <section className="panel w-full p-6">
        <div>
          <p className="text-[12px] font-semibold uppercase tracking-[0.08em] text-brand-600">
            AI Course Creator
          </p>
          <h1 className="mt-2 text-2xl font-semibold text-ink">{title}</h1>
          <p className="mt-1 text-sm text-ink-500">{subtitle}</p>
        </div>

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <div>
            <label htmlFor="email" className="mb-1.5 block text-[12.5px] font-medium text-ink-700">
              Email
            </label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="email"
              className="field"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={busy}
              required
            />
          </div>

          <div>
            <label
              htmlFor="password"
              className="mb-1.5 block text-[12.5px] font-medium text-ink-700"
            >
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete={isRegister ? "new-password" : "current-password"}
              className="field"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={busy}
              minLength={isRegister ? 8 : 1}
              required
            />
          </div>

          {error ? (
            <p className="rounded-[10px] border border-red-200 bg-red-50 px-3 py-2 text-[12px] text-red-700">
              {error}
            </p>
          ) : null}

          <Button type="submit" className="w-full" disabled={busy}>
            {busy ? (
              <>
                <Loader2 size={14} className="animate-spin" />
                {isRegister ? "Creating account" : "Signing in"}
              </>
            ) : (
              <>
                {title}
                <ArrowRight size={14} />
              </>
            )}
          </Button>
        </form>

        <p className="mt-5 text-center text-[13px] text-ink-500">
          {alternateText}{" "}
          <Link href={alternateHref} className="font-medium text-brand-700 hover:text-brand-800">
            {alternateAction}
          </Link>
        </p>
      </section>
    </main>
  );
}
