import { request } from "./client";
import type {
  AuthResponse,
  ForgotPasswordRequest,
  LoginRequest,
  RegisterRequest,
  ResetPasswordRequest,
} from "@/lib/types/auth";

export function register(payload: RegisterRequest): Promise<AuthResponse> {
  return request<AuthResponse>("/auth/register", {
    method: "POST",
    body: payload,
  });
}

export function login(payload: LoginRequest): Promise<AuthResponse> {
  return request<AuthResponse>("/auth/login", {
    method: "POST",
    body: payload,
  });
}

export function logout(): Promise<{ status: string }> {
  return request<{ status: string }>("/auth/logout", {
    method: "POST",
  });
}

export function refresh(): Promise<AuthResponse> {
  return request<AuthResponse>("/auth/refresh", {
    method: "POST",
  });
}

export function getCurrentUser(): Promise<AuthResponse> {
  return request<AuthResponse>("/auth/me");
}

export function requestPasswordReset(payload: ForgotPasswordRequest): Promise<{ status: string }> {
  return request<{ status: string }>("/auth/forgot-password", {
    method: "POST",
    body: payload,
  });
}

export function confirmPasswordReset(payload: ResetPasswordRequest): Promise<{ status: string }> {
  return request<{ status: string }>("/auth/reset-password", {
    method: "POST",
    body: payload,
  });
}
