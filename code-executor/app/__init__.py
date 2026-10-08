"""Sandboxed multi-language code execution service.

A standalone service on purpose: it holds no application secrets, shares no
database or storage with the main backend, and sits on an internal-only
network. See docker-compose.yml and app/firewall.py for the isolation model.
"""
