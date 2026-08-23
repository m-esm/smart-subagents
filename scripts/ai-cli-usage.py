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

HOME = Path.home()
TIMEOUT = 15


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
    if pcode == 200 and isinstance(profile, dict):
        acc = profile.get("account") or {}
        org = profile.get("organization") or {}
        st.account = str(acc.get("email") or "")
        st.plan = str(org.get("organization_type") or st.plan or org.get("rate_limit_tier") or "")
        st.extras["subscription_status"] = org.get("subscription_status")
        st.extras["rate_limit_tier"] = org.get("rate_limit_tier") or st.extras.get(
            "rate_limit_tier"
        )

    st.available = True

    def add_window(name: str, block: Optional[dict], unit: str = "pct") -> None:
        if not block:
            return
        util = block.get("utilization")
        # Claude oauth usage: five_hour=1.0 means 1%, seven_day=63.0 means 63%.
        used_pct = float(util) if util is not None else None
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
            )
        )

    add_window("5h_session", data.get("five_hour"))
    add_window("weekly_all", data.get("seven_day"))
    add_window("weekly_opus", data.get("seven_day_opus"))
    add_window("weekly_sonnet", data.get("seven_day_sonnet"))

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
        pct = lim.get("percent")
        if pct is None:
            continue
        used_pct = float(pct)
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
    worst = max((w.used_pct or 0) for w in binding) if binding else 0
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

    st.available = True
    st.plan = str(data.get("plan_type") or st.plan)
    st.account = str(data.get("email") or st.account)

    rl = data.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    used_pct = pw.get("used_percent")
    if used_pct is not None:
        used_pct = float(used_pct)
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
    )
    st.windows.append(w)

    if rl.get("secondary_window"):
        sw = rl["secondary_window"]
        used2 = sw.get("used_percent")
        if used2 is not None:
            used2 = float(used2)
        st.windows.append(
            Window(
                name="secondary_window",
                used_pct=used2,
                remaining_pct=(100.0 - used2) if used2 is not None else None,
                severity=_severity(used2),
            )
        )

    credits = data.get("credits") or {}
    resets = data.get("rate_limit_reset_credits") or {}
    st.extras["credits_balance"] = credits.get("balance")
    st.extras["has_credits"] = credits.get("has_credits")
    st.extras["banked_resets"] = resets.get("available_count") or 0
    st.extras["limit_reached"] = bool(rl.get("limit_reached"))
    st.extras["allowed"] = rl.get("allowed")

    used = used_pct if used_pct is not None else 100.0
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

    cfg = billing.get("config") or billing
    limit_v = ((cfg.get("monthlyLimit") or {}).get("val"))
    used_v = ((cfg.get("used") or {}).get("val"))
    on_demand = ((cfg.get("onDemandCap") or {}).get("val"))
    period_start = cfg.get("billingPeriodStart")
    period_end = cfg.get("billingPeriodEnd")

    st.available = True
    if limit_v is not None and used_v is not None:
        limit_f = float(limit_v)
        used_f = float(used_v)
        rem = max(0.0, limit_f - used_f)
        used_pct = (used_f / limit_f * 100.0) if limit_f > 0 else 0.0
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
            )
        )
        st.score = max(0.0, 100.0 - used_pct)
        st.eligible = used_pct < 99.5
        if not st.eligible:
            st.skip_reason = "Grok monthly CLI credits exhausted"
    else:
        st.score = 50.0
        st.eligible = True

    ucode, user = _http_json("https://cli-chat-proxy.grok.com/v1/user", headers)
    if ucode == 200 and isinstance(user, dict):
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

    st.available = True
    user = data.get("user") or {}
    membership = user.get("membership") or {}
    st.extras["membership_level"] = membership.get("level")
    st.extras["region"] = user.get("region")
    st.extras["sub_type"] = data.get("subType")

    mcode, me = _http_json("https://api.kimi.com/coding/v1/me", headers)
    if mcode == 200 and isinstance(me, dict):
        st.account = str(me.get("nickname") or me.get("user_id") or "")
        st.plan = str(me.get("user_level_name") or me.get("user_level") or "")
    else:
        st.plan = str(membership.get("level") or "")

    usage = data.get("usage") or {}
    try:
        limit_f = float(usage.get("limit")) if usage.get("limit") is not None else None
        used_f = float(usage.get("used")) if usage.get("used") is not None else None
        rem_f = float(usage.get("remaining")) if usage.get("remaining") is not None else None
    except (TypeError, ValueError):
        limit_f = used_f = rem_f = None

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
        )
    )

    for lim in data.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        window = lim.get("window") or {}
        detail = lim.get("detail") or {}
        dur = window.get("duration")
        unit = str(window.get("timeUnit") or "")
        # TIME_UNIT_MINUTE + duration 300 ≈ 5h throughput window
        if unit.endswith("MINUTE") and dur is not None:
            name = f"throughput_{dur}m"
        elif unit.endswith("HOUR") and dur is not None:
            name = f"throughput_{dur}h"
        else:
            name = f"window_{dur}_{unit}".lower().replace("time_unit_", "")
        try:
            d_limit = float(detail["limit"]) if detail.get("limit") is not None else None
            d_rem = float(detail["remaining"]) if detail.get("remaining") is not None else None
        except (TypeError, ValueError, KeyError):
            d_limit = d_rem = None
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
            )
        )

    parallel = data.get("parallel") or {}
    if parallel.get("limit") is not None:
        st.extras["parallel_limit"] = parallel.get("limit")

    used = used_pct if used_pct is not None else 0.0
    st.score = max(0.0, 100.0 - used)
    st.eligible = used < 99.5
    if not st.eligible:
        st.skip_reason = "Kimi weekly quota exhausted"
    # Safety note for supervisor (not a skip)
    st.extras["sandbox"] = False
    st.extras["dispatch_note"] = "no sandbox — worktree only"

    return st


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

WORKER_CLIS = ("codex", "grok", "kimi")

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


# Capability priors by task kind (multiplied into headroom score, not hard gates).
# Higher = better default fit when quotas are comparable.
FIT = {
    "default": {"codex": 1.05, "grok": 1.0, "kimi": 0.9},
    "impl": {"codex": 1.12, "grok": 1.05, "kimi": 0.95},
    "review": {"codex": 1.15, "grok": 1.0, "kimi": 1.05},  # kimi good third opinion
    "debug": {"codex": 1.1, "grok": 1.05, "kimi": 0.95},
    "best_of_n": {"codex": 0.85, "grok": 1.2, "kimi": 0.8},
    "analysis": {"codex": 1.05, "grok": 1.0, "kimi": 0.95},
}


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

# Effort rungs each CLI actually accepts, weakest first. Kept per-CLI rather
# than assumed uniform: grok enumerates xhigh, codex does not document it on
# every version, and kimi has no effort flag at all. A target above a CLI's
# ceiling clamps to its top rung.
EFFORT_LADDER: dict[str, list[str]] = {
    "codex": ["low", "medium", "high"],
    "grok": ["low", "medium", "high", "xhigh"],
    "kimi": [],
}


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
    consumes this verbatim instead of reimplementing the mapping.
    """
    effort, _, _ = DIFFICULTY.get(difficulty) or DIFFICULTY["routine"]
    rung = _clamp_effort(cli, effort)
    if cli == "codex":
        return ["-c", f"model_reasoning_effort={rung}"] if rung else []
    if cli == "grok":
        return ["--reasoning-effort", rung] if rung else []
    if cli == "kimi":
        # No effort flag; the only lever is a faster model alias.
        cheap = difficulty == "trivial" or (
            task_size in ("tiny", "small") and difficulty in ("trivial", "routine")
        )
        return ["-m", "kimi-for-coding-highspeed"] if cheap else []
    return []


def recommend(
    statuses: list[CliStatus],
    task_size: str = "medium",
    task_kind: str = "default",
    prefer: str = "",
    difficulty: str = "routine",
) -> dict[str, Any]:
    """Rank external worker CLIs for labor. Claude is supervisor, not a worker pick."""
    by = {s.cli: s for s in statuses}
    fit = FIT.get(task_kind) or FIT["default"]
    if difficulty not in DIFFICULTY:
        difficulty = "routine"
    effort, floor_mult, cross_review = DIFFICULTY[difficulty]
    # Harder work needs more headroom: retries are likelier and cost more per turn.
    base_floor = {"tiny": 5, "small": 15, "medium": 25, "large": 40}.get(task_size, 25)
    min_score = min(90.0, base_floor * floor_mult)

    candidates: list[tuple[float, CliStatus]] = []
    for name in WORKER_CLIS:
        s = by.get(name)
        if not s or not s.available or not s.eligible:
            continue
        if s.score < min_score and task_size in ("medium", "large"):
            # Still allow if it is the only option later; skip for ranking first pass
            continue
        adj = s.score * float(fit.get(name, 1.0))
        # Prefer sticky parent choice lightly when eligible
        if prefer and prefer == name:
            adj *= 1.08
        candidates.append((adj, s))

    # Low difficulty work may still use the best available eligible worker.
    if not candidates and difficulty in ("trivial", "routine"):
        for name in WORKER_CLIS:
            s = by.get(name)
            if s and s.available and s.eligible:
                adj = s.score * float(fit.get(name, 1.0))
                if prefer and prefer == name:
                    adj *= 1.08
                candidates.append((adj, s))

    candidates.sort(key=lambda t: t[0], reverse=True)
    ranked_statuses = [s for _, s in candidates]

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
    # Honor prefer when eligible even if not top score (parent intent)
    if prefer and prefer in {s.cli for s in ranked_statuses}:
        primary = prefer
    fallbacks = [s.cli for s in ranked_statuses if s.cli != primary]

    reasons = []
    if primary:
        top = next(s for s in ranked_statuses if s.cli == primary)
        reasons.append(
            f"primary={primary} (headroom={top.score:.0f}, kind={task_kind}, "
            f"size={task_size}, difficulty={difficulty}, "
            f"effort={_clamp_effort(primary, effort) or 'n/a'}, floor={min_score:.0f})"
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
        "reasons": reasons,
        "ranked": [
            {
                "cli": s.cli,
                "score": round(s.score, 1),
                "adjusted_score": round(next(a for a, x in candidates if x.cli == s.cli), 1),
                "plan": s.plan,
            }
            for s in ranked_statuses
        ],
    }


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
        print(f"\n[{flag}] {s.cli.upper()}  plan={s.plan or '?'}  score={s.score:.0f}")
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
    if rec.get("cross_review_required"):
        print("  cross_review   : REQUIRED (frontier difficulty)")
    for r in rec.get("reasons") or []:
        print(f"  - {r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Check AI CLI remaining usage quotas")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--recommend", action="store_true", help="print primary worker only")
    ap.add_argument(
        "--cli",
        choices=["claude", "codex", "grok", "kimi", "all"],
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
        help="preferred worker if still eligible (codex|grok|kimi)",
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
    args = ap.parse_args()

    checkers = {
        "claude": check_claude,
        "codex": check_codex,
        "grok": check_grok,
        "kimi": check_kimi,
    }
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
