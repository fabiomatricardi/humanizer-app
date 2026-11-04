"""Humanizer local web app – backend API (port 8774) + frontend UI (port 8775)
No third‑party dependencies. Run with:  uv run python app.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request

# ---- project helpers -------------------------------------------------------
import core
from core import (load_config, save_config, validate_config,
                  build_messages, extract_content, FinalTextFilter,
                  api_error_message, chat_url, models_url,
                  APP_NAME, APP_VERSION)

# ---- constants -------------------------------------------------------------
DEFAULT_BACKEND_PORT = 8774
DEFAULT_FRONTEND_PORT = 8775
DEFAULT_HOST = "127.0.0.1"

# Some providers/WAFs reject Python's default "Python-urllib/…" agent.
UPSTREAM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def https_opener(verify_ssl: bool = True) -> urllib.request.OpenerDirector:
    """HTTPS opener honoring the verify_ssl toggle.

    Some VPNs and corporate proxies intercept HTTPS with a private CA whose
    Basic Constraints extension is not marked critical. Python 3.13+
    (OpenSSL 3.5) verifies RFC 5280 strictly by default and rejects such
    chains, so calls fail with CERTIFICATE_VERIFY_FAILED on those networks
    even though they worked minutes before on another network. Turning
    the toggle off skips certificate checks; keeping it on keeps full
    chain and hostname verification."""
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

# ---- HTTP handler base -----------------------------------------------------
class BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HumanizerApp/0.1"
    timeout = 60

    def log_message(self, fmt, *args):
        # minimise noise; optional: write to stderr
        pass

    def handle(self) -> None:
        # A keep-alive client can drop the connection between requests
        # (Stop button, tab close, browser pruning a spare connection).
        # On Windows that raises ConnectionAbortedError (WinError 10053)
        # while we wait for the next request line. That is normal client
        # behaviour, not a server error, so close quietly instead of
        # printing a traceback.
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- read JSON body (safety limit 5 MB) --------------------------------
    def read_json_body(self, limit: int = 5_000_000) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            raise ValueError("bad body")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # ---- send JSON response --------------------------------------------------
    def send_json(self, status: int, obj: object) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---- API handler (serves /api/* on the backend port) ----------------------
class ApiHandler(BaseHandler):
    # ---- GET routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 – required name by BaseHTTP
        path = self.path.split("?")[0]  # strip query string
        if path == "/api/health":
            self.send_json(200, {"ok": True, "app": "Humanizer", "version": "0.1.0"})
            return
        if path == "/api/config":
            cfg = load_config()
            cfg_configured = bool(cfg["base_url"] and cfg["model_name"])
            self.send_json(200, {"ok": True, "config": cfg, "configured": cfg_configured})
            return
        if path == "/api/test":
            self._handle_test()
            return
        # anything else under /api -> 404
        self.send_json(404, {"ok": False, "error": "not found"})
        return

    # ---- POST routing ------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            raw = self.read_json_body()
        except ValueError:
            self.send_json(400, {"ok": False, "error": "Invalid JSON body."})
            return

        if path == "/api/config":
            clean, errors = validate_config(raw)
            if errors:
                self.send_json(400, {"ok": False, "errors": errors})
                return
            save_config(clean)
            self.send_json(200, {"ok": True, "config": clean})
            return

        if path == "/api/test":
            self._handle_test()
            return

        if path == "/api/humanize":
            self._handle_humanize(raw)
            return

        self.send_json(404, {"ok": False, "error": "not found"})

    # ---- /api/test ---------------------------------------------------------
    def _handle_test(self) -> None:
        cfg = load_config()
        if not cfg.get("base_url"):
            self.send_json(400, {"ok": False, "error": "Base URL is not set."})
            return

        # 1) try /models (with auth headers – previously they were computed
        #    but never sent, which made providers reject the request)
        models_error = None
        opener = https_opener(cfg.get("verify_ssl", True))
        try:
            url = models_url(cfg["base_url"])
            req = urllib.request.Request(url, headers={**self._auth_headers(cfg),
                                                       "User-Agent": UPSTREAM_UA})
            with opener.open(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
                self.send_json(200, {"ok": True, "message": "Models endpoint reachable.",
                                     "preview": body[:200]})
                return
        except urllib.error.HTTPError as e:
            models_error = api_error_message(e.code, e.read().decode("utf-8", "replace"))
            e.close()
        except Exception as e:
            models_error = str(e)

        # 2) fallback: 1‑token chat completion
        url = chat_url(cfg["base_url"])
        try:
            payload = {"model": cfg["model_name"], "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1, "stream": False}
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={**self._auth_headers(cfg), "Content-Type": "application/json",
                         "User-Agent": UPSTREAM_UA},
                method="POST")
            with opener.open(req, timeout=30) as resp:
                self.send_json(200, {"ok": True, "message": "Chat completions reachable.",
                                     "detail": f"(/models said: {models_error})" if models_error else None})
                return
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            e.close()
            self.send_json(502, {"ok": False, "error": api_error_message(e.code, body),
                                 "url": url,
                                 "models_error": models_error,
                                 "proxies": urllib.request.getproxies()})
        except Exception as e2:
            self.send_json(502, {"ok": False, "error": str(e2),
                                 "url": url,
                                 "models_error": models_error,
                                 "proxies": urllib.request.getproxies()})

    # ---- /api/humanize -----------------------------------------------------
    def _handle_humanize(self, body: dict) -> None:
        # NOTE: the JSON body has already been parsed by do_POST() and is
        # passed in here – reading the request stream twice would hang.
        text = str(body.get("text") or "").strip()
        if not text:
            self.send_json(400, {"ok": False, "error": "No text provided."})
            return

        cfg = load_config()
        if not (cfg.get("base_url") and cfg.get("model_name")):
            self.send_json(400, {"ok": False, "error": "Not configured. Open the Configuration tab."})
            return

        # ---- build payload -------------------------------------------------
        # intensity comes from the request body ("light" or "thorough");
        # anything unexpected means thorough
        intensity = ("light" if str(body.get("intensity") or "").strip().lower()
                     == "light" else "thorough")
        verify = cfg.get("verify_ssl", True)
        sample = cfg.get("writing_sample") or ""
        # multi-pass chain runs for Thorough only, when not disabled in config
        multi_pass = (intensity == "thorough" and cfg.get("multi_pass", True))

        # ---- stream response to client -------------------------------------
        # The 200 goes out before the scan phase so the client can show the
        # scan animation while phase 1 runs. Errors that happen after this
        # point travel as \x01E...\x01 markers (see handleMarker in app.js).
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        filt = FinalTextFilter()
        client_gone = False
        sent = [False]  # use list for mutability in nested fn
        full_parts: list[str] = []  # server-side copy of everything streamed
        reasoning_chars = 0
        last_activity = 0.0
        wlock = threading.Lock()        # serializes every write to wfile
        first_content = threading.Event()   # set on the first content byte
        hb_stop = threading.Event()         # stops the heartbeat thread

        def send_phase(name: str) -> None:
            """Phase marker for multi-pass runs: \x01P<name>\x01. The frontend
            uses it to switch the pane animation and status line; unknown
            markers are ignored by older clients."""
            send_marker(f"P{name}")

        def send_marker(text: str) -> None:
            nonlocal client_gone
            m = f"\x01{text}\x01".encode("utf-8")
            with wlock:
                if client_gone:
                    return
                try:
                    self.wfile.write(f"{len(m):X}\r\n".encode("ascii") + m + b"\r\n")
                    self.wfile.flush()
                except OSError:
                    client_gone = True

        def send_activity() -> None:
            """Liveness marker: tells the client the upstream is still working
            (e.g. a reasoning model is 'thinking') without leaking any text
            into the output. Format: \\x01A\\x01 – stripped by the frontend."""
            nonlocal client_gone
            m = b"\x01A\x01"
            with wlock:
                if client_gone:
                    return
                try:
                    self.wfile.write(f"{len(m):X}\r\n".encode("ascii") + m + b"\r\n")
                    self.wfile.flush()
                except OSError:
                    client_gone = True

        def write_text(chunk: str) -> None:
            nonlocal client_gone
            if not chunk:
                return
            data = chunk.encode("utf-8")
            with wlock:
                if client_gone:
                    return
                try:
                    self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    client_gone = True
                else:
                    first_content.set()
            sent[0] = True
            full_parts.append(chunk)

        def heartbeat() -> None:
            """Writes a liveness marker every ~5s while the rewrite is silent.

            Providers often send no response headers until the first token is
            ready, so between 'rewrite started' and 'first token' there is no
            traffic at all. Without these beats the browser shows nothing
            for minutes. The thread stops at the first content byte, on
            client disconnect, or when the request ends."""
            while not hb_stop.wait(5.0):
                if first_content.is_set() or client_gone:
                    return
                with wlock:
                    if client_gone:
                        return
                    try:
                        m = b"\x01A\x01"
                        self.wfile.write(f"{len(m):X}\r\n".encode("ascii") + m + b"\r\n")
                        self.wfile.flush()
                    except OSError:
                        return

        threading.Thread(target=heartbeat, daemon=True).start()

        # ---- multi-pass, phase 1: scan -------------------------------------
        # Gives the model scratch space: it externalizes the tells it finds,
        # then the rewrite pass works from that list instead of juggling the
        # whole pattern catalogue and the text at once.
        scan_list = ""
        if multi_pass:
            send_phase("scan")
            send_marker("MStep 1 of 3 – scanning for AI tells…")
            t0 = time.monotonic()
            try:
                scan_raw = self._complete_once(core.build_scan_messages(text),
                                               cfg, max_tokens=700, verify_ssl=verify)
                scan_list = core.clean_analysis(scan_raw)
                if core.is_clean_verdict(scan_list):
                    scan_list = ""
                n_tells = len(scan_list.splitlines()) if scan_list else 0
                sys.stderr.write(f"multipass: scan {time.monotonic() - t0:.1f}s, "
                                 f"{n_tells} tells\n")
            except Exception as e:
                multi_pass = False
                sys.stderr.write(f"multipass: scan failed ({e}); "
                                 f"falling back to single-pass rewrite\n")
            if multi_pass:
                send_phase("rewrite")
                detail = (f" from {n_tells} found tells" if n_tells
                          else " – scan found no tells, full rewrite")
                send_marker(f"MStep 2 of 3 – rewriting{detail}…")

        if multi_pass:
            msgs = core.build_rewrite_messages(text, scan_list, intensity, sample)
        else:
            msgs = core.build_messages(text, sample, intensity)
            send_marker("MRewriting (single pass)…")
        payload_base = {"model": cfg["model_name"], "messages": msgs, "stream": True}
        if cfg["temperature"] is not None:
            payload_base["temperature"] = cfg["temperature"]
        if cfg["max_tokens"] is not None:
            payload_base["max_tokens"] = cfg["max_tokens"]

        # Reasoning-disable extras: OpenAI/DeepSeek-style endpoints understand
        # reasoning_effort, NVIDIA/vLLM-style endpoints understand
        # chat_template_kwargs.enable_thinking. Send both when the toggle is
        # on; if the endpoint rejects them (400), fall back to a plain payload.
        extras_variants: list[dict] = []
        if cfg.get("disable_reasoning"):
            extras_variants.append({
                "reasoning_effort": "none",
                "chat_template_kwargs": {"enable_thinking": False},
            })
        extras_variants.append({})  # plain payload – always the last resort

        url = chat_url(cfg["base_url"])
        auth = self._auth_headers(cfg)

        # ---- stream upstream, with automatic fallbacks ---------------------
        upstream_resp = None
        last_error: Exception | None = None
        extras_i = 0
        swapped_max = False
        attempts = 0
        max_attempts = 6
        while attempts < max_attempts:
            attempts += 1
            payload = dict(payload_base)
            payload.update(extras_variants[extras_i])
            if swapped_max and "max_tokens" in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            try:
                upstream_resp = self._stream_upstream(url, payload, auth, timeout=600,
                                                      verify_ssl=verify)
                break
            except Exception as e:
                last_error = e
                if isinstance(e, self._UpstreamError) and e.status == 400:
                    # 1) endpoint rejected the reasoning params -> drop them
                    if extras_i < len(extras_variants) - 1:
                        extras_i += 1
                        sys.stderr.write(
                            "note: endpoint rejected the reasoning-disable "
                            "parameters; retrying without them\n")
                        continue
                    # 2) provider wants max_completion_tokens instead
                    if not swapped_max and "max_tokens" in payload_base:
                        swapped_max = True
                        continue
                break

        if upstream_resp is None:
            # NEVER let an upstream error escape this handler: an uncaught
            # exception drops the socket and the browser shows "Failed to fetch".
            # The 200 is already out, so the error travels as an \x01E...\x01
            # marker; the frontend shows it as a toast.
            msg = str(last_error) if last_error else "Upstream request failed after retries."
            sys.stderr.write(f"humanize upstream error: {msg}\n")
            hb_stop.set()
            if not client_gone:
                send_marker("E" + msg[:300])
                with wlock:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                    except Exception:
                        pass
            return

        # ---- stream the rewrite pass (pass 2) ------------------------------
        try:
            ctype = (upstream_resp.headers.get("Content-Type") or "").lower()
            if "event-stream" in ctype or ctype.startswith("text/event-stream"):
                for raw in upstream_resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    ds = line[5:].strip()
                    if ds == "[DONE]":
                        break
                    # upstream is alive -> heartbeat while nothing is shown yet
                    now = time.monotonic()
                    if not sent[0] and now - last_activity >= 0.3:
                        last_activity = now
                        send_activity()
                    # unwrap the SSE JSON envelope -> actual text delta
                    try:
                        obj = json.loads(ds)
                    except (ValueError, TypeError):
                        obj = None
                    if isinstance(obj, dict):
                        ch0 = (obj.get("choices") or [{}])[0] or {}
                        rc = (ch0.get("delta") or {}).get("reasoning_content")
                        if rc:
                            reasoning_chars += len(rc)
                        piece = extract_content(obj)
                    else:
                        piece = ""
                    if not piece:
                        continue
                    out = filt.feed(piece)
                    write_text(out)
            else:
                # non‑streaming fallback – read whole body, extract, filter, send once
                body = upstream_resp.read().decode("utf-8", "replace")
                try:
                    obj = json.loads(body)
                    text_out = extract_content(obj)
                except Exception:
                    text_out = body
                out = filt.feed(text_out)
                write_text(out)
        except (BrokenPipeError, ConnectionResetError, OSError):
            client_gone = True
        except Exception as e:
            # Upstream died mid-stream: finish the chunked response cleanly
            # instead of dropping the connection ("Failed to fetch" in the
            # browser). Partial text already sent stays valid.
            sys.stderr.write(f"humanize stream error: {e}\n")
        finally:
            upstream_resp.close()
            # send terminating chunk later – the multi-pass check/fix stages
            # may append more chunks to the same response body first
            if not client_gone:
                tail = filt.finish()
                if tail or not sent[0]:
                    write_text(tail)
            # after the final flush: report a run that produced no text
            if not sent[0]:
                sys.stderr.write(
                    f"humanize: no content received "
                    f"(reasoning chars: {reasoning_chars})\n")

        # ---- multi-pass, phases 3+: check and fix --------------------------
        # The rewrite text now lives in current. Each check pass lists
        # surviving tells / lost facts; each fix pass applies exactly that
        # list. The loop is bounded so a stubborn model cannot loop forever.
        current = "".join(full_parts).strip()
        if multi_pass and not client_gone and current:
            sample = cfg.get("writing_sample") or ""
            max_tokens_full = cfg["max_tokens"] or 8000
            rounds = 0
            while rounds < 2 and not client_gone:
                rounds += 1
                send_phase("check")
                send_marker("MStep 3 of 3 – checking the rewrite…")
                t0 = time.monotonic()
                try:
                    raw = self._complete_once(core.build_check_messages(text, current),
                                              cfg, max_tokens=700, verify_ssl=verify)
                except Exception as e:
                    sys.stderr.write(f"multipass: check failed ({e})\n")
                    break
                issues = core.clean_analysis(raw)
                # deterministic Thorough floor: code also flags near-verbatim
                # paragraphs, so the floor works on small models that check loosely
                untouched = core.find_untouched_paragraphs(text, current)
                issues_full = issues
                if untouched:
                    issues_full = (issues + "\n" if issues else "") + "\n".join(untouched)
                sys.stderr.write(f"multipass: check {time.monotonic() - t0:.1f}s -> "
                                 f"{issues[:120]!r} + {len(untouched)} untouched paragraphs\n")
                if not issues_full:
                    break

                send_phase("fix")
                n_issues = (len(issues.splitlines()) if issues else 0) + len(untouched)
                send_marker(f"MFixing {n_issues} issues (round {rounds})…")
                t0 = time.monotonic()
                try:
                    fixed = self._complete_once(
                        core.build_fix_messages(text, current, issues_full,
                                                intensity, sample),
                        cfg, max_tokens=max_tokens_full, verify_ssl=verify)
                except Exception as e:
                    sys.stderr.write(f"multipass: fix failed ({e})\n")
                    break
                fixed = core.clean_analysis(fixed)
                f2 = FinalTextFilter()          # reuse the stream clean-up:
                fixed = f2.feed(fixed) + f2.finish()   # strip preamble/fences
                if (not fixed.strip() or fixed.strip() == current
                        or len(fixed) < len(current) * 0.4):
                    sys.stderr.write("multipass: fix produced no usable change; "
                                     "keeping the previous rewrite\n")
                    break
                sys.stderr.write(f"multipass: fix {time.monotonic() - t0:.1f}s, "
                                 f"{len(fixed)} chars\n")
                current = fixed
                # replace the client output with the corrected rewrite
                with wlock:
                    try:
                        m = b"\x01C\x01"
                        self.wfile.write(f"{len(m):X}\r\n".encode("ascii") + m + b"\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        client_gone = True
                if client_gone:
                    break
                write_text(fixed)
            if not client_gone:
                send_phase("done")

        # ---- markdown safety net: restore images the model dropped ---------
        if not client_gone and current:
            restored = core.restore_lost_images(text, current)
            if restored:
                n_img = len(core.IMG_URL_RE.findall(restored))
                sys.stderr.write(f"multipass: restored lost images "
                                 f"({n_img} image lines in output)\n")
                current = restored
                with wlock:
                    try:
                        m = b"\x01C\x01"
                        self.wfile.write(f"{len(m):X}\r\n".encode("ascii") + m + b"\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        client_gone = True
                    else:
                        first_content.set()
                write_text(current)

        # ---- terminate the chunked response --------------------------------
        hb_stop.set()
        if not client_gone:
            with wlock:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    pass

    # ---- small helpers -----------------------------------------------------
    def _auth_headers(self, cfg: dict) -> dict:
        if cfg.get("api_key"):
            return {"Authorization": f"Bearer {cfg['api_key']}"}
        return {}

    # ---------- streaming upstream request ---------------------------------
    class _UpstreamError(Exception):
        def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
            super().__init__(message)
            self.status = status
            self.body = body

    def _stream_upstream(self, url: str, payload: dict, auth: dict, *,
                         timeout: int = 600, verify_ssl: bool = True):
        """POST to the chat completions endpoint with stream=True.
        Returns the http.client.HTTPResponse, or raises _UpstreamError."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h = {"Content-Type": "application/json", "Accept": "text/event-stream",
             "User-Agent": UPSTREAM_UA}
        h.update(auth)
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        try:
            return https_opener(verify_ssl).open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            e.close()
            raise self._UpstreamError(
                api_error_message(e.code, body), status=e.code, body=body) from None
        except urllib.error.URLError as e:
            msg = f"Cannot reach the API at {url}: {e.reason}"
            if "CERTIFICATE_VERIFY_FAILED" in str(e.reason) and verify_ssl:
                msg += (" – if you are on a VPN or corporate proxy, turn off "
                        "'Verify SSL certificates' in the Configuration tab.")
            raise self._UpstreamError(msg) from None

    def _complete_once(self, msgs: list[dict], cfg: dict, *,
                        max_tokens: int, verify_ssl: bool) -> str:
        """Non-streaming completion used by the scan/check/fix passes.
        Returns the plain content, or raises _UpstreamError. Reasoning
        disable extras are sent once and dropped if the endpoint rejects
        them with a 400."""
        payload = {"model": cfg["model_name"], "messages": msgs, "stream": False,
                   "max_tokens": (min(cfg["max_tokens"], max_tokens)
                                  if cfg["max_tokens"] else max_tokens)}
        if cfg["temperature"] is not None:
            payload["temperature"] = cfg["temperature"]
        url = chat_url(cfg["base_url"])
        variants = []
        if cfg.get("disable_reasoning"):
            variants.append({"reasoning_effort": "none",
                             "chat_template_kwargs": {"enable_thinking": False}})
        variants.append({})
        last_error: Exception | None = None
        for extras in variants:
            payload.update(extras)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": UPSTREAM_UA, **self._auth_headers(cfg)})
            try:
                with https_opener(verify_ssl).open(req, timeout=600) as resp:
                    obj = json.loads(resp.read().decode("utf-8", "replace"))
                return core.extract_content(obj)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                e.close()
                last_error = self._UpstreamError(
                    core.api_error_message(e.code, body), status=e.code, body=body)
                if e.code == 400 and extras:
                    continue    # drop the reasoning extras and retry once
            except urllib.error.URLError as e:
                msg = f"Cannot reach the API at {url}: {e.reason}"
                if "CERTIFICATE_VERIFY_FAILED" in str(e.reason) and verify_ssl:
                    msg += (" – turn off 'Verify SSL certificates' in the "
                            "Configuration tab if a VPN or proxy causes this.")
                raise self._UpstreamError(msg) from None
        raise last_error  # type: ignore[misc]


# ---- Frontend handler (serves static + /env.js on the frontend port) ------
class FrontendHandler(BaseHandler):
    # ---- GET routing -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", ""):
            self._serve_static("index.html")
            return
        if path == "/env.js":
            self._serve_env_js()
            return
        # try to serve any other file under static/ (prevent directory traversal)
        target = (Path("static") / path.lstrip("/")).resolve()
        if str(target).startswith(str(Path("static").resolve())) and target.is_file():
            self._serve_static_file(target)
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    # ---- POST routing ------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        # The UI calls /api/* on the backend port directly (CORS), so we don't
        # need to handle POST here. Return 404.
        self.send_json(404, {"ok": False, "error": "not found"})

    # ---- static file serving -----------------------------------------------
    def _serve_static(self, filename: str) -> None:
        target = (Path("static") / filename).resolve()
        if not str(target).startswith(str(Path("static").resolve())):
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        if not target.is_file():
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        data = target.read_bytes()
        ext = target.suffix.lower()
        mime_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        mime = mime_types.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static_file(self, target: Path) -> None:
        # same as _serve_static but called when path already resolved
        data = target.read_bytes()
        ext = target.suffix.lower()
        mime_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        mime = mime_types.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_env_js(self) -> None:
        # Read ports from the running servers; we have them as globals set in main().
        # For now use the defaults; if the user changed them, the env.js will
        # reflect the actual ports because main() overwrites the constants via
        # a module-level dict (see main() below).
        # Here we just use the module-level variables HOST, BACKEND_PORT, FRONTEND_PORT.
        host = getattr(FrontendHandler, "host", DEFAULT_HOST)
        bport = getattr(FrontendHandler, "backend_port", DEFAULT_BACKEND_PORT)
        fport = getattr(FrontendHandler, "frontend_port", DEFAULT_FRONTEND_PORT)
        js = f"window.APP_CONFIG = {{ apiBase: 'http://{host}:{bport}' }};\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(js.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(js.encode("utf-8"))


# ---- environment helpers ---------------------------------------------------
def _env_int(name: str, fallback: int) -> int:
    """Read an int from the environment; invalid/empty values use the fallback."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(f"Warning: ignoring invalid {name}={raw!r} (using {fallback}).\n")
        return fallback


def _env_str(name: str, fallback: str) -> str:
    raw = (os.environ.get(name) or "").strip()
    return raw or fallback


# ---- main ------------------------------------------------------------------
def main() -> None:
    # Environment variables provide the defaults; explicit CLI flags win.
    parser = argparse.ArgumentParser(description="Humanizer local web app")
    parser.add_argument("--backend-port", type=int,
                        default=_env_int("HUMANIZER_BACKEND_PORT", DEFAULT_BACKEND_PORT),
                        help=f"Backend API port (default {DEFAULT_BACKEND_PORT}, env HUMANIZER_BACKEND_PORT)")
    parser.add_argument("--frontend-port", type=int,
                        default=_env_int("HUMANIZER_FRONTEND_PORT", DEFAULT_FRONTEND_PORT),
                        help=f"Frontend UI port (default {DEFAULT_FRONTEND_PORT}, env HUMANIZER_FRONTEND_PORT)")
    parser.add_argument("--host",
                        default=_env_str("HUMANIZER_HOST", DEFAULT_HOST),
                        help=f"Bind host (default {DEFAULT_HOST}, env HUMANIZER_HOST)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open the browser automatically")
    args = parser.parse_args()

    backend_port = args.backend_port
    frontend_port = args.frontend_port
    host = args.host

    # ---- prepare handler classes with port references -----------------------
    FrontendHandler.host = host
    FrontendHandler.backend_port = backend_port
    FrontendHandler.frontend_port = frontend_port

    # ---- two HTTP servers ---------------------------------------------------
    backend_server = ThreadingHTTPServer((host, backend_port), ApiHandler)
    frontend_server = ThreadingHTTPServer((host, frontend_port), FrontendHandler)

    backend_thread = threading.Thread(target=backend_server.serve_forever, daemon=True)
    frontend_thread = threading.Thread(target=frontend_server.serve_forever, daemon=True)
    backend_thread.start()
    frontend_thread.start()

    # ---- open browser -------------------------------------------------------
    frontend_url = f"http://{host}:{frontend_port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(frontend_url)).start()

    # ---- print URLs ---------------------------------------------------------
    sys.stderr.write(f"\n{APP_NAME} {APP_VERSION}\n")
    sys.stderr.write(f"  Backend API : http://{host}:{backend_port}\n")
    sys.stderr.write(f"  Frontend UI : {frontend_url}\n")
    sys.stderr.write(f"  (Ctrl+C to stop)\n\n")

    try:
        # Keep the main thread alive while the daemon threads serve requests.
        while backend_thread.is_alive() or frontend_thread.is_alive():
            backend_thread.join(timeout=1)
            frontend_thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        backend_server.shutdown()
        frontend_server.shutdown()
        backend_server.server_close()
        frontend_server.server_close()
        sys.stderr.write("\nStopped.\n")


# For direct execution: `python app.py`
if __name__ == "__main__":
    main()