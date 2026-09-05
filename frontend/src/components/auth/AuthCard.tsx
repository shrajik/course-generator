"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { ArrowRight, Loader2 } from "lucide-react";
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

export function AuthCard({ mode }: { mode: Mode }) {
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
