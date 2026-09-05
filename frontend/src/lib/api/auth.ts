import { request } from "./client";
import type { AuthResponse, LoginRequest, RegisterRequest } from "@/lib/types/auth";

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
