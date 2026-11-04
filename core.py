"""Core logic for the Humanizer local web app: configuration, the skill prompt,
API helpers, and the final‑text filter used during streaming."""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CONFIG_PATH = ROOT / "config.json"
SKILL_PATH = ROOT / "humanizer" / "SKILL.md"

APP_NAME = "Humanizer"
APP_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Default configuration (merged with whatever is on disk)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "base_url": "",
    "api_key": "",
    "model_name": "",
    "temperature": 0.7,
    "max_tokens": 4000,
    "writing_sample": "",
    "disable_reasoning": False,
    "edit_intensity": "thorough",
    "verify_ssl": True,
    "multi_pass": True,
}


# ---------------------------------------------------------------------------
# Configuration IO
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """Return a config dict merged with defaults.  Creates a default file if
    one does not exist yet."""
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    else:
        raw = {}

    # simple merge – user values override defaults
    cfg = {**DEFAULT_CONFIG, **raw}
    # coerce types that might have come from JSON strings
    if cfg["temperature"] is not None:
        try:
            cfg["temperature"] = float(cfg["temperature"])
        except (TypeError, ValueError):
            cfg["temperature"] = DEFAULT_CONFIG["temperature"]
    if cfg["max_tokens"] is not None:
        try:
            cfg["max_tokens"] = int(cfg["max_tokens"])
        except (TypeError, ValueError):
            cfg["max_tokens"] = DEFAULT_CONFIG["max_tokens"]
    if cfg.get("edit_intensity") not in ("light", "thorough"):
        cfg["edit_intensity"] = "thorough"
    vs = cfg.get("verify_ssl")
    if isinstance(vs, str):
        cfg["verify_ssl"] = vs.strip().lower() in ("1", "true", "yes", "on")
    elif not isinstance(vs, bool):
        cfg["verify_ssl"] = True
    mp = cfg.get("multi_pass")
    if isinstance(mp, str):
        cfg["multi_pass"] = mp.strip().lower() in ("1", "true", "yes", "on")
    elif not isinstance(mp, bool):
        cfg["multi_pass"] = True
    return cfg


def save_config(cfg: dict) -> None:
    """Write the config dict to disk atomically (temp file + rename)."""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def validate_config(raw: dict) -> tuple[dict, list[str]]:
    """Validate a raw config dict and return (cleaned_cfg, errors).

    * `base_url` – required, must start with http:// or https://
    * `model_name` – required, non‑empty
    * `api_key` – optional (stored verbatim)
    * `temperature` – optional float 0‑2, otherwise null
    * `max_tokens` – optional int >= 1, otherwise null
    * `writing_sample` – optional string (kept as‑is)
    * `disable_reasoning` – optional bool; ask supported endpoints to skip
      the model's thinking/reasoning phase
    * `edit_intensity` – "thorough" (default) or "light"; how deeply the
      rewrite may change the sentences
    * `verify_ssl` – optional bool, default True. Turn off on networks
      whose proxy or VPN breaks certificate verification.
    """
    errors: list[str] = []
    base = str(raw.get("base_url") or "").strip().rstrip("/")
    if not base:
        errors.append("Base URL is required.")
    elif not base.startswith(("http://", "https://")):
        errors.append("Base URL must start with http:// or https://")

    model = str(raw.get("model_name") or "").strip()
    if not model:
        errors.append("Model name is required.")

    # temperature
    temp = raw.get("temperature")
    if temp is not None and temp != "":
        try:
            t = float(temp)
            if not (0 <= t <= 2):
                errors.append("Temperature must be between 0 and 2.")
            else:
                t = t  # keep the float
        except (TypeError, ValueError):
            errors.append("Temperature must be a number.")
            t = None
    else:
        t = None

    # max_tokens
    mt = raw.get("max_tokens")
    if mt is not None and mt != "":
        try:
            m = int(mt)
            if m < 1:
                errors.append("Max tokens must be at least 1.")
            else:
                m = m
        except (TypeError, ValueError):
            errors.append("Max tokens must be a whole number.")
            m = None
    else:
        m = None

    sample = str(raw.get("writing_sample") or "")

    # disable_reasoning – accept a JSON bool or a "true"/"false" string
    dr_raw = raw.get("disable_reasoning", False)
    if isinstance(dr_raw, str):
        disable_reasoning = dr_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        disable_reasoning = bool(dr_raw)

    # verify_ssl – same rule; default True (verify certificates)
    vs_raw = raw.get("verify_ssl", True)
    if isinstance(vs_raw, str):
        verify_ssl = vs_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        verify_ssl = bool(vs_raw)

    # multi_pass – same rule; default True (scan -> rewrite -> check -> fix)
    mp_raw = raw.get("multi_pass", True)
    if isinstance(mp_raw, str):
        multi_pass = mp_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        multi_pass = bool(mp_raw)

    # edit_intensity – only two values are valid; anything else means thorough
    intensity = ("light" if str(raw.get("edit_intensity") or "").strip().lower()
                 == "light" else "thorough")

    if errors:
        return None, errors

    clean = {
        "base_url": base,
        "api_key": str(raw.get("api_key") or ""),
        "model_name": model,
        "temperature": t,
        "max_tokens": m,
        "writing_sample": sample,
        "disable_reasoning": disable_reasoning,
        "edit_intensity": intensity,
        "verify_ssl": verify_ssl,
        "multi_pass": multi_pass,
    }
    return clean, []


# ---------------------------------------------------------------------------
# Humanizer skill prompt
# ---------------------------------------------------------------------------
def strip_frontmatter(text: str) -> str:
    """Given the full SKILL.md file, return just the body after the YAML front‑matter."""
    # front‑matter starts with ---, ends at the next --- on its own line
    m = re.match(r"---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        return text[m.end():]
    return text


_skill_cache: dict = {"mtime": None, "body": ""}


def load_skill_body() -> str:
    """Read the Humanizer skill prompt (the markdown body, no front‑matter),
    cached by mtime so we only re‑read when the file changes."""
    st = SKILL_PATH.stat()
    if _skill_cache["mtime"] != st.st_mtime:
        raw = SKILL_PATH.read_text(encoding="utf-8")
        body = strip_frontmatter(raw)
        _skill_cache.update(mtime=st.st_mtime, body=body)
    return _skill_cache["body"]


# Common part of the app directive: how the result must be returned.
_OUTPUT_RULES = """\
## Output mode (app)

This app uses *embedded mode*, which overrides the "What to return" section of the skill.

Run the process from "How to work" on the text below, then return **only** the final rewritten text, with nothing else:

- No preamble, greeting, or explanation.
- No "Final version", "Final rewrite", or similar heading.
- No list of remaining patterns.
- No code fence around the text.
- Keep the language of the input.
""".strip()

# How deep the rewrite must go. Exactly one of these blocks is included.
_DEPTH_THOROUGH = """\
### Rewrite depth: thorough

Rewrite thoroughly, not lightly:

- Work at paragraph scale, not only word by word. Do not return the input with a few words changed.
- Rework every sentence that carries a tell from the list, even a mild one.
- Turn labeled lists into prose when the labels add nothing (§19), instead of editing the labels in place.
- Vary sentence length and structure across the whole text.
- Dashes: the final text must contain no em dashes (—) or en dashes (–) unless the writer's sample uses them (§8).
- Before finishing, re-read the rewrite and fix any tell that survived. If the result is nearly identical to the input, rewrite it again.
""".strip()

_DEPTH_LIGHT = """\
### Rewrite depth: light

Edit with a light hand:

- Fix the tells you are confident about; leave borderline sentences alone.
- Always fix these, even in light mode: bold labels on list items (§19), title-case or decorated headings (§20), curly quotes (§21), and a closing triad or "future looks bright" send-off (§6, §13).
- Keep the writer's sentence rhythm, word choices, and paragraph shape. Do not restructure passages that carry no clear tell.
- Keep emphasis that carries meaning; remove only decorative bold.
""".strip()

# Rules that hold at every depth: keep the facts and the formatting.
_PRESERVE_RULES = """\
### Preserve structure and facts exactly

- Keep every fact, name, number, date, quote, citation, and link. Add nothing.
- Keep every link exactly as written: `[text](url)` stays a link, with the same visible text and the same URL. Never unwrap a link into plain text. Keep every image line `![alt](url)` too.
- Keep the markdown intact: headings, lists, blockquotes, and paragraph breaks stay in place.
- Keep bold and italics that mark emphasis, as in **this matters** or *de facto*. Remove bold only where it decorates every item of a list or adds no emphasis (§19).
- Go paragraph by paragraph: every paragraph must come out with at least one sentence reworded, unless it contains no tell at all.
""".strip()


def build_directive(intensity: str = "thorough") -> str:
    """App directive for the requested edit intensity ('light' or 'thorough')."""
    depth = _DEPTH_LIGHT if intensity == "light" else _DEPTH_THOROUGH
    return "\n\n".join((_OUTPUT_RULES, depth, _PRESERVE_RULES))


def build_system_prompt(intensity: str = "thorough") -> str:
    """Skill body + app directive, ready to be sent as the system message."""
    skill_body = load_skill_body()
    return f"{skill_body.strip()}\n\n---\n\n{build_directive(intensity)}"


def build_messages(original_text: str, writing_sample: str = "",
                   intensity: str = "thorough") -> list[dict]:
    """Return the message list for the chat‑completions call.

    * system – the skill prompt + directive.
    * optional system – writing sample for voice matching.
    * user – the original text to humanize.
    """
    msgs: list[dict] = [{"role": "system", "content": build_system_prompt(intensity)}]

    if writing_sample and writing_sample.strip():
        msgs.append(
            {"role": "system", "content": "Here is a sample of my writing for voice matching:\n\n"
                                             + writing_sample.strip()}
        )

    msgs.append({"role": "user", "content": original_text})
    return msgs


# ---------------------------------------------------------------------------
# Multi-pass pipeline (scan -> rewrite -> check -> fix)
# ---------------------------------------------------------------------------
SCAN_DIRECTIVE = """\
## Phase 1: scan (app)

You are scanning the text below for AI-writing tells from the skill. Do NOT rewrite anything yet.

Return **only** a numbered list of the tells you find, strongest first. For each item give one line: the §pattern number, the exact phrase or sentence, and why it is a tell.

- Maximum 20 items; merge repeats of the same pattern.
- Also name paragraph-scale tells: repeated closers, forced triads, staged openings.
- If the text is completely clean, return exactly: CLEAN
- No preamble, no closing remark.
""".strip()

CHECK_DIRECTIVE = """\
## Phase: check (app)

You are checking a rewrite against its original text. Compare them and return **only** a numbered list of problems, one line each:

- AI-writing tells from the skill that survive in the rewrite (give the §number and the phrase).
- Facts, names, numbers, dates, citations or meaning that the rewrite lost or changed.
- Markdown that was dropped or broken: links unwrapped, images removed, headings, lists or bold emphasis deleted (report each missing image or link by its URL).
- Paragraphs copied word-for-word from the original (report each as "paragraph N of the rewrite is a word-for-word copy").

Maximum 15 items. If the rewrite is clean and complete, return exactly: CLEAN
No preamble, no closing remark.
""".strip()


def build_scan_messages(text: str) -> list[dict]:
    """Phase 1: find and list the tells; no rewriting."""
    skill_body = load_skill_body()
    system = f"{skill_body.strip()}\n\n---\n\n{SCAN_DIRECTIVE}"
    return [{"role": "system", "content": system},
            {"role": "user", "content": text}]


def build_rewrite_messages(original_text: str, scan_list: str,
                           intensity: str = "thorough",
                           writing_sample: str = "") -> list[dict]:
    """Phase 2: rewrite, with the scan findings placed as an assistant turn
    so the model treats them as its own analysis."""
    msgs: list[dict] = [{"role": "system", "content": build_system_prompt(intensity)}]

    if writing_sample and writing_sample.strip():
        msgs.append(
            {"role": "system", "content": "Here is a sample of my writing for voice matching:\n\n"
                                             + writing_sample.strip()}
        )

    msgs.append({"role": "user", "content": original_text})
    if scan_list:
        msgs.append({"role": "assistant",
                     "content": "Before rewriting, I found these tells in the text:\n\n"
                                + scan_list})
        msgs.append({"role": "user",
                     "content": "Now rewrite the text so it fixes every tell in that list, "
                                "following all the rules above. Return only the final "
                                "rewritten text, nothing else."})
    return msgs


def build_check_messages(original_text: str, current_rewrite: str) -> list[dict]:
    """Phase 3: compare the rewrite against the original; list problems."""
    skill_body = load_skill_body()
    system = f"{skill_body.strip()}\n\n---\n\n{CHECK_DIRECTIVE}"
    return [{"role": "system", "content": system},
            {"role": "user",
             "content": "Original text:\n\n" + original_text
                        + "\n\n---\n\nCurrent rewrite:\n\n" + current_rewrite
                        + "\n\nList the remaining problems, or return CLEAN."}]


def build_fix_messages(original_text: str, current_rewrite: str, issues: str,
                       intensity: str = "thorough",
                       writing_sample: str = "") -> list[dict]:
    """Phase 4: apply exactly the check findings; keep everything else."""
    msgs: list[dict] = [{"role": "system", "content": build_system_prompt(intensity)}]

    if writing_sample and writing_sample.strip():
        msgs.append(
            {"role": "system", "content": "Here is a sample of my writing for voice matching:\n\n"
                                             + writing_sample.strip()}
        )

    msgs.append({"role": "user", "content": original_text})
    msgs.append({"role": "assistant",
                 "content": "I rewrote the text, but a check found these remaining "
                            "problems:\n\n" + issues})
    msgs.append({"role": "user",
                 "content": "Return the corrected final rewrite now, fixing exactly those "
                            "problems and nothing else. Keep everything that was already "
                            "correct, including all markdown, links and facts. Return only "
                            "the final rewritten text, nothing else."})
    return msgs


def clean_analysis(text: str) -> str:
    """Strip code fences and blank padding from a scan/check list."""
    t = (text or "").strip()
    t = re.sub(r"^```[A-Za-z0-9_-]*\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def is_clean_verdict(issues: str) -> bool:
    """True when the check pass found nothing to fix."""
    return bool(re.match(r"(?i)^\W*clean\b", issues.strip()))


# ---------------------------------------------------------------------------
# Deterministic coverage check (the Thorough floor)
# ---------------------------------------------------------------------------
def _paras(text: str) -> list[str]:
    """Split into paragraphs (blank-line separated), keeping content only."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # a "paragraph" that is just image lines is markdown, not prose
    return [p for p in paras
            if not all(IMG_LINE_RE.match(ln) for ln in p.splitlines())]


def _norm(text: str) -> str:
    """Collapse whitespace so formatting edits do not affect comparison."""
    return re.sub(r"\s+", " ", text).strip().lower()


def find_untouched_paragraphs(original: str, current: str, *,
                              ratio_threshold: float = 0.85,
                              limit: int = 8) -> list[str]:
    """Detect rewrite paragraphs that are near-verbatim copies of the original.

    This is the Thorough floor, enforced in code so it works on small models:
    the returned list feeds the fix pass as concrete targets. Returns a
    list of instruction strings, most-similar first, at most `limit`.
    """
    oparas = [p for p in _paras(original)]
    cparas = [p for p in _paras(current)]
    if not oparas or not cparas:
        return []

    onorm = [_norm(p) for p in oparas]
    found: list[tuple[float, int, int, str]] = []
    for ci, cpara in enumerate(cparas):
        cn = _norm(cpara)
        if len(cn) < 60:            # tiny paragraphs (headings, links) skip
            continue
        if cn in onorm:             # exact copy of a paragraph
            oi = onorm.index(cn)
            found.append((1.0, oi, ci, cpara))
            continue
        best_r, best_i = 0.0, -1
        for oi, on in enumerate(onorm):
            if abs(len(on) - len(cn)) > 0.5 * max(len(on), len(cn)):
                continue            # length filter: skip obviously different sizes
            sm = difflib.SequenceMatcher(a=on, b=cn)
            if sm.quick_ratio() < ratio_threshold:
                continue
            r = sm.ratio()
            if r > best_r:
                best_r, best_i = r, oi
        if best_r >= ratio_threshold:
            found.append((best_r, best_i, ci, cpara))
    found.sort(reverse=True)        # most identical first
    out = []
    for r, oi, ci, cpara in found[:limit]:
        excerpt = cpara[:100] + ("…" if len(cpara) > 100 else "")
        out.append(f"Paragraph {ci + 1} of the rewrite is a near-verbatim copy "
                   f"of paragraph {oi + 1} of the original ({r:.0%} identical): "
                   f"rework its wording, keeping the meaning. Text: {excerpt!r}")
    return out


# ---------------------------------------------------------------------------
# Markdown safety net
# ---------------------------------------------------------------------------
IMG_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\(\s*[^)\s]+\s*\)\s*$")
IMG_URL_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")


def restore_lost_images(original: str, final: str) -> str | None:
    """Reinsert standalone image lines the rewrite dropped.

    Models rewriting long markdown sometimes shed standalone image lines
    even when told to keep them. This is deterministic: any image URL
    from the original that is missing from the final text is inserted as a
    standalone line, proportionally close to its original position.

    Returns the corrected text, or None when nothing was missing."""
    orig_lines = original.splitlines()
    if not orig_lines:
        return None
    final_urls = set(IMG_URL_RE.findall(final))
    missing = []                                   # (index in original, line)
    for idx, line in enumerate(orig_lines):
        if not IMG_LINE_RE.match(line):
            continue
        urls = IMG_URL_RE.findall(line)
        if not urls:
            continue
        if urls[0] not in final_urls:
            missing.append((idx, line.strip()))
    if not missing:
        return None

    fin_lines = final.splitlines()
    n_orig, n_fin = max(len(orig_lines), 1), max(len(fin_lines), 1)
    # insert from the bottom up so earlier indexes stay valid
    for idx, line in reversed(missing):
        pos = min(n_fin, max(0, round(idx / n_orig * n_fin)))
        while pos < len(fin_lines) and not fin_lines[pos].strip():
            pos += 1                                 # prefer a non-blank anchor
        fin_lines.insert(pos, "")
        fin_lines.insert(pos + 1, line)
        fin_lines.insert(pos + 2, "")
        n_fin = len(fin_lines)
    return "\n".join(fin_lines)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def chat_url(base: str) -> str:
    """Return the full /chat/completions URL for a given base."""
    b = base.strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def models_url(base: str) -> str:
    """Return the full /models URL for a given base (used for the test‑connection check)."""
    b = base.strip().rstrip("/")
    if b.endswith("/chat/completions"):
        b = b[: -len("/chat/completions")]
    return b + "/models"


# ---------------------------------------------------------------------------
# Delta / content extraction
# ---------------------------------------------------------------------------
def extract_content(obj: object) -> str:
    """Pull the .content delta from a chat‑completions response object.

    Works for both the ``delta`` field and the final ``message`` field.
    """
    if not isinstance(obj, dict):
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    ch = choices[0]  # type: ignore
    # 1) delta.content (streaming chunks)
    delta = ch.get("delta") or {}
    c = delta.get("content")
    if c:
        return c
    # 2) final message.content
    msg = ch.get("message") or {}
    return msg.get("content") or ""


def api_error_message(code: int, body: str) -> str:
    """Build a short human‑readable error message from an HTTP status and body."""
    msg = ""
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or str(err)[:300]
            elif err:
                msg = str(err)[:300]
    except Exception:
        pass
    if not msg:
        msg = (body or "").strip()[:400] or "(empty response body)"
    return f"API error {code}: {msg}"


# ---------------------------------------------------------------------------
# FinalTextFilter – streaming clean‑up
# ---------------------------------------------------------------------------
class FinalTextFilter:
    """Small streaming filter that strips the model’s preamble/label and
    any surrounding code‑fences so the UI only receives the clean rewrite.

    It works by waiting until we have enough text to decide what to keep,
    then emits clean text in 16‑byte chunks (harmless lag)."""
    def __init__(self) -> None:
        self._started = False
        self._pending: str = ""
        self._had_fence = False

    def feed(self, chunk: str) -> str:
        """Give the filter a chunk of model output.  Returns clean text
        once we’ve passed the initial ‘label / fence’ phase."""
        self._pending += chunk
        if not self._started and ("\n" in self._pending or len(self._pending) > 200):
            self._start()
        out: str = ""
        if self._started and len(self._pending) > 16:
            out = self._pending[:-16]
            self._pending = self._pending[-16:]
        return out

    def finish(self) -> str:
        """Called when the stream ends.  Returns the remaining text with
        any leading label / fence stripped."""
        if not self._started:
            self._start()
        out = self._pending
        self._pending = ""
        out = out.rstrip()
        # strip a leading “Final version:” / “Final rewrite:” label etc.
        out = re.sub(
            r"^(?i:(?:here(?:'s| is)|sure|certainly|of course)[^:\\n]{0,80}:|"
            r"final (?:version|rewrite|text)\s*:)\s*\n?",
            "", out)
        # strip a leading code fence like ```text or ```markdown
        m = re.match(r"^```[A-Za-z0-9_-]*\s*\n", out)
        if m:
            self._had_fence = True
            out = out[m.end():]
        # strip a trailing fence ``` 
        if self._had_fence and out.endswith("```"):
            out = out[:-3].rstrip()
        return out

    def _start(self) -> None:
        self._started = True
        # strip leading whitespace / blank lines
        self._pending = self._pending.lstrip()
        # drop a leading label such as "Here is the final version:" or "Final version:"
        self._pending = re.sub(
            r"^(?i:(?:here(?:'s| is)|sure|certainly|of course)[^:\\n]{0,80}:|"
            r"final (?:version|rewrite|text)\s*:)\s*\n?",
            "", self._pending)


# ---------------------------------------------------------------------------
# Small HTTP helper used by the test‑connection endpoint
# ---------------------------------------------------------------------------
class UpstreamError(Exception):
    """Wrapper so the test‑connection handler can distinguish a 400‑max_tokens
    retry from other failures."""
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body