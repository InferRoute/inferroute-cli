"""Local, deterministic secret-redaction ("the scrubber").

Runs entirely on the user's machine. It NEVER touches the network and NEVER
transmits anything itself — it only rewrites text. It sanitizes logs / session
payloads BEFORE another component (e.g. the local recorder) syncs them to the
cloud, so the cloud never sees a real secret.

What makes this more than a dumb ``***`` masker:

  * Stable tokenization — each distinct secret VALUE maps to a STABLE
    placeholder that encodes its kind and a short HMAC hash, e.g.
    ``<SECRET:stripe:a1>`` / ``<SECRET:env:OPENAI_API_KEY:c2>``. The same value
    always yields the same placeholder, within a run AND across runs (via a
    persistent local salt), so a downstream LLM can still reason about
    correlation ("these 12 log lines share one key") without seeing the value.

  * Reversible locally, never remotely — a local-only reverse map
    ``{placeholder -> original}`` is kept on disk with 0600 perms under the
    user's config dir. It is NEVER sent anywhere. :func:`rehydrate` swaps
    placeholders back to the real values, for re-inflating cloud results before
    they are shown to the user.

  * Structure-preserving — only the secret VALUE is replaced; the key name, log
    shape, and punctuation around it are kept byte-for-byte.

Honesty (see README): this is best-effort secret-HIDING, not anonymization and
not a privacy guarantee. It can miss secrets embedded in free text or in novel
formats. The optional PII layer is OFF by default — this hides secrets, it does
not anonymize people.

No third-party dependencies, no ML. A clean :class:`Detector` seam is left so a
small local NER model can be plugged in later for fuzzy PII (v2).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

__all__ = [
    "Scrubber",
    "ScrubResult",
    "ScrubberError",
    "Detector",
    "Match",
    "scrub",
    "rehydrate",
    "default_config_dir",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ScrubberError(RuntimeError):
    """Raised when the scrubber cannot run safely.

    The cardinal case (fail-safe): the reverse map cannot be written securely.
    We refuse to scrub rather than produce un-recoverable output.
    """


# --------------------------------------------------------------------------- #
# Matches & detectors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Match:
    """A detected secret span.

    Only ``[start, end)`` of the original text is replaced — that span is the
    secret VALUE itself, never the surrounding key/punctuation.
    """

    start: int
    end: int
    kind: str            # e.g. "aws", "stripe", "jwt", "env", "uri", ...
    value: str           # the exact secret substring (used for hashing + map)
    priority: int        # higher wins when spans overlap
    label: Optional[str] = None   # extra placeholder segment, e.g. env KEY name


# A Detector is any callable that yields Matches for a chunk of text.
Detector = Callable[[str], Iterable[Match]]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def default_config_dir() -> Path:
    """Where salt + reverse map + config live.

    Priority: ``INFERROUTE_SCRUBBER_DIR`` env, then ``$XDG_CONFIG_HOME`` or
    ``~/.config`` under ``inferroute/scrubber``.
    """
    env = os.environ.get("INFERROUTE_SCRUBBER_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "inferroute" / "scrubber"


# The placeholder grammar. Kept deliberately simple and self-delimiting so it is
# trivial to detect (for idempotency) and to rehydrate.
#   <SECRET:kind:hash>
#   <SECRET:env:KEY_NAME:hash>
_PLACEHOLDER_RE = re.compile(r"<SECRET:[A-Za-z0-9_]+(?::[A-Za-z0-9_]+)*:[0-9a-f]{2,}>")

_ENV_KEY_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|AUTH)", re.IGNORECASE
)

# Auth-scheme keywords that may appear as the first token of an Authorization
# header value — never the secret themselves.
_AUTH_SCHEMES = frozenset({"bearer", "basic", "token", "digest", "negotiate"})

# Service keywords harvested from the gitleaks rule set (MIT). Used by the
# contextual detector to catch short / numeric provider secrets that have no
# distinctive prefix and slip past the generic-entropy net — exactly how
# gitleaks catches them: a known service name sitting next to the value.
_SERVICE_KEYWORDS = (
    "adafruit", "adobe", "airtable", "algolia", "alibaba", "artifactory", "asana",
    "atatt3", "atlassian", "atlasv1", "bintray", "bitbucket", "bittrex", "cloudflare",
    "cmvmd", "codecov", "cohere", "coinbase", "confluence", "confluent", "contentful",
    "datadog", "discord", "dnkey", "droneci", "dropbox", "eyjrijoi", "facebook", "fastly",
    "finicity", "finnhub", "flickr", "freshbooks", "gocardless", "gr1348941", "heroku",
    "hubspot", "incomingwebhook", "intercom", "jfrog", "kraken", "kucoin", "launchdarkly",
    "linkedin", "mailchimp", "mailgun", "mapbox", "mattermost", "meraki", "messagebird",
    "netlify", "newrelic", "newyorktimes", "nytimes", "plaid", "privateai", "rapidapi",
    "sendbird", "sentry", "sonar", "sourcegraph", "squarespace", "t3blbkfj", "telegr",
    "travis", "twitch", "twitter", "webhookb2", "yandex", "zendesk",
)
# <service keyword><up to 20 key chars> <sep> <value>. Captures only the value.
# Mirrors the gitleaks connective: the keyword may be a suffix of a longer key
# (MAILCHIMP_API_KEY) and the separator set is broad (=, :, =>, :=, ||, ?=, ,).
# Gated by _SERVICE_HINT_RE + an 8-char token-shaped value to bound false hits.
_SERVICE_CTX_RE = re.compile(
    r"(?i)(?:" + "|".join(_SERVICE_KEYWORDS) + r")"
    r"[ \t\w.\-]{0,20}?[\s'\"]{0,3}(?:=|>|:{1,3}=?|\|\||=>|\?=|,)[\s'\"\x60]{0,5}"
    r"(?P<val>[A-Za-z0-9][\w.\-/+=]{7,79})"
)


@dataclass
class ScrubberConfig:
    """User-tunable knobs, persisted as ``config.json`` in the config dir."""

    pii: bool = False                  # OFF by default — secret-hiding, not anonymization
    contextual: bool = True            # mask service-keyword-adjacent values (gitleaks-style)
    mask_userinfo: bool = False        # also mask the user in user:pass@host URIs
    hash_len: int = 6                  # hex chars of HMAC in the placeholder (collision-extended)
    entropy_min_len: int = 20          # generic high-entropy: min token length
    entropy_min_bits: float = 3.5      # generic high-entropy: min Shannon bits/char
    entropy_min_classes: int = 2       # require >= N of {lower,upper,digit,symbol}
    entropy_word_skip_ceiling: float = 4.2  # below this, skip natural-language identifiers
    allowlist: list[str] = field(default_factory=list)  # never redact (literal, or /regex/)
    denylist: list[str] = field(default_factory=list)   # always redact (regex)

    @classmethod
    def load(cls, path: Path) -> "ScrubberConfig":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text("utf-8"))
        except Exception:
            return cls()
        cfg = cls()
        for k, v in raw.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg


# --------------------------------------------------------------------------- #
# Entropy
# --------------------------------------------------------------------------- #
def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _char_classes(s: str) -> int:
    return sum(
        bool(re.search(p, s))
        for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )


# Word-segment splitter: breaks on non-letters (digits, -, _, .) AND camelCase.
_WORD_SEG_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
_VOWELS = frozenset("aeiouAEIOU")


def looks_like_words(tok: str) -> bool:
    """True if ``tok`` reads as a natural-language identifier — a filename,
    slug, or camelCase name like ``2026-06-05-class-bugfix-notes`` or
    ``UserProfileSettingsPanel`` — rather than a random secret.

    The generic high-entropy heuristic over-redacts these because a hyphen
    inflates them past the character-class gate and their entropy (~3.5–4.0)
    overlaps real hex secrets. Structure is the discriminator: slugs decompose
    into ≥2 vowel-containing word segments that cover most of the token's
    letters; random secrets do not. Used ONLY in the ambiguous entropy band, so
    it can never suppress a high-entropy random secret.
    """
    words = [w for w in _WORD_SEG_RE.findall(tok)
             if len(w) >= 3 and any(c in _VOWELS for c in w)]
    if len(words) < 2:
        return False
    total_letters = sum(c.isalpha() for c in tok)
    return total_letters > 0 and sum(len(w) for w in words) / total_letters >= 0.75


# --------------------------------------------------------------------------- #
# The scrubber
# --------------------------------------------------------------------------- #
@dataclass
class ScrubResult:
    """Return value of :meth:`Scrubber.scrub`.

    ``redaction_report`` carries COUNTS and CATEGORIES only — never values.
    """

    scrubbed_text: str
    redaction_report: dict
    reverse_map_ref: str   # path to the on-disk reverse map (never its contents)


class Scrubber:
    """Stateful, deterministic secret scrubber.

    A single instance owns one config dir (salt + reverse map + config). It is
    safe to reuse across many :meth:`scrub` calls; the reverse map accumulates
    so tokenization stays stable across calls and runs.
    """

    def __init__(
        self,
        config_dir: Optional[Path | str] = None,
        config: Optional[ScrubberConfig] = None,
        *,
        require_recoverable: bool = True,
    ) -> None:
        self.config_dir = Path(config_dir).expanduser() if config_dir else default_config_dir()
        self.salt_path = self.config_dir / "salt"
        self.map_path = self.config_dir / "reverse_map.json"
        self.config_path = self.config_dir / "config.json"

        self._ensure_secure_dir()
        self.config = config or ScrubberConfig.load(self.config_path)
        self._salt = self._load_or_create_salt()

        # placeholder -> original (the reverse map, persisted)
        self._map: dict[str, str] = {}
        # original value -> placeholder (in-memory inverse, for value-stable reuse)
        self._inverse: dict[str, str] = {}
        self._load_map()

        if require_recoverable:
            # Fail-safe: prove we can persist recoverability BEFORE scrubbing.
            self._assert_writable()

        self._detectors: list[Detector] = self._build_detectors()
        self._compiled_denylist = [re.compile(p) for p in self._safe_regexes(self.config.denylist)]
        self._allow_literals, self._allow_regexes = self._split_allowlist(self.config.allowlist)

    # ----- secure storage --------------------------------------------------- #
    def _ensure_secure_dir(self) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.config_dir, 0o700)
        except OSError as e:
            raise ScrubberError(
                f"cannot create secure config dir {self.config_dir}: {e}"
            ) from e

    def _assert_writable(self) -> None:
        """Refuse to run if we cannot durably store the reverse map (0600)."""
        try:
            self._write_map_atomic()
        except OSError as e:
            raise ScrubberError(
                "refusing to scrub: reverse map is not securely writable at "
                f"{self.map_path} ({e}). Scrubbing without recoverability is unsafe."
            ) from e

    def _load_or_create_salt(self) -> bytes:
        try:
            if self.salt_path.exists():
                return bytes.fromhex(self.salt_path.read_text("utf-8").strip())
        except (OSError, ValueError):
            pass
        salt = secrets.token_bytes(32)
        try:
            self._atomic_write_bytes(self.salt_path, salt.hex().encode("ascii"), 0o600)
        except OSError as e:
            raise ScrubberError(f"cannot persist salt at {self.salt_path}: {e}") from e
        return salt

    def _load_map(self) -> None:
        if not self.map_path.exists():
            return
        try:
            data = json.loads(self.map_path.read_text("utf-8"))
            self._map = dict(data.get("map", {}))
        except Exception:
            # A corrupt map must not crash scrubbing; start fresh but keep a
            # backup so nothing recoverable is silently lost.
            try:
                self.map_path.rename(self.map_path.with_suffix(".corrupt"))
            except OSError:
                pass
            self._map = {}
        # Build the value->placeholder inverse. If a value somehow has multiple
        # placeholders, the lexicographically smallest wins (deterministic).
        self._inverse = {}
        for ph, val in self._map.items():
            cur = self._inverse.get(val)
            if cur is None or ph < cur:
                self._inverse[val] = ph

    def _write_map_atomic(self) -> None:
        payload = json.dumps({"version": 1, "map": self._map}, ensure_ascii=False)
        self._atomic_write_bytes(self.map_path, payload.encode("utf-8"), 0o600)

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def flush(self) -> None:
        """Persist the reverse map. Call after a batch of scrubs."""
        self._write_map_atomic()

    # ----- tokenization ----------------------------------------------------- #
    def _hmac_hex(self, value: str) -> str:
        return hmac.new(self._salt, value.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _sanitize_label(label: str) -> str:
        # Keep the placeholder grammar clean (segments are [A-Za-z0-9_]).
        return re.sub(r"[^A-Za-z0-9_]", "_", label)[:48] or "x"

    def _placeholder_for(self, value: str, kind: str, label: Optional[str]) -> str:
        """Return the STABLE placeholder for ``value``, creating it if new.

        Value-keyed: the SAME value always returns the SAME placeholder, even if
        a later occurrence is detected under a different kind/label — correlation
        is preserved. Collisions on the short hash are resolved by lengthening
        the hash until the placeholder uniquely identifies this value.
        """
        existing = self._inverse.get(value)
        if existing is not None:
            return existing

        segs = ["SECRET", self._sanitize_label(kind)]
        if label:
            segs.append(self._sanitize_label(label))
        full = self._hmac_hex(value)

        length = max(2, int(self.config.hash_len))
        while True:
            ph = "<" + ":".join(segs + [full[:length]]) + ">"
            owner = self._map.get(ph)
            if owner is None or owner == value:
                break
            length += 2
            if length > len(full):  # astronomically unlikely; disambiguate hard
                ph = "<" + ":".join(segs + [full + self._hmac_hex(ph)[:4]]) + ">"
                break

        self._map[ph] = value
        self._inverse[value] = ph
        return ph

    # ----- detectors -------------------------------------------------------- #
    def _build_detectors(self) -> list[Detector]:
        dets: list[Detector] = [
            self._detect_pem,
            self._detect_uri_credentials,
            self._detect_env_assignments,
            self._detect_authorization,
            self._detect_known,
            self._detect_generic_entropy,
        ]
        if self.config.contextual:
            dets.append(self._detect_service_context)
        if self.config.pii:
            dets.append(self._detect_pii)
        return dets

    # PEM private-key blocks (multi-line). Whole block is the secret. Handles
    # the "PRIVATE KEY BLOCK" form (PGP) and any type prefix (RSA/DSA/EC/OPENSSH).
    # Guarded by a cheap substring check so plain log lines skip the regex.
    _PEM_RE = re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"
        r".*?-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----",
        re.DOTALL,
    )

    def _detect_pem(self, text: str) -> Iterator[Match]:
        if "PRIVATE KEY" not in text:
            return
        for m in self._PEM_RE.finditer(text):
            yield Match(m.start(), m.end(), "pem", m.group(0), priority=100)

    # scheme://user:pass@host  -> mask password (and user if configured)
    _URI_RE = re.compile(
        r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://"
        # user may be empty (e.g. redis://:password@host)
        r"(?P<user>[^:/?#@\s]*):(?P<pw>[^@/?#\s]+)@",
    )

    def _detect_uri_credentials(self, text: str) -> Iterator[Match]:
        if "://" not in text:
            return
        for m in self._URI_RE.finditer(text):
            yield Match(m.start("pw"), m.end("pw"), "uri", m.group("pw"), priority=90)
            if self.config.mask_userinfo:
                yield Match(
                    m.start("user"), m.end("user"), "uri_user", m.group("user"), priority=90
                )

    # KEY=value / KEY: value where KEY matches the secret-name pattern.
    # A quoted value is authoritative — it ends at the matching close quote, even
    # when the line continues (e.g. an inline `PGPASSWORD='…' psql …` shell
    # prefix), so only the value is masked and the trailing command is kept.
    # An unquoted value is bounded at whitespace/`#` (shell semantics), so we
    # never swallow the rest of the line.
    _ENV_RE = re.compile(
        r"(?im)^(?P<lead>[ \t]*(?:export[ \t]+)?)"
        r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?P<mid>[ \t]*[:=][ \t]*)"
        r"(?:"
        r"(?P<q>[\"'])(?P<qval>(?:\\.|(?!(?P=q)).)*)(?P=q)"   # quoted value
        r"|"
        r"(?P<val>[^\s#]+)"                                   # unquoted: no spaces
        r")",
    )

    # Cheap substring gate (both cases) for the env pre-filter — far faster than
    # an IGNORECASE regex search on every line.
    _ENV_HINTS = (
        "SECRET", "secret", "Secret", "TOKEN", "token", "Token",
        "PASSW", "passw", "Passw", "KEY", "key", "Key",
        "PRIVATE", "private", "Private", "CREDENTIAL", "credential", "Credential",
        "AUTH", "auth", "Auth",
    )

    def _detect_env_assignments(self, text: str) -> Iterator[Match]:
        # The KEY must contain a secret-word; if the line has none, skip the
        # (more expensive) anchored env regex entirely.
        if not any(h in text for h in self._ENV_HINTS):
            return
        for m in self._ENV_RE.finditer(text):
            key = m.group("key")
            if not _ENV_KEY_RE.search(key):
                continue
            grp = "qval" if m.group("qval") is not None else "val"
            val = m.group(grp)
            if not val:
                continue
            # Don't mask a bare auth-scheme keyword as the "value" of a header
            # like "Authorization: Bearer <tok>" (the KEY matches AUTH). The real
            # token after it is handled by the authorization/bearer detector.
            if val.lower() in _AUTH_SCHEMES:
                continue
            yield Match(m.start(grp), m.end(grp), "env", val, priority=85, label=key)

    # Authorization headers / bearer tokens.
    _AUTHZ_RE = re.compile(
        r"(?i)\b(?:authorization|proxy-authorization)\b[ \t]*[:=][ \t]*"
        r"(?:(?:bearer|basic|token)[ \t]+)?(?P<tok>[A-Za-z0-9._\-+/=~]{8,})",
    )
    _BEARER_RE = re.compile(r"(?i)\bbearer[ \t]+(?P<tok>[A-Za-z0-9._\-+/=~]{8,})")

    def _detect_authorization(self, text: str) -> Iterator[Match]:
        # Priority 86 sits above the .env detector (85) so an
        # "Authorization: Bearer <tok>" header keeps the "Bearer" scheme word
        # and masks only the token, instead of being swallowed whole by the
        # env rule (which fires because "Authorization" contains "AUTH").
        # Guard on the trigger keywords (any case) to skip the regex on the vast
        # majority of log lines.
        if not (
            "earer" in text or "EARER" in text
            or "uthorization" in text or "UTHORIZATION" in text
        ):
            return
        for m in self._AUTHZ_RE.finditer(text):
            yield Match(m.start("tok"), m.end("tok"), "bearer", m.group("tok"), priority=86)
        for m in self._BEARER_RE.finditer(text):
            yield Match(m.start("tok"), m.end("tok"), "bearer", m.group("tok"), priority=86)

    # Known-format credentials by prefix/shape, plus JWT. Each is compiled
    # SEPARATELY (not as one alternation): a single big alternation defeats
    # re's per-pattern literal-prefix fast-scan and is ~8x slower on large logs.
    # Order matters within a position only for overlaps — anthropic (sk-ant-)
    # precedes openai (sk-), of which it is a prefix superset.
    _KNOWN_SPECS: tuple[tuple[str, str], ...] = (
        ("anthropic", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
        ("openai", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
        ("aws", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|ASCA)[0-9A-Z]{16}\b"),
        ("stripe", r"\b(?:sk|pk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
        ("github", r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[0-9A-Za-z_]{20,}\b"),
        ("slack", r"\bxox[baprs]-[0-9A-Za-z-]{10,}"),
        ("gcp", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
        ("google_oauth", r"\bya29\.[0-9A-Za-z_\-]{20,}"),
        # JWT: three base64url segments; header starts eyJ ( = base64 of '{"' ).
        ("jwt", r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    )
    _KNOWN_PATTERNS = tuple((name, re.compile(pat)) for name, pat in _KNOWN_SPECS)
    # Cheap substring gate: if NONE of these literal prefixes appear, the line
    # can hold no known-format credential, so we skip all the regexes. 7-of-8
    # ordinary log lines bail here on C-level `in` checks.
    _KNOWN_HINTS = (
        "sk-", "sk_", "pk_", "rk_", "AKIA", "ASIA", "AGPA", "AIDA", "AROA",
        "ANPA", "ANVA", "ASCA", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
        "github_pat", "xox", "AIza", "ya29.", "eyJ",
    )

    def _detect_known(self, text: str) -> Iterator[Match]:
        # Priority 82 outranks generic (40) and is below bearer (86)/env (85)/
        # uri (90) so the more informative kind wins in a bare token but a
        # surrounding Authorization/.env/URI structure is still preserved.
        if not any(h in text for h in self._KNOWN_HINTS):
            return
        for kind, rx in self._KNOWN_PATTERNS:
            for m in rx.finditer(text):
                yield Match(m.start(), m.end(), kind, m.group(0), priority=82)

    # Generic high-entropy strings (catch unknown-format keys). Err toward
    # over-redaction, but gate so we do not nuke ordinary prose. The charset is
    # base64url ([A-Za-z0-9_-]) deliberately EXCLUDING '=' '/' '+': those appear
    # in KEY=value and scheme:// delimiters, and including them would let a
    # token span bridge across a delimiter and eat the key name / URL scheme,
    # destroying structure. Standard-base64 blobs with +// still get caught —
    # just split into (still long, still high-entropy) alnum runs.
    _TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{12,}")

    def _detect_generic_entropy(self, text: str) -> Iterator[Match]:
        cfg = self.config
        for m in self._TOKEN_RE.finditer(text):
            tok = m.group(0)
            if len(tok) < cfg.entropy_min_len:
                continue
            if _char_classes(tok) < cfg.entropy_min_classes:
                continue
            ent = shannon_entropy(tok)
            if ent < cfg.entropy_min_bits:
                continue
            # Precision guard: in the ambiguous entropy band, skip tokens that
            # read as natural-language identifiers (filenames, slugs, camelCase
            # names) so we don't corrupt non-secret context. Above the band,
            # everything is treated as a possible random secret.
            if ent < cfg.entropy_word_skip_ceiling and looks_like_words(tok):
                continue
            yield Match(m.start(), m.end(), "generic", tok, priority=40)

    def _detect_service_context(self, text: str) -> Iterator[Match]:
        # Cheap gate: skip the heavy contextual regex unless a known service
        # keyword appears at all (true of very few lines). A lowercase-once +
        # substring scan is ~25x faster than a 70-keyword IGNORECASE alternation
        # regex (which dominated profiles at 70% of total time).
        tl = text.lower()
        if not any(kw in tl for kw in _SERVICE_KEYWORDS):
            return
        # Priority 60: below specific creds/env/bearer/uri (so those keep their
        # informative kind and structure) but above the generic net (40).
        for m in _SERVICE_CTX_RE.finditer(text):
            yield Match(m.start("val"), m.end("val"), "service", m.group("val"), priority=60)

    # Optional PII layer (OFF by default).
    _EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
    _IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
    _IPV6_RE = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b")
    _PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().\-]{7,}\d(?!\w)")
    _CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

    @staticmethod
    def _luhn_ok(digits: str) -> bool:
        d = [int(c) for c in digits if c.isdigit()]
        if not 13 <= len(d) <= 19:
            return False
        checksum, parity = 0, len(d) % 2
        for i, n in enumerate(d):
            if i % 2 == parity:
                n *= 2
                if n > 9:
                    n -= 9
            checksum += n
        return checksum % 10 == 0

    def _detect_pii(self, text: str) -> Iterator[Match]:
        for m in self._EMAIL_RE.finditer(text):
            yield Match(m.start(), m.end(), "email", m.group(0), priority=35)
        for m in self._IPV4_RE.finditer(text):
            yield Match(m.start(), m.end(), "ipv4", m.group(0), priority=33)
        for m in self._IPV6_RE.finditer(text):
            yield Match(m.start(), m.end(), "ipv6", m.group(0), priority=33)
        for m in self._CC_RE.finditer(text):
            if self._luhn_ok(m.group(0)):
                yield Match(m.start(), m.end(), "cc", m.group(0), priority=34)
        for m in self._PHONE_RE.finditer(text):
            yield Match(m.start(), m.end(), "phone", m.group(0), priority=31)

    # denylist (custom always-redact regexes)
    def _detect_denylist(self, text: str) -> Iterator[Match]:
        for rx in self._compiled_denylist:
            for m in rx.finditer(text):
                if m.group(0):
                    yield Match(m.start(), m.end(), "custom", m.group(0), priority=95)

    # ----- allow/denylist helpers ------------------------------------------ #
    @staticmethod
    def _safe_regexes(patterns: Iterable[str]) -> list[str]:
        out = []
        for p in patterns:
            try:
                re.compile(p)
                out.append(p)
            except re.error:
                continue
        return out

    @classmethod
    def _split_allowlist(cls, items: Iterable[str]) -> tuple[set[str], list[re.Pattern]]:
        literals: set[str] = set()
        regexes: list[re.Pattern] = []
        for it in items:
            if len(it) >= 2 and it.startswith("/") and it.endswith("/"):
                try:
                    regexes.append(re.compile(it[1:-1]))
                except re.error:
                    continue
            else:
                literals.add(it)
        return literals, regexes

    def _is_allowlisted(self, value: str) -> bool:
        if value in self._allow_literals:
            return True
        return any(rx.search(value) for rx in self._allow_regexes)

    @staticmethod
    def _resolve_overlaps(
        candidates: list[Match], protected: list[tuple[int, int]]
    ) -> list[Match]:
        """Pick a non-overlapping winning set, near-linearly.

        Highest priority wins on overlap (then longer span, then earlier start);
        protected placeholder spans are inviolable. Instead of the naive
        O(k^2) "does this overlap any accepted?" scan — which explodes on data
        files that yield tens of thousands of high-entropy tokens — we sort by
        start and split into clusters of mutually-overlapping intervals. Tokens
        are delimiter-separated so they are almost all disjoint (cluster size 1,
        accepted immediately); only the handful of intervals around an actual
        secret form a small cluster resolved by priority. Overall O(k log k).
        """
        # (start, end, priority, match-or-None). Protected spans are sentinels
        # with no Match and an effectively infinite priority.
        items: list[tuple[int, int, float, Optional[Match]]] = [
            (s, e, float("inf"), None) for s, e in protected
        ]
        items.extend((c.start, c.end, c.priority, c) for c in candidates)
        items.sort(key=lambda x: x[0])

        accepted: list[Match] = []
        n = len(items)
        i = 0
        while i < n:
            cluster_end = items[i][1]
            j = i + 1
            while j < n and items[j][0] < cluster_end:
                if items[j][1] > cluster_end:
                    cluster_end = items[j][1]
                j += 1
            cluster = items[i:j]
            if len(cluster) == 1:
                m = cluster[0][3]
                if m is not None:
                    accepted.append(m)
            else:
                # Resolve the small cluster by priority-greedy.
                cluster.sort(key=lambda x: (-x[2], -(x[1] - x[0]), x[0]))
                occ: list[tuple[int, int]] = []
                for s, e, _pr, m in cluster:
                    if any(not (e <= os_ or s >= oe) for os_, oe in occ):
                        continue
                    occ.append((s, e))
                    if m is not None:
                        accepted.append(m)
            i = j

        accepted.sort(key=lambda c: c.start)
        return accepted

    # ----- the core text transform ----------------------------------------- #
    def _scrub_text(self, text: str) -> tuple[str, dict[str, int]]:
        """Scrub to a FIXED POINT — loop the single-pass transform until the
        text stops changing.

        A single pass is not guaranteed idempotent: detectors with broad,
        overlapping spans (notably the contextual detector) can produce a
        different winning set on already-scrubbed text, because masking one span
        shifts the boundaries a neighbouring candidate sees. Iterating to a fixed
        point makes ``scrub`` self-stabilising, so ``scrub(scrub(x)) == scrub(x)``
        holds by construction regardless of detector interactions. Pass 2+ runs
        on placeholder-dense text that yields ~no new candidates, so it is cheap
        (a scan, essentially no masking work).
        """
        counts: dict[str, int] = {}
        for _ in range(5):  # converges in 1-2 in practice; cap is a backstop
            text, c = self._scrub_text_once(text)
            if not c:
                break
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
        return text, counts

    def _scrub_text_once(self, text: str) -> tuple[str, dict[str, int]]:
        """One detection+replacement pass. Returns (scrubbed, per-kind counts)."""
        if not text:
            return text, {}

        # Idempotency: protect spans that are ALREADY placeholders so we never
        # redact a redaction.
        protected = [(m.start(), m.end()) for m in _PLACEHOLDER_RE.finditer(text)]

        candidates: list[Match] = []
        for det in self._detectors:
            candidates.extend(det(text))
        candidates.extend(self._detect_denylist(text))

        # Drop allowlisted values (never-redact), but denylist/custom is forced.
        filtered = [
            c for c in candidates
            if c.end > c.start and (c.kind == "custom" or not self._is_allowlisted(c.value))
        ]
        if not filtered and not protected:
            return text, {}

        accepted = self._resolve_overlaps(filtered, protected)  # already start-sorted
        if not accepted:
            return text, {}

        out: list[str] = []
        counts: dict[str, int] = {}
        pos = 0
        for c in accepted:
            out.append(text[pos:c.start])
            out.append(self._placeholder_for(c.value, c.kind, c.label))
            counts[c.kind] = counts.get(c.kind, 0) + 1
            pos = c.end
        out.append(text[pos:])
        return "".join(out), counts

    # ----- public API ------------------------------------------------------- #
    def scrub(self, input_text: str, *, persist: bool = True) -> ScrubResult:
        """Scrub a string. Returns scrubbed text + a value-free report.

        Idempotent: re-scrubbing already-scrubbed text is a no-op.
        """
        scrubbed, counts = self._scrub_text(input_text)
        if persist:
            self.flush()
        report = self._report(counts)
        return ScrubResult(scrubbed, report, str(self.map_path))

    def rehydrate(self, text: str) -> str:
        """Swap placeholders back to their original values (local-only)."""
        if not self._map:
            return text
        return _PLACEHOLDER_RE.sub(
            lambda m: self._map.get(m.group(0), m.group(0)), text
        )

    def scrub_stream(
        self,
        in_fp: Iterable[str],
        out_fp,
        *,
        persist: bool = True,
    ) -> dict:
        """Streaming scrub for large (multi-GB) inputs.

        Reads line by line so memory stays bounded; multi-line PEM blocks are
        buffered until their END marker. Writes scrubbed text to ``out_fp`` and
        returns a value-free redaction report.
        """
        counts: dict[str, int] = {}
        pem_buf: list[str] = []
        in_pem = False

        def bump(c: dict[str, int]) -> None:
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v

        for line in in_fp:
            if not in_pem and "-----BEGIN" in line and "PRIVATE KEY" in line:
                # BEGIN and END may be on the same line, or span many lines.
                # ("PRIVATE KEY" — not "PRIVATE KEY-----" — also matches the
                # "PRIVATE KEY BLOCK-----" / type-prefixed PGP/DSA forms.)
                if "-----END" in line and "PRIVATE KEY" in line.split("-----END", 1)[1]:
                    s, c = self._scrub_text(line)
                    out_fp.write(s)
                    bump(c)
                else:
                    in_pem = True
                    pem_buf = [line]
                continue
            if in_pem:
                pem_buf.append(line)
                if "-----END" in line and "PRIVATE KEY" in line:
                    s, c = self._scrub_text("".join(pem_buf))
                    out_fp.write(s)
                    bump(c)
                    in_pem = False
                    pem_buf = []
                continue
            s, c = self._scrub_text(line)
            out_fp.write(s)
            bump(c)

        if in_pem:  # unterminated block at EOF — scrub what we have
            s, c = self._scrub_text("".join(pem_buf))
            out_fp.write(s)
            bump(c)

        if persist:
            self.flush()
        return self._report(counts)

    def _report(self, counts: dict[str, int]) -> dict:
        return {
            "total": sum(counts.values()),
            "distinct": len(self._inverse),
            "by_category": dict(sorted(counts.items())),
        }


# --------------------------------------------------------------------------- #
# Module-level convenience (one-shot, default config dir)
# --------------------------------------------------------------------------- #
_DEFAULT: Optional[Scrubber] = None


def _default() -> Scrubber:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Scrubber()
    return _DEFAULT


def scrub(input_text: str) -> ScrubResult:
    """One-shot scrub using the default (per-user) config dir."""
    return _default().scrub(input_text)


def rehydrate(text: str) -> str:
    """One-shot rehydrate using the default (per-user) config dir."""
    return _default().rehydrate(text)
