#!/usr/bin/env python3
"""Live usage / remaining quota for AI coding CLIs on this machine.

Part of smart-subagents. Checks Claude Code, Codex (ChatGPT), Grok, and Kimi
Code using each CLI's own stored local credentials. Never prints or transmits
tokens, and redacts account emails unless you ask for them.

Usage:
  ai-cli-usage.py                    # human table + recommendation
  ai-cli-usage.py --json             # machine-readable
  ai-cli-usage.py --recommend        # one-line pick only
  ai-cli-usage.py --cli codex        # single CLI
  ai-cli-usage.py --include-account  # opt in to unredacted emails

Exit codes: 0 ok (some CLIs may be degraded/unavailable), 2 total failure.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The worker registry is the single source of per-CLI knowledge: which workers
# exist, which probe reports on each, the effort ladder, the capability priors
# and the difficulty-to-flag mapping. Adding a worker is an entry there.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssa import registry as ssa_registry  # noqa: E402

HOME = Path.home()
TIMEOUT = 15


def _registry():
    try:
        return ssa_registry.load()
    except ssa_registry.RegistryError as exc:
        print("ai-cli-usage: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


REGISTRY = _registry()


@dataclass
class Window:
    name: str
    used_pct: Optional[float] = None  # 0-100, higher = more consumed
    remaining_pct: Optional[float] = None
    used: Optional[float] = None
    limit: Optional[float] = None
    remaining: Optional[float] = None
    unit: str = "pct"
    resets_at: Optional[str] = None  # ISO or human
    resets_in_hours: Optional[float] = None
    severity: str = "ok"  # ok | low | critical | exhausted | unknown
    note: str = ""
    # How long the whole window is, when the provider says so. Needed to tell a
    # 5h burst window from a monthly budget, which score very differently.
    period_seconds: Optional[float] = None
    # Ranking-only headroom (see effective_score). Severity stays on used_pct.
    effective: Optional[float] = None
    # Capacity this window can fund (see admission_score). What the floor reads.
    admission: Optional[float] = None
    forecast_basis: str = ""  # short | pace | raw
    forecast_exhausts_in_hours: Optional[float] = None


@dataclass
class CliStatus:
    cli: str
    available: bool
    plan: str = ""
    account: str = ""
    windows: list[Window] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    score: float = 0.0  # higher = more headroom for work (0-100)
    eligible: bool = False
    skip_reason: str = ""
    # Min effective headroom over the binding windows. None until computed, and
    # it falls back to `score` so a synthetic status still ranks.
    effective_score: Optional[float] = None
    # What the quota floor is checked against. Pace is not capacity, so this
    # stays on raw remaining except where a reset is genuinely imminent.
    admission_score: Optional[float] = None


def _now() -> float:
    return time.time()


def _iso_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _parse_iso(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return None


def _hours_until(iso_or_ts: Any) -> Optional[float]:
    if iso_or_ts is None:
        return None
    if isinstance(iso_or_ts, (int, float)):
        return (float(iso_or_ts) - _now()) / 3600.0
    ts = _parse_iso(str(iso_or_ts))
    if ts is None:
        return None
    return (ts - _now()) / 3600.0


def _severity(used_pct: Optional[float]) -> str:
    if used_pct is None:
        return "unknown"
    if used_pct >= 99.5:
        return "exhausted"
    if used_pct >= 90:
        return "critical"
    if used_pct >= 70:
        return "low"
    return "ok"


# ---------------------------------------------------------------------------
# Missing and malformed provider data
#
# Every provider gets the same two answers, because the old per-provider
# improvisation (codex assumed the worst, grok and kimi assumed the best) meant
# the same silence routed three different ways.
#
#   missing   -> available, not eligible, skip_reason "usage data missing"
#   malformed -> that field becomes None and the status carries a warning
#
# Both fail closed: a worker whose quota nobody can read is not a worker you
# can dispatch on, and it is never given invented headroom.
# ---------------------------------------------------------------------------

USAGE_MISSING = "usage data missing"


def _warn(st: "CliStatus", message: str) -> None:
    st.extras.setdefault("warnings", []).append(message)


def _num(st: "CliStatus", value: Any, field_name: str) -> Optional[float]:
    """float(value), or None plus a warning. Never raises on provider junk."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        _warn(st, "%s: not a number (%r), dropped" % (field_name, value))
        return None


def _mark_missing_usage(st: "CliStatus") -> None:
    st.available = True
    st.eligible = False
    st.skip_reason = USAGE_MISSING
    st.score = 0.0


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)
    return v if v > 0 else float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = int(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return int(default)
    return v if v > 0 else int(default)


# ---------------------------------------------------------------------------
# Effective headroom
#
# Subscription quota has zero marginal cost and is worth nothing at reset, so
# raw "percent left" is the wrong ranking key. Two corrections:
#
#   Short windows (<= 6h): quota that resets in minutes is nearly free, so the
#   penalty for having spent it decays with the reset horizon.
#   Long windows (weekly, monthly): the question is whether you are ahead of
#   pace for the period. Being behind pace is not a bonus; being ahead is a cost.
#
# Raw used_pct still drives severity labels and the exhaustion gates. This is a
# ranking key only.
#
# Admission is a different question and gets a different number. Being behind
# pace on a weekly window does not create capacity: 7% left is 7% left whether
# you spent it early or late. So the quota floor is checked against
# admission_score, which stays on raw remaining for long windows and only
# credits a short window whose reset is genuinely imminent.
# ---------------------------------------------------------------------------

SHORT_WINDOW_MAX_SECONDS = 6 * 3600


def _pace_pct(w: Window) -> Optional[float]:
    period = w.period_seconds
    if not period or period <= 0 or w.resets_in_hours is None:
        return None
    elapsed = float(period) - max(0.0, float(w.resets_in_hours)) * 3600.0
    return 100.0 * min(1.0, max(0.0, elapsed / float(period)))


def effective_score(w: Window) -> float:
    """Headroom worth ranking on, 0-100. Never used for severity or gating."""
    used = w.used_pct
    if used is None:
        return float(w.remaining_pct) if w.remaining_pct is not None else 50.0
    used = float(used)
    period = w.period_seconds
    if period is not None and 0 < float(period) <= SHORT_WINDOW_MAX_SECONDS:
        if w.resets_in_hours is None:
            return max(0.0, 100.0 - used)
        horizon = _env_float("SSA_SHORT_HORIZON_HOURS", 4.0)
        frac = min(1.0, max(0.0, float(w.resets_in_hours)) / horizon)
        return max(0.0, 100.0 - used * frac)
    pace = _pace_pct(w)
    if pace is not None:
        return max(0.0, 100.0 - max(0.0, used - pace))
    return max(0.0, 100.0 - used)


def admission_score(w: Window) -> float:
    """Capacity this window can actually fund, 0-100. What the floor checks."""
    used = w.used_pct
    if used is None:
        return float(w.remaining_pct) if w.remaining_pct is not None else 50.0
    period = w.period_seconds
    if period is not None and 0 < float(period) <= SHORT_WINDOW_MAX_SECONDS:
        # An imminent reset is real capacity: the window refills before the run
        # can spend what is left of it.
        return effective_score(w)
    return max(0.0, 100.0 - float(used))


def _effective_basis(w: Window) -> str:
    period = w.period_seconds
    if w.used_pct is None:
        return "raw"
    if period is not None and 0 < float(period) <= SHORT_WINDOW_MAX_SECONDS:
        return "short" if w.resets_in_hours is not None else "raw"
    return "pace" if _pace_pct(w) is not None else "raw"


def binding_windows(st: CliStatus) -> list:
    """Windows that actually gate this CLI, mirroring the claude probe's choice."""
    if st.cli == "claude":
        binding = [
            w
            for w in st.windows
            if _is_premium_window(w.name) or w.name in ("weekly_all", "5h_session")
        ]
        return binding or st.windows
    return st.windows


def apply_effective(st: CliStatus) -> None:
    """Annotate every window, then take the worst on each of the two axes."""
    for w in st.windows:
        w.effective = round(effective_score(w), 1)
        w.admission = round(admission_score(w), 1)
        w.forecast_basis = _effective_basis(w)
    binding = binding_windows(st)
    vals = [w.effective for w in binding if w.effective is not None]
    st.effective_score = round(min(vals), 1) if vals else None
    adm = [w.admission for w in binding if w.admission is not None]
    st.admission_score = round(min(adm), 1) if adm else None


def eff_of(st: CliStatus) -> float:
    """Effective headroom, falling back to raw score when nothing was computed."""
    return float(st.effective_score if st.effective_score is not None else st.score)


def adm_of(st: CliStatus) -> float:
    """Admissible capacity, falling back to raw score when nothing was computed."""
    return float(st.admission_score if st.admission_score is not None else st.score)


def _http_json(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    data: Optional[bytes] = None,
) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body) if body else {}
            except json.JSONDecodeError:
                return r.status, {"_raw": body[:2000]}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body) if body else {"_error": str(e)}
        except json.JSONDecodeError:
            return e.code, {"_error": str(e), "_raw": body[:500]}
    except Exception as e:
        return 0, {"_error": str(e)}


def _jwt_payload(tok: str) -> dict:
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def _keychain_claude_creds() -> Optional[dict]:
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def check_claude() -> CliStatus:
    st = CliStatus(cli="claude", available=False)
    creds = _keychain_claude_creds()
    if not creds or "claudeAiOauth" not in creds:
        # Fallback: claude auth status for plan only
        try:
            out = subprocess.check_output(
                ["claude", "auth", "status"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            info = json.loads(out)
            st.available = bool(info.get("loggedIn"))
            st.plan = str(info.get("subscriptionType") or "")
            st.account = str(info.get("email") or "")
            st.error = "oauth token missing from keychain; plan known but no live meters"
            return st
        except Exception as e:
            st.error = f"not logged in / no credentials: {e}"
            return st

    oauth = creds["claudeAiOauth"]
    access = oauth.get("accessToken") or ""
    st.plan = str(oauth.get("subscriptionType") or "")
    st.extras["rate_limit_tier"] = oauth.get("rateLimitTier")
    exp = oauth.get("expiresAt")
    if exp and exp < int(_now() * 1000):
        st.error = "access token expired; re-login with `claude auth login`"
        return st

    code, data = _http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-code/cli",
        },
    )
    if code != 200 or not isinstance(data, dict):
        st.error = f"usage API HTTP {code}: {data}"
        return st

    # profile for email/plan
    pcode, profile = _http_json(
        "https://api.anthropic.com/api/oauth/profile",
        {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-code/cli",
        },
    )
    profile_dict = profile if (pcode == 200 and isinstance(profile, dict)) else None
    return parse_claude_usage(data, profile_dict, st)


def parse_claude_usage(
    data: dict, profile: Optional[dict] = None, st: Optional[CliStatus] = None
) -> CliStatus:
    """Pure: turn a Claude oauth/usage payload (+ optional profile) into a CliStatus.

    No network, no credentials. `st` lets the caller carry over fields it
    already knows (plan/extras from the oauth token) before this fills in the
    rest; a fresh CliStatus is created when not given.
    """
    st = st if st is not None else CliStatus(cli="claude", available=True)

    if profile:
        acc = profile.get("account") or {}
        org = profile.get("organization") or {}
        st.account = str(acc.get("email") or "")
        st.plan = str(org.get("organization_type") or st.plan or org.get("rate_limit_tier") or "")
        st.extras["subscription_status"] = org.get("subscription_status")
        st.extras["rate_limit_tier"] = org.get("rate_limit_tier") or st.extras.get(
            "rate_limit_tier"
        )

    st.available = True

    def add_window(
        name: str,
        block: Optional[dict],
        unit: str = "pct",
        period_seconds: Optional[float] = None,
    ) -> None:
        if not block:
            return
        # Claude oauth usage: five_hour=1.0 means 1%, seven_day=63.0 means 63%.
        used_pct = _num(st, block.get("utilization"), "%s.utilization" % name)
        resets = block.get("resets_at")
        st.windows.append(
            Window(
                name=name,
                used_pct=used_pct,
                remaining_pct=(100.0 - used_pct) if used_pct is not None else None,
                unit=unit,
                resets_at=resets,
                resets_in_hours=_hours_until(resets),
                severity=_severity(used_pct),
                period_seconds=period_seconds,
            )
        )

    add_window("5h_session", data.get("five_hour"), period_seconds=5 * 3600)
    add_window("weekly_all", data.get("seven_day"), period_seconds=7 * 86400)
    add_window("weekly_opus", data.get("seven_day_opus"), period_seconds=7 * 86400)
    add_window("weekly_sonnet", data.get("seven_day_sonnet"), period_seconds=7 * 86400)

    # Model-scoped extra limits (e.g. a per-model weekly cap). Skip kinds already covered.
    seen = {w.name for w in st.windows}
    kind_alias = {
        "session": "5h_session",
        "weekly_all": "weekly_all",
    }
    for lim in data.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        kind = lim.get("kind") or "limit"
        scope = lim.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name") or (
            scope.get("model") or {}
        ).get("id")
        base = kind_alias.get(kind, kind)
        name = f"{base}_{model}" if model else base
        if name in seen and not model:
            continue
        if lim.get("percent") is None:
            continue
        used_pct = _num(st, lim.get("percent"), "limits[%s].percent" % name)
        if used_pct is None:
            continue
        resets = lim.get("resets_at")
        note = "active binding limit" if lim.get("is_active") else ""
        st.windows.append(
            Window(
                name=name,
                used_pct=used_pct,
                remaining_pct=100.0 - used_pct,
                resets_at=resets,
                resets_in_hours=_hours_until(resets),
                severity=_severity(used_pct),
                note=note,
                period_seconds=(5 * 3600 if base == "5h_session" else 7 * 86400),
            )
        )
        seen.add(name)

    extra = data.get("extra_usage") or {}
    if extra:
        st.extras["extra_usage"] = {
            "is_enabled": extra.get("is_enabled"),
            "disabled_reason": extra.get("disabled_reason"),
            "used_credits": extra.get("used_credits"),
        }

    # Score for "can the local session do labor": worst of binding weekly + session
    binding = [
        w
        for w in st.windows
        if _is_premium_window(w.name) or w.name in ("weekly_all", "5h_session")
    ]
    if not binding:
        binding = st.windows
    known = [w.used_pct for w in binding if w.used_pct is not None]
    if not known:
        # No readable meter on any binding window: fail closed rather than
        # score a blank as 100.
        _mark_missing_usage(st)
        st.extras["local_labor"] = False
        return st
    worst = max(known)
    st.score = max(0.0, 100.0 - worst)
    if worst >= 99.5:
        st.eligible = False
        st.skip_reason = "Claude quota exhausted on a binding window"
    elif worst >= 90 and any(
        _is_premium_window(w.name) and (w.used_pct or 0) >= 90 for w in st.windows
    ):
        # Top tier critical: cheap in-session models still fine, premium labor is not
        st.eligible = True
        st.skip_reason = (
            "premium weekly window critical — supervise only; prefer CLI workers"
        )
        st.extras["local_labor"] = False
    else:
        st.eligible = True
        st.extras["local_labor"] = worst < 85

    return st


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def check_codex() -> CliStatus:
    st = CliStatus(cli="codex", available=False)
    auth_path = HOME / ".codex" / "auth.json"
    if not auth_path.exists():
        st.error = "no ~/.codex/auth.json"
        return st
    try:
        auth = json.loads(auth_path.read_text())
    except Exception as e:
        st.error = f"bad auth.json: {e}"
        return st

    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token") or ""
    account_id = tokens.get("account_id") or ""
    if not access:
        st.error = "no access_token (run `codex login`)"
        return st

    pl = _jwt_payload(access)
    profile = pl.get("https://api.openai.com/profile") or {}
    oauth = pl.get("https://api.openai.com/auth") or {}
    st.account = str(profile.get("email") or "")
    st.plan = str(oauth.get("chatgpt_plan_type") or auth.get("auth_mode") or "")

    code, data = _http_json(
        "https://chatgpt.com/backend-api/wham/usage",
        {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "ChatGPT-Account-Id": account_id,
            "User-Agent": "codex-cli",
        },
    )
    if code != 200 or not isinstance(data, dict):
        st.error = f"usage API HTTP {code}: {data}"
        return st

    return parse_codex_usage(data, st)


def _codex_window_seconds(block: dict) -> Optional[float]:
    try:
        v = float(block.get("limit_window_seconds"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_codex_usage(data: dict, st: Optional[CliStatus] = None) -> CliStatus:
    """Pure: turn a Codex wham/usage payload into a CliStatus. No network."""
    st = st if st is not None else CliStatus(cli="codex", available=True)
    st.available = True
    st.plan = str(data.get("plan_type") or st.plan)
    st.account = str(data.get("email") or st.account)

    rl = data.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    used_pct = _num(st, pw.get("used_percent"), "primary_window.used_percent")
    reset_at = pw.get("reset_at")
    reset_after = pw.get("reset_after_seconds")
    resets_iso = None
    if reset_at:
        resets_iso = datetime.fromtimestamp(float(reset_at), tz=timezone.utc).isoformat()

    w = Window(
        name="primary_window",
        used_pct=used_pct,
        remaining_pct=(100.0 - used_pct) if used_pct is not None else None,
        resets_at=resets_iso,
        resets_in_hours=(float(reset_after) / 3600.0) if reset_after is not None else _hours_until(reset_at),
        severity=_severity(used_pct),
        note=f"window={pw.get('limit_window_seconds')}s",
        period_seconds=_codex_window_seconds(pw),
    )
    st.windows.append(w)

    if rl.get("secondary_window"):
        sw = rl["secondary_window"]
        used2 = _num(st, sw.get("used_percent"), "secondary_window.used_percent")
        reset_after2 = sw.get("reset_after_seconds")
        st.windows.append(
            Window(
                name="secondary_window",
                used_pct=used2,
                remaining_pct=(100.0 - used2) if used2 is not None else None,
                resets_in_hours=(
                    float(reset_after2) / 3600.0 if reset_after2 is not None else None
                ),
                severity=_severity(used2),
                period_seconds=_codex_window_seconds(sw),
            )
        )

    credits = data.get("credits") or {}
    resets = data.get("rate_limit_reset_credits") or {}
    st.extras["credits_balance"] = credits.get("balance")
    st.extras["has_credits"] = credits.get("has_credits")
    st.extras["banked_resets"] = resets.get("available_count") or 0
    st.extras["limit_reached"] = bool(rl.get("limit_reached"))
    st.extras["allowed"] = rl.get("allowed")

    if used_pct is None:
        # No readable primary window: not "fully spent", just unknown, and
        # unknown is not something to dispatch on.
        _mark_missing_usage(st)
        return st

    used = used_pct
    st.score = max(0.0, 100.0 - used)
    # Boost slightly if banked resets exist but still mark exhausted primary
    if used >= 99.5:
        if st.extras["banked_resets"] and st.extras["banked_resets"] > 0:
            st.eligible = False
            st.skip_reason = (
                f"Codex primary window exhausted; {st.extras['banked_resets']} banked "
                "reset(s) available but do not auto-redeem — pick another CLI or ask user"
            )
            st.score = 5.0
        else:
            st.eligible = False
            st.skip_reason = "Codex rate limit exhausted"
    else:
        st.eligible = True

    return st


# ---------------------------------------------------------------------------
# Grok
# ---------------------------------------------------------------------------


def check_grok() -> CliStatus:
    st = CliStatus(cli="grok", available=False)
    auth_path = HOME / ".grok" / "auth.json"
    if not auth_path.exists():
        st.error = "no ~/.grok/auth.json"
        return st
    try:
        auth = json.loads(auth_path.read_text())
    except Exception as e:
        st.error = f"bad auth.json: {e}"
        return st

    entry = None
    for v in auth.values():
        if isinstance(v, dict) and v.get("key"):
            entry = v
            break
    if not entry:
        st.error = "no oauth key in auth.json (run `grok login`)"
        return st

    token = entry["key"]
    st.account = str(entry.get("email") or "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "GrokBuild/cli-usage",
    }

    # Subscription
    scode, subs = _http_json("https://grok.com/rest/subscriptions", headers)
    active_tiers = []
    if scode == 200 and isinstance(subs, dict):
        for s in subs.get("subscriptions") or []:
            if s.get("status") == "SUBSCRIPTION_STATUS_ACTIVE":
                active_tiers.append(s.get("tier") or "active")
                st.extras["billing_period_end"] = s.get("billingPeriodEnd")
                st.extras["cancel_at_period_end"] = s.get("cancelAtPeriodEnd")
                offer = s.get("activeOffer") or {}
                if offer:
                    st.extras["active_offer"] = {
                        "end": offer.get("offerEnd"),
                        "discount_pct": (offer.get("discount") or {}).get("percentageOff"),
                    }
    st.plan = ", ".join(active_tiers) if active_tiers else "unknown"

    # Grok Build CLI monthly credits
    bcode, billing = _http_json("https://cli-chat-proxy.grok.com/v1/billing", headers)
    if bcode != 200 or not isinstance(billing, dict):
        # User endpoint still useful
        ucode, user = _http_json("https://cli-chat-proxy.grok.com/v1/user", headers)
        if ucode == 200 and isinstance(user, dict):
            st.available = bool(user.get("hasGrokCodeAccess"))
            st.account = str(user.get("email") or st.account)
            st.error = f"billing HTTP {bcode}; hasGrokCodeAccess={user.get('hasGrokCodeAccess')}"
            st.eligible = st.available
            st.score = 50.0 if st.available else 0.0
            if not st.available:
                st.skip_reason = "no Grok Code access"
            return st
        st.error = f"billing HTTP {bcode}: {billing}"
        return st

    ucode, user = _http_json("https://cli-chat-proxy.grok.com/v1/user", headers)
    user_dict = user if (ucode == 200 and isinstance(user, dict)) else None
    return parse_grok_usage(billing, user_dict, st)


def parse_grok_usage(
    billing: dict, user: Optional[dict] = None, st: Optional[CliStatus] = None
) -> CliStatus:
    """Pure: turn a Grok CLI billing payload (+ optional /user) into a CliStatus.

    `billing` may nest its fields under "config" or carry them at the root;
    both shapes are handled the same way. No network.
    """
    st = st if st is not None else CliStatus(cli="grok", available=True)
    cfg = billing.get("config") or billing
    limit_v = ((cfg.get("monthlyLimit") or {}).get("val"))
    used_v = ((cfg.get("used") or {}).get("val"))
    on_demand = ((cfg.get("onDemandCap") or {}).get("val"))
    period_start = cfg.get("billingPeriodStart")
    period_end = cfg.get("billingPeriodEnd")

    st.available = True
    limit_f = _num(st, limit_v, "monthlyLimit.val")
    used_f = _num(st, used_v, "used.val")
    if limit_f is not None and used_f is not None:
        rem = max(0.0, limit_f - used_f)
        used_pct = (used_f / limit_f * 100.0) if limit_f > 0 else 0.0
        start_ts, end_ts = _parse_iso(str(period_start or "")), _parse_iso(
            str(period_end or "")
        )
        period_len = (
            (end_ts - start_ts) if (start_ts and end_ts and end_ts > start_ts) else None
        )
        st.windows.append(
            Window(
                name="monthly_cli_credits",
                used_pct=used_pct,
                remaining_pct=100.0 - used_pct,
                used=used_f,
                limit=limit_f,
                remaining=rem,
                unit="credits",
                resets_at=period_end,
                resets_in_hours=_hours_until(period_end),
                severity=_severity(used_pct),
                note=f"period {period_start} → {period_end}; on_demand_cap={on_demand}",
                period_seconds=period_len,
            )
        )
        st.score = max(0.0, 100.0 - used_pct)
        st.eligible = used_pct < 99.5
        if not st.eligible:
            st.skip_reason = "Grok monthly CLI credits exhausted"
    else:
        # No credit meter to read. Same answer as every other provider.
        _mark_missing_usage(st)

    if user is not None:
        st.extras["has_grok_code_access"] = user.get("hasGrokCodeAccess")
        if user.get("hasGrokCodeAccess") is False:
            st.eligible = False
            st.skip_reason = "hasGrokCodeAccess=false"
            st.score = 0.0

    return st


# ---------------------------------------------------------------------------
# Kimi
# ---------------------------------------------------------------------------


def _kimi_refresh_if_needed(cred: dict, path: Path) -> dict:
    exp = float(cred.get("expires_at") or 0)
    if exp > _now() + 30:
        return cred
    # Force CLI refresh with a trivial prompt (updates credentials file).
    refresh_dir = tempfile.mkdtemp()
    try:
        subprocess.run(
            [
                str(HOME / ".kimi-code" / "bin" / "kimi"),
                "-p",
                "Reply with exactly: ok",
                "--output-format",
                "text",
            ],
            cwd=refresh_dir,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception:
        pass
    finally:
        shutil.rmtree(refresh_dir, ignore_errors=True)
    try:
        return json.loads(path.read_text())
    except Exception:
        return cred


def check_kimi() -> CliStatus:
    st = CliStatus(cli="kimi", available=False)
    path = HOME / ".kimi-code" / "credentials" / "kimi-code.json"
    if not path.exists():
        st.error = "no ~/.kimi-code/credentials/kimi-code.json (run `kimi login`)"
        return st
    try:
        cred = json.loads(path.read_text())
    except Exception as e:
        st.error = f"bad credentials: {e}"
        return st

    cred = _kimi_refresh_if_needed(cred, path)
    token = cred.get("access_token") or ""
    if not token:
        st.error = "empty access_token"
        return st

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "kimi-code/cli-usage",
    }
    code, data = _http_json("https://api.kimi.com/coding/v1/usages", headers)
    if code == 401:
        cred = _kimi_refresh_if_needed(
            {**cred, "expires_at": 0}, path
        )  # force refresh
        token = cred.get("access_token") or token
        headers["Authorization"] = f"Bearer {token}"
        code, data = _http_json("https://api.kimi.com/coding/v1/usages", headers)

    if code != 200 or not isinstance(data, dict):
        st.error = f"usages HTTP {code}: {data}"
        return st

    mcode, me = _http_json("https://api.kimi.com/coding/v1/me", headers)
    me_dict = me if (mcode == 200 and isinstance(me, dict)) else None
    return parse_kimi_usage(data, me_dict, st)


def parse_kimi_usage(
    data: dict, me: Optional[dict] = None, st: Optional[CliStatus] = None
) -> CliStatus:
    """Pure: turn a Kimi /usages payload (+ optional /me) into a CliStatus.

    Covers the weekly quota window and every throughput/window limit under
    data["limits"]. No network.
    """
    st = st if st is not None else CliStatus(cli="kimi", available=True)
    st.available = True
    user = data.get("user") or {}
    membership = user.get("membership") or {}
    st.extras["membership_level"] = membership.get("level")
    st.extras["region"] = user.get("region")
    st.extras["sub_type"] = data.get("subType")

    if me is not None:
        st.account = str(me.get("nickname") or me.get("user_id") or "")
        st.plan = str(me.get("user_level_name") or me.get("user_level") or "")
    else:
        st.plan = str(membership.get("level") or "")

    usage = data.get("usage") or {}
    limit_f = _num(st, usage.get("limit"), "usage.limit")
    used_f = _num(st, usage.get("used"), "usage.used")
    rem_f = _num(st, usage.get("remaining"), "usage.remaining")

    used_pct = None
    if limit_f and used_f is not None and limit_f > 0:
        used_pct = used_f / limit_f * 100.0
    resets = usage.get("resetTime")
    st.windows.append(
        Window(
            name="weekly_quota",
            used_pct=used_pct,
            remaining_pct=(100.0 - used_pct) if used_pct is not None else None,
            used=used_f,
            limit=limit_f,
            remaining=rem_f,
            unit="quota_units",
            resets_at=resets,
            resets_in_hours=_hours_until(resets),
            severity=_severity(used_pct),
            period_seconds=7 * 86400,
        )
    )

    for lim in data.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        window = lim.get("window") or {}
        detail = lim.get("detail") or {}
        dur = window.get("duration")
        unit = str(window.get("timeUnit") or "")
        period_len: Optional[float] = None
        # TIME_UNIT_MINUTE + duration 300 ≈ 5h throughput window
        if unit.endswith("MINUTE") and dur is not None:
            name = f"throughput_{dur}m"
            period_len = float(dur) * 60.0
        elif unit.endswith("HOUR") and dur is not None:
            name = f"throughput_{dur}h"
            period_len = float(dur) * 3600.0
        else:
            name = f"window_{dur}_{unit}".lower().replace("time_unit_", "")
            if unit.endswith("DAY") and dur is not None:
                period_len = float(dur) * 86400.0
        d_limit = _num(st, detail.get("limit"), "limits[%s].limit" % name)
        d_rem = _num(st, detail.get("remaining"), "limits[%s].remaining" % name)
        d_used = None
        d_pct = None
        if d_limit is not None and d_rem is not None:
            d_used = d_limit - d_rem
            d_pct = (d_used / d_limit * 100.0) if d_limit > 0 else 0.0
        st.windows.append(
            Window(
                name=name,
                used_pct=d_pct,
                remaining_pct=(100.0 - d_pct) if d_pct is not None else None,
                used=d_used,
                limit=d_limit,
                remaining=d_rem,
                unit="quota_units",
                resets_at=detail.get("resetTime"),
                resets_in_hours=_hours_until(detail.get("resetTime")),
                severity=_severity(d_pct),
                period_seconds=period_len,
            )
        )

    parallel = data.get("parallel") or {}
    if parallel.get("limit") is not None:
        st.extras["parallel_limit"] = parallel.get("limit")

    if used_pct is None:
        # No readable weekly meter: unknown is not "fully available".
        _mark_missing_usage(st)
    else:
        st.score = max(0.0, 100.0 - used_pct)
        st.eligible = used_pct < 99.5
        if not st.eligible:
            st.skip_reason = "Kimi weekly quota exhausted"
    # Safety note for the supervisor (not a skip). The capability itself is a
    # registry fact, not a hardcoded name.
    spec = REGISTRY.workers.get("kimi")
    sandboxed = bool(spec) and spec.sandbox != "none"
    st.extras["sandbox"] = sandboxed
    if not sandboxed:
        st.extras["dispatch_note"] = "no sandbox, worktree only"

    return st


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

# Who the workers are is a registry fact. Claude is also a worker (Fable)
# when the registry says so; local_labor_ok still meters the same probe.
WORKER_CLIS = REGISTRY.names


def cli_check_names() -> list[str]:
    """Unique names for `--cli`, supervisor first, then workers, then all."""
    names = ["claude"]
    for name in WORKER_CLIS:
        if name not in names:
            names.append(name)
    names.append("all")
    return names

# Cache holds plan, quota and (unredacted) account identifiers, so it never goes
# in world-readable /tmp. Honors XDG_CACHE_HOME; created 0700, written 0600.
_CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME") or (HOME / ".cache")
) / "smart-subagents"
CACHE_PATH = _CACHE_DIR / "ai-cli-usage.json"
CACHE_TTL_SEC = 180  # 3 minutes — fresh enough for routing, cheap on repeated picks

# Model-scoped weekly windows that gate whether the *local* Claude session should
# do labor itself. Names come from the usage API's limits[].scope.model
# .display_name, so override when your plan exposes a different top tier.
PREMIUM_MODEL_WINDOWS: tuple[str, ...] = tuple(
    m.strip()
    for m in os.environ.get("SSA_PREMIUM_MODELS", "Fable,Opus").split(",")
    if m.strip()
)


def _is_premium_window(name: str) -> bool:
    return any(m.lower() in (name or "").lower() for m in PREMIUM_MODEL_WINDOWS)


# Capability priors by task kind (multiplied into headroom score, not hard
# gates). Higher = better default fit when quotas are comparable. Each worker
# declares its own priors in the registry; a kind a worker says nothing about
# defaults to a neutral 1.0.
FIT = REGISTRY.fit_table()

FIT_MIN, FIT_MAX = 0.85, 1.15
# A FIT multiplier of 1.0 means "the usual outcome", which is a 0.75 success
# rate, not a perfect one. Both directions of that mapping live here so the
# scale is never re-derived by eye somewhere else.
FIT_NEUTRAL_SUCCESS = 0.75
FIT_PRIOR_WEIGHT = 8.0  # pseudo-observations behind the static prior
FIT_SHRINK_SAMPLES = 5.0  # below this, blend the cell toward the CLI aggregate


def fit_to_success(multiplier: float) -> float:
    """FIT multiplier -> expected success rate on 0..1."""
    return max(0.0, min(1.0, FIT_NEUTRAL_SUCCESS * float(multiplier)))


def success_to_fit(success: float) -> float:
    """Expected success rate -> FIT multiplier, clamped to the advisory band."""
    return max(FIT_MIN, min(FIT_MAX, float(success) / FIT_NEUTRAL_SUCCESS))


# ---------------------------------------------------------------------------
# State: cooldowns, the outcome ledger, and the usage history used to forecast
# burn. All under XDG_STATE_HOME because they must survive a cache wipe.
# ---------------------------------------------------------------------------


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(HOME / ".local" / "state")
    return Path(base) / "smart-subagents"


def _ensure_state_dir() -> Path:
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _cooldown_path() -> Path:
    return _state_dir() / "cooldowns.json"


def _ledger_path() -> Path:
    override = os.environ.get("SSA_LEDGER")
    return Path(override) if override else _state_dir() / "outcomes.jsonl"


def _history_path() -> Path:
    return _state_dir() / "usage-history.jsonl"


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write inside the 0700 state dir."""
    _ensure_state_dir()
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), prefix=path.name + ".", delete=False
        ) as fh:
            tmp_name = fh.name
            os.chmod(tmp_name, 0o600)
            fh.write(text)
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


COOLDOWN_REASONS = ("rate-limit", "auth")
# Never open-ended: a mistake here would quietly retire a worker forever.
COOLDOWN_DEFAULT_MINUTES = {"rate-limit": 15, "auth": 24 * 60}
COOLDOWN_MAX_MINUTES = 7 * 24 * 60


def _read_cooldowns() -> dict:
    try:
        data = json.loads(_cooldown_path().read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_cooldowns(now: Optional[float] = None) -> dict:
    """Active cooldowns only: {cli: {until, reason, set_at, minutes_left}}."""
    now = _now() if now is None else now
    out = {}
    for cli, entry in _read_cooldowns().items():
        if not isinstance(entry, dict):
            continue
        try:
            until = float(entry.get("until") or 0)
        except (TypeError, ValueError):
            continue
        if until <= now:
            continue
        out[cli] = {
            "until": until,
            "reason": str(entry.get("reason") or "rate-limit"),
            "set_at": entry.get("set_at"),
            "minutes_left": int((until - now) / 60) + 1,
        }
    return out


def set_cooldown(cli: str, reason: str, minutes: Optional[float] = None) -> dict:
    reason = reason if reason in COOLDOWN_REASONS else "rate-limit"
    if minutes is None:
        minutes = COOLDOWN_DEFAULT_MINUTES[reason]
    minutes = max(1.0, min(float(minutes), float(COOLDOWN_MAX_MINUTES)))
    now = _now()
    data = _read_cooldowns()
    entry = {"until": now + minutes * 60.0, "reason": reason, "set_at": now}
    data[cli] = entry
    _write_private(_cooldown_path(), json.dumps(data, indent=2) + "\n")
    return entry


def clear_cooldown(cli: str) -> bool:
    data = _read_cooldowns()
    if cli not in data:
        return False
    del data[cli]
    _write_private(_cooldown_path(), json.dumps(data, indent=2) + "\n")
    return True


# ---------------------------------------------------------------------------
# Learned fit: empirical Bayes over the outcome ledger, promoted cautiously.
#
# The static FIT table is a cold-start prior, nothing more. Once a (worker,
# kind) cell has real dispatches behind it, the posterior takes over, but only
# past SSA_FIT_MIN_SAMPLES effective observations and only inside a clamp
# narrow enough that a bad streak cannot retire a worker.
# ---------------------------------------------------------------------------

FIT_REWARD_EXCLUDED = ("blocked", "env-blocked", "rate-limited")


def _outcome_reward(rec: dict) -> Optional[float]:
    outcome = str(rec.get("outcome") or "")
    if outcome in FIT_REWARD_EXCLUDED:
        return None
    try:
        retries = int(rec.get("retries") or 0)
    except (TypeError, ValueError):
        retries = 0
    if outcome == "verified-pass":
        return 1.0 if retries <= 1 else 0.5
    if outcome == "partial":
        return 0.5
    if outcome == "rejected":
        return 0.0
    return None


def read_ledger() -> tuple:
    """(rows, skipped). A corrupt line is skipped and counted, never fatal."""
    path = _ledger_path()
    rows: list = []
    skipped = 0
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return rows, skipped
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            skipped += 1
            continue
        if not isinstance(rec, dict):
            skipped += 1
            continue
        ts = _parse_iso(str(rec.get("ts") or ""))
        if ts is None:
            skipped += 1
            continue
        rec["_ts"] = ts
        rows.append(rec)
    return rows, skipped


def _decay(age_seconds: float) -> float:
    half = _env_float("SSA_FIT_HALFLIFE_DAYS", 30.0) * 86400.0
    return 0.5 ** (max(0.0, age_seconds) / half)


def _posterior(prior_success: float, samples: list) -> tuple:
    """samples: [(weight, reward)] -> (mean, n_eff)."""
    n_eff = sum(w for w, _ in samples)
    total = sum(w * r for w, r in samples)
    mean = (FIT_PRIOR_WEIGHT * prior_success + total) / (FIT_PRIOR_WEIGHT + n_eff)
    return mean, n_eff


def fit_posterior(
    cli: str,
    kind: str,
    now: Optional[float] = None,
    rows: Optional[list] = None,
) -> tuple:
    """(multiplier, n_eff, used) for one (worker, kind) cell."""
    now = _now() if now is None else now
    if rows is None:
        rows, _ = read_ledger()
    table = FIT.get(kind) or FIT["default"]
    prior_mult = float(table.get(cli, 1.0))
    prior_success = fit_to_success(prior_mult)

    cell: list = []
    agg: list = []
    for rec in rows:
        if (rec.get("worker") or "") != cli:
            continue
        reward = _outcome_reward(rec)
        if reward is None:
            continue
        weight = _decay(now - float(rec.get("_ts") or now))
        agg.append((weight, reward))
        if (rec.get("kind") or "default") == kind:
            cell.append((weight, reward))

    cell_mean, n_eff = _posterior(prior_success, cell)
    if n_eff < FIT_SHRINK_SAMPLES:
        # Thin cell: borrow strength from everything this worker has done.
        agg_mean, _ = _posterior(fit_to_success(FIT["default"].get(cli, 1.0)), agg)
        blend = n_eff / FIT_SHRINK_SAMPLES
        cell_mean = blend * cell_mean + (1.0 - blend) * agg_mean

    used = n_eff >= float(_env_int("SSA_FIT_MIN_SAMPLES", 10))
    return success_to_fit(cell_mean), round(n_eff, 2), used


# ---------------------------------------------------------------------------
# Burn-rate forecast: advisory only. It never gates a dispatch, because a
# forecast built from a handful of samples is a hint, not a measurement.
# ---------------------------------------------------------------------------

HISTORY_KEEP_DAYS = 7
FORECAST_LOOKBACK_HOURS = 6.0
FORECAST_MIN_SNAPSHOTS = 3


def _median(values: list) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def record_usage_history(statuses: list, now: Optional[float] = None) -> None:
    """Append one line per window, then compact to the last 7 days."""
    now = _now() if now is None else now
    fresh = []
    for s in statuses:
        for w in s.windows:
            if w.used_pct is None:
                continue
            fresh.append(
                {
                    "ts": round(now, 0),
                    "cli": s.cli,
                    "window": w.name,
                    "used_pct": round(float(w.used_pct), 2),
                }
            )
    if not fresh:
        return
    path = _history_path()
    cutoff = now - HISTORY_KEEP_DAYS * 86400
    kept: list = []
    try:
        if path.exists():
            for line in path.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if float(rec.get("ts") or 0) >= cutoff:
                        kept.append(rec)
                except Exception:
                    continue
        kept.extend(fresh)
        _write_private(path, "".join(json.dumps(r) + "\n" for r in kept))
    except Exception:
        pass


def read_usage_history() -> list:
    rows: list = []
    try:
        text = _history_path().read_text(errors="replace")
    except Exception:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("cli") and rec.get("window") is not None:
            rows.append(rec)
    return rows


def forecast(
    cli: str,
    window: str,
    now: Optional[float] = None,
    rows: Optional[list] = None,
) -> Optional[float]:
    """Hours until this window exhausts at the current burn, or None."""
    now = _now() if now is None else now
    if rows is None:
        rows = read_usage_history()
    points = []
    for rec in rows:
        if rec.get("cli") != cli or rec.get("window") != window:
            continue
        try:
            ts = float(rec.get("ts"))
            pct = float(rec.get("used_pct"))
        except (TypeError, ValueError):
            continue
        if ts >= now - FORECAST_LOOKBACK_HOURS * 3600:
            points.append((ts, pct))
    if len(points) < FORECAST_MIN_SNAPSHOTS:
        return None
    points.sort()
    slopes = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dt = (points[j][0] - points[i][0]) / 3600.0
            if dt <= 0:
                continue
            dv = points[j][1] - points[i][1]
            if dv < 0:
                # Straddles a reset: the pair says nothing about burn.
                continue
            slopes.append(dv / dt)
    if not slopes:
        return None
    slope = _median(slopes)
    if slope <= 0:
        return None
    remaining = max(0.0, 100.0 - points[-1][1])
    return round(remaining / slope, 2)


def annotate_forecasts(statuses: list, now: Optional[float] = None) -> None:
    rows = read_usage_history()
    if not rows:
        return
    for s in statuses:
        for w in s.windows:
            w.forecast_exhausts_in_hours = forecast(s.cli, w.name, now=now, rows=rows)


# ---------------------------------------------------------------------------
# Difficulty: how *hard* the task is, which is not how *big* it is.
#
# Size answers "how many files does this touch" and gates how much quota
# headroom a worker needs. Difficulty answers "how much thinking does this
# need" and gates how expensively each worker runs. A 15-line lock-free ring
# buffer is small and hard; a 40-file mechanical rename is large and trivial.
# ---------------------------------------------------------------------------

# difficulty -> (target reasoning effort, quota-floor multiplier, cross-review)
DIFFICULTY: dict[str, tuple[str, float, bool]] = {
    "trivial": ("low", 0.6, False),
    "routine": ("medium", 1.0, False),
    "hard": ("high", 1.4, False),
    "frontier": ("xhigh", 1.8, True),
}

# Effort rungs each CLI actually accepts, weakest first, straight from the
# registry. Kept per-CLI rather than assumed uniform: grok enumerates xhigh,
# codex does not document it on every version, and kimi has no effort flag at
# all. A target above a CLI's ceiling clamps to its top rung.
EFFORT_LADDER: dict[str, list[str]] = REGISTRY.effort_ladders()


def _clamp_effort(cli: str, target: str) -> str:
    ladder = EFFORT_LADDER.get(cli) or []
    if not ladder:
        return ""
    order = ["low", "medium", "high", "xhigh"]
    want = order.index(target) if target in order else 1
    best = ladder[0]
    for rung in ladder:
        if rung in order and order.index(rung) <= want:
            best = rung
    return best


def worker_args(cli: str, difficulty: str, task_size: str) -> list[str]:
    """CLI flags that realize `difficulty` for this worker.

    Single source of truth for effort/model selection: the shell dispatcher
    consumes this verbatim instead of reimplementing the mapping. The flag
    shapes and the model rules come from the registry, so a new worker gets
    its effort knob by declaring one, not by adding a branch here.
    """
    spec = REGISTRY.workers.get(cli)
    if spec is None:
        return []
    effort, _, _ = DIFFICULTY.get(difficulty) or DIFFICULTY["routine"]
    rung = _clamp_effort(cli, effort)
    args: list[str] = []
    if rung and spec.effort_flags:
        args.extend(token.replace("{effort}", rung) for token in spec.effort_flags)
    model = spec.model_for(difficulty, task_size)
    if model and spec.model_flags:
        args.extend(token.replace("{model}", model) for token in spec.model_flags)
    return args


def recommend(
    statuses: list[CliStatus],
    task_size: str = "medium",
    task_kind: str = "default",
    prefer: str = "",
    difficulty: str = "routine",
) -> dict[str, Any]:
    """Rank registered worker CLIs for labor. Claude can be one of them."""
    by = {s.cli: s for s in statuses}
    if difficulty not in DIFFICULTY:
        difficulty = "routine"
    effort, floor_mult, cross_review = DIFFICULTY[difficulty]
    # Harder work needs more headroom: retries are likelier and cost more per turn.
    base_floor = {"tiny": 5, "small": 15, "medium": 25, "large": 40}.get(task_size, 25)
    min_score = min(90.0, base_floor * floor_mult)
    now = _now()

    for s in statuses:
        if s.effective_score is None and s.windows:
            apply_effective(s)

    # Cross-task memory: a worker that just 429'd or 401'd is out until it is not.
    cools = load_cooldowns(now=now)
    for name, entry in cools.items():
        s = by.get(name)
        if not s:
            continue
        s.eligible = False
        s.skip_reason = "cooldown (%s, %d min left)" % (
            entry["reason"],
            entry["minutes_left"],
        )

    # Learned fit is computed for every worker, whether or not it gets promoted.
    ledger_rows, ledger_skipped = read_ledger()
    prior_table = FIT.get(task_kind) or FIT["default"]
    fit_info: dict[str, dict[str, Any]] = {}
    fit_used: dict[str, float] = {}
    for name in WORKER_CLIS:
        prior = float(prior_table.get(name, 1.0))
        post, n_eff, used = fit_posterior(name, task_kind, now=now, rows=ledger_rows)
        fit_info[name] = {
            "prior": round(prior, 3),
            "posterior": round(post, 3),
            "n_eff": n_eff,
            "used": used,
        }
        fit_used[name] = post if used else prior

    # Filter first (eligibility, then the quota floor on admissible capacity),
    # rank second. The floor applies at every size: a tiny task on a worker
    # running on fumes still fails, it just fails cheaper. Admission never reads
    # pace, so a weekly window at 7% left cannot talk its way in by being early.
    rank_basis = "fit" if difficulty in ("hard", "frontier") else "headroom"

    def _keys(s: CliStatus) -> tuple:
        f, e = fit_used.get(s.cli, 1.0), eff_of(s)
        return (f, e) if rank_basis == "fit" else (e, f)

    survivors = [
        s
        for name in WORKER_CLIS
        for s in [by.get(name)]
        if s and s.available and s.eligible and adm_of(s) >= min_score
    ]
    relaxed = False
    if not survivors and difficulty in ("trivial", "routine"):
        # Cheap work may still take the best available worker below the floor.
        survivors = [
            s
            for name in WORKER_CLIS
            for s in [by.get(name)]
            if s and s.available and s.eligible
        ]
        relaxed = bool(survivors)

    ranked_statuses = sorted(survivors, key=_keys, reverse=True)

    claude = by.get("claude")
    local_labor = True
    if claude and claude.extras.get("local_labor") is False:
        local_labor = False
    if claude and any(
        _is_premium_window(w.name) and (w.used_pct or 0) >= 90 for w in claude.windows
    ):
        local_labor = False
    if claude and any(
        w.name == "5h_session" and (w.used_pct or 0) >= 95 for w in claude.windows
    ):
        local_labor = False

    primary = ranked_statuses[0].cli if ranked_statuses else None
    # A preferred CLI that survived the filter wins outright: parent intent
    # beats a marginal ranking difference, and no thumb on the scale is needed.
    if prefer and prefer in {s.cli for s in ranked_statuses}:
        primary = prefer
    fallbacks = [s.cli for s in ranked_statuses if s.cli != primary]

    reasons = []
    if primary:
        top = next(s for s in ranked_statuses if s.cli == primary)
        reasons.append(
            f"primary={primary} (headroom={top.score:.0f}, "
            f"effective={eff_of(top):.0f}, admission={adm_of(top):.0f}, "
            f"fit={fit_used.get(primary, 1.0):.2f}, "
            f"rank_basis={rank_basis}, kind={task_kind}, "
            f"size={task_size}, difficulty={difficulty}, "
            f"effort={_clamp_effort(primary, effort) or 'n/a'}, floor={min_score:.0f})"
        )
        if relaxed:
            reasons.append(
                f"floor {min_score:.0f} relaxed for difficulty={difficulty}: "
                "every worker is below it, so the best available one takes it"
            )
    else:
        if difficulty in ("hard", "frontier") and any(
            s.available and s.eligible for s in by.values() if s.cli in WORKER_CLIS
        ):
            reasons.append(
                f"quota floor {min_score:.0f} not met by any worker for "
                f"difficulty={difficulty}; not dispatching on fumes"
            )
        else:
            reasons.append("no eligible external worker, all exhausted or unavailable")
    if not local_labor:
        reasons.append(
            "local premium labor discouraged: keep the main session on supervision "
            "only; prefer a cheap in-session model or an external CLI worker"
        )
    for s in statuses:
        if s.cli in WORKER_CLIS and s.skip_reason:
            reasons.append(f"{s.cli}: {s.skip_reason}")
    for s in statuses:
        for w in s.windows:
            f_h = w.forecast_exhausts_in_hours
            if f_h is None or w.resets_in_hours is None:
                continue
            if f_h < float(w.resets_in_hours):
                reasons.append(
                    f"advisory: {s.cli} {w.name} exhausts in ~{f_h:.0f}h at current burn"
                )

    return {
        "primary_worker": primary,
        "fallback_workers": fallbacks,
        "local_labor_ok": local_labor,
        "task_size": task_size,
        "task_kind": task_kind,
        "difficulty": difficulty,
        "target_effort": effort,
        "min_score": round(min_score, 1),
        "cross_review_required": cross_review,
        "worker_args": {
            c: worker_args(c, difficulty, task_size) for c in WORKER_CLIS
        },
        "prefer": prefer or None,
        "rank_basis": rank_basis,
        "floor_relaxed": relaxed,
        "cooldowns": {
            c: {
                "reason": e["reason"],
                "minutes_left": e["minutes_left"],
                "until": round(float(e["until"]), 0),
            }
            for c, e in cools.items()
        },
        "fit": fit_info,
        "fit_ledger_skipped": ledger_skipped,
        "reasons": reasons,
        "ranked": [
            {
                "cli": s.cli,
                "score": round(s.score, 1),
                "adjusted_score": round(_keys(s)[0], 3),
                "effective_score": round(eff_of(s), 1),
                "admission_score": round(adm_of(s), 1),
                "fit": round(fit_used.get(s.cli, 1.0), 3),
                "rank_basis": rank_basis,
                "plan": s.plan,
            }
            for s in ranked_statuses
        ],
    }


# ---------------------------------------------------------------------------
# Probes
#
# The registry names a probe per worker; this is the table of probes that
# exist. A registered worker whose probe is not here is reported unavailable
# with a reason, never handed invented headroom, because a worker nobody can
# meter is exactly the one you must not route real work to.
# ---------------------------------------------------------------------------

PROBES = {
    "check_claude": check_claude,
    "check_codex": check_codex,
    "check_grok": check_grok,
    "check_kimi": check_kimi,
}

NO_PROBE = "no quota probe"


def _probe_for(cli: str):
    spec = REGISTRY.workers.get(cli)
    probe = PROBES.get(spec.probe) if spec else None
    if probe is not None:
        return probe

    def _missing_probe() -> CliStatus:
        want = spec.probe if spec else "?"
        return CliStatus(
            cli=cli,
            available=False,
            eligible=False,
            score=0.0,
            skip_reason=NO_PROBE,
            error="registry names probe %r, which this build does not implement" % want,
        )

    return _missing_probe


def _load_cache() -> Optional[dict]:
    try:
        if not CACHE_PATH.exists():
            return None
        data = json.loads(CACHE_PATH.read_text())
        if _now() - float(data.get("cached_at", 0)) > CACHE_TTL_SEC:
            return None
        return data
    except Exception:
        return None


def _save_cache(payload: dict) -> None:
    tmp_name = ""
    try:
        payload = dict(payload)
        payload["cached_at"] = _now()
        _CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(_CACHE_DIR), prefix="ai-cli-usage.", delete=False
        ) as fh:
            tmp_name = fh.name
            os.chmod(tmp_name, 0o600)
            json.dump(payload, fh, default=str)
        os.replace(tmp_name, CACHE_PATH)
        tmp_name = ""
    except Exception:
        pass
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _acquire_refresh_lock() -> bool:
    """Acquire the cache refresh lock, waiting briefly for another refresher."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_dir = _CACHE_DIR / "refresh.lock"
    try:
        os.mkdir(lock_dir, 0o700)
        return True
    except OSError:
        deadline = _now() + 10
        while _now() < deadline:
            if _load_cache() is not None:
                return False
            time.sleep(0.2)
        return False


def _release_refresh_lock() -> None:
    try:
        os.rmdir(_CACHE_DIR / "refresh.lock")
    except OSError:
        pass


def _statuses_from_cache(cached: dict) -> list[CliStatus]:
    statuses = []
    for c in cached.get("clis") or []:
        statuses.append(
            CliStatus(
                cli=c["cli"],
                available=c.get("available", False),
                plan=c.get("plan") or "",
                account=c.get("account") or "",
                windows=[Window(**w) for w in c.get("windows") or []],
                extras=c.get("extras") or {},
                error=c.get("error") or "",
                score=float(c.get("score") or 0),
                eligible=bool(c.get("eligible")),
                skip_reason=c.get("skip_reason") or "",
                effective_score=(
                    float(c["effective_score"])
                    if c.get("effective_score") is not None
                    else None
                ),
                admission_score=(
                    float(c["admission_score"])
                    if c.get("admission_score") is not None
                    else None
                ),
            )
        )
    return statuses


def redact_account(account: str) -> str:
    """Mask an account email. Public tool: never emit a full address by default."""
    if not account or "@" not in account:
        return account
    local, _, domain = account.partition("@")
    return (local[:2] + "***@" + domain) if len(local) > 2 else "***@" + domain


def _status_to_dict(s: CliStatus, include_account: bool = False) -> dict:
    d = asdict(s)
    if not include_account:
        d["account"] = redact_account(d.get("account") or "")
    return d


def _fmt_window(w: Window) -> str:
    parts = []
    if w.used is not None and w.limit is not None:
        parts.append(f"{w.used:g}/{w.limit:g} {w.unit}")
    if w.used_pct is not None:
        parts.append(f"{w.used_pct:.0f}% used")
    if w.remaining_pct is not None:
        parts.append(f"{w.remaining_pct:.0f}% left")
    if w.resets_in_hours is not None:
        if w.resets_in_hours < 0:
            parts.append("reset overdue")
        elif w.resets_in_hours < 48:
            parts.append(f"resets in {w.resets_in_hours:.1f}h")
        else:
            parts.append(f"resets in {w.resets_in_hours/24:.1f}d")
    if w.effective is not None:
        parts.append(f"eff={w.effective:.0f}")
    if w.admission is not None and w.admission != w.effective:
        parts.append(f"adm={w.admission:.0f}")
    if w.forecast_exhausts_in_hours is not None:
        parts.append(f"exhausts in ~{w.forecast_exhausts_in_hours:.0f}h")
    if w.note:
        parts.append(w.note)
    return f"{w.name}: " + ", ".join(parts) if parts else w.name


def print_human(
    statuses: list[CliStatus], rec: dict, include_account: bool = False
) -> None:
    print(f"AI CLI usage  ({_iso_local(_now())})")
    print("=" * 64)
    for s in statuses:
        flag = "OK" if s.available and s.eligible else ("SKIP" if s.available else "DOWN")
        eff = f"  eff={eff_of(s):.0f}" if s.effective_score is not None else ""
        adm = f"  adm={adm_of(s):.0f}" if s.admission_score is not None else ""
        print(
            f"\n[{flag}] {s.cli.upper()}  plan={s.plan or '?'}  "
            f"score={s.score:.0f}{eff}{adm}"
        )
        if s.account:
            acct = s.account if include_account else redact_account(s.account)
            print(f"  account: {acct}")
        if s.error:
            print(f"  error: {s.error}")
        if s.skip_reason:
            print(f"  skip: {s.skip_reason}")
        for w in s.windows:
            sev = w.severity.upper()
            print(f"  ({sev}) {_fmt_window(w)}")
        if s.extras:
            interesting = {
                k: v
                for k, v in s.extras.items()
                if k
                in (
                    "banked_resets",
                    "credits_balance",
                    "has_credits",
                    "limit_reached",
                    "local_labor",
                    "has_grok_code_access",
                    "parallel_limit",
                    "dispatch_note",
                    "rate_limit_tier",
                    "extra_usage",
                    "billing_period_end",
                )
            }
            if interesting:
                print(f"  extras: {json.dumps(interesting, default=str)}")

    print("\n" + "=" * 64)
    print("RECOMMENDATION")
    print(f"  primary_worker : {rec.get('primary_worker') or '(none)'}")
    print(f"  fallbacks      : {', '.join(rec.get('fallback_workers') or []) or '(none)'}")
    print(f"  local_labor_ok : {rec.get('local_labor_ok')}")
    prim = rec.get("primary_worker")
    if prim:
        wargs = (rec.get("worker_args") or {}).get(prim) or []
        print(
            f"  difficulty     : {rec.get('difficulty')} "
            f"(effort={rec.get('target_effort')}, floor={rec.get('min_score')})"
        )
        print(f"  worker_args    : {' '.join(wargs) or '(CLI defaults)'}")
    print(f"  rank_basis     : {rec.get('rank_basis')}")
    if rec.get("cross_review_required"):
        print("  cross_review   : REQUIRED (frontier difficulty)")
    for cli, info in sorted((rec.get("fit") or {}).items()):
        print(
            "  fit %-6s     : prior=%.2f posterior=%.2f n_eff=%.1f used=%s"
            % (
                cli,
                info.get("prior", 1.0),
                info.get("posterior", 1.0),
                info.get("n_eff", 0.0),
                "yes" if info.get("used") else "no (advisory)",
            )
        )
    for cli, cd in sorted((rec.get("cooldowns") or {}).items()):
        print(
            "  cooldown %-6s: %s, %d min left"
            % (cli, cd.get("reason"), cd.get("minutes_left", 0))
        )
    for r in rec.get("reasons") or []:
        print(f"  - {r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Check AI CLI remaining usage quotas")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--recommend", action="store_true", help="print primary worker only")
    ap.add_argument(
        "--cli",
        choices=cli_check_names(),
        default="all",
        help="which CLI to check",
    )
    ap.add_argument(
        "--task-size",
        choices=["tiny", "small", "medium", "large"],
        default="medium",
        help="affects minimum headroom for recommendation",
    )
    ap.add_argument(
        "--task-kind",
        choices=list(FIT.keys()),
        default="default",
        help="capability prior: impl|review|debug|best_of_n|analysis|default",
    )
    ap.add_argument(
        "--difficulty",
        choices=list(DIFFICULTY.keys()),
        default="routine",
        help=(
            "how hard the task is, independent of size: trivial|routine|hard|"
            "frontier. Sets worker reasoning effort and the quota floor."
        ),
    )
    ap.add_argument(
        "--prefer",
        default="",
        help="preferred worker if still eligible",
    )
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="bypass the 3-minute cache",
    )
    ap.add_argument(
        "--include-account",
        action="store_true",
        help="emit full account emails instead of redacting them",
    )
    ap.add_argument(
        "--cooldown",
        default="",
        metavar="CLI",
        help="mark a CLI ineligible for a while after a rate limit or auth failure",
    )
    ap.add_argument(
        "--cooldown-reason",
        choices=list(COOLDOWN_REASONS),
        default="rate-limit",
        help="why the CLI is on cooldown (default: rate-limit)",
    )
    ap.add_argument(
        "--cooldown-minutes",
        type=float,
        default=None,
        help="override the cooldown length (default: 15 rate-limit, 1440 auth)",
    )
    ap.add_argument(
        "--clear-cooldown",
        default="",
        metavar="CLI",
        help="lift an active cooldown for a CLI",
    )
    args = ap.parse_args()

    if args.clear_cooldown:
        cleared = clear_cooldown(args.clear_cooldown)
        print(
            "cooldown cleared: %s" % args.clear_cooldown
            if cleared
            else "no cooldown set for %s" % args.clear_cooldown
        )
        return 0
    if args.cooldown:
        entry = set_cooldown(
            args.cooldown, args.cooldown_reason, args.cooldown_minutes
        )
        print(
            "cooldown set: %s (%s) until %s"
            % (args.cooldown, entry["reason"], _iso_local(entry["until"]))
        )
        return 0

    checkers = {"claude": check_claude}
    for name in WORKER_CLIS:
        checkers[name] = _probe_for(name)
    names = list(checkers) if args.cli == "all" else [args.cli]

    statuses: list[CliStatus] = []
    from_cache = False
    if not args.fresh and args.cli == "all":
        cached = _load_cache()
        if cached and cached.get("clis"):
            try:
                statuses = _statuses_from_cache(cached)
                from_cache = True
            except Exception:
                statuses = []
                from_cache = False

    if not statuses:
        owns_refresh_lock = False
        if args.cli == "all":
            try:
                owns_refresh_lock = _acquire_refresh_lock()
                if not owns_refresh_lock:
                    cached = _load_cache()
                    if cached and cached.get("clis"):
                        statuses = _statuses_from_cache(cached)
                        from_cache = True
            except Exception:
                owns_refresh_lock = False
        try:
            if not statuses:
                for name in names:
                    try:
                        statuses.append(checkers[name]())
                    except Exception as e:
                        statuses.append(
                            CliStatus(
                                cli=name,
                                available=False,
                                error=f"checker crashed: {e}",
                            )
                        )
                for s in statuses:
                    apply_effective(s)
                # Only a real fetch is a new observation; a cache read is not.
                record_usage_history(statuses)
                if args.cli == "all":
                    _save_cache(
                        {
                            "clis": [_status_to_dict(s) for s in statuses],
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
        finally:
            if owns_refresh_lock:
                _release_refresh_lock()

    for s in statuses:
        apply_effective(s)
    annotate_forecasts(statuses)

    rec = recommend(
        statuses,
        task_size=args.task_size,
        task_kind=args.task_kind,
        prefer=(args.prefer or "").lower(),
        difficulty=args.difficulty,
    )
    rec["from_cache"] = from_cache

    if args.recommend:
        print(rec.get("primary_worker") or "none")
        return 0 if rec.get("primary_worker") else 1

    if args.json:
        out = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "from_cache": from_cache,
            "clis": [
                _status_to_dict(s, include_account=args.include_account)
                for s in statuses
            ],
            "recommendation": rec,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        if from_cache:
            print(f"(cached ≤{CACHE_TTL_SEC}s — pass --fresh to recheck)\n")
        print_human(statuses, rec, include_account=args.include_account)

    if not any(s.available for s in statuses):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
