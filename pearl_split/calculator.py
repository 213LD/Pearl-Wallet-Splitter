"""Pure calculation logic: worker classification, share deltas, exact split, and custom fee pipelines."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

VALID_FEE_CATEGORIES = ("transfer", "trade", "withdraw", "fx", "payout", "other")

DEFAULT_FEE_PIPELINE_STEPS: list[dict[str, Any]] = [
    {
        "id": "wallet_to_exchange",
        "name": "官方钱包转交易所",
        "category": "transfer",
        "fee_currency": "PRL",
        "only_official_wallet": True,
        "default_fee": 0.0,
        "enabled": True,
        "notes": "仅官方钱包转入交易所时产生；直充交易所钱包将自动免除此项",
    },
    {
        "id": "exchange_trade",
        "name": "交易所卖币手续费",
        "category": "trade",
        "fee_currency": "USDT",
        "only_official_wallet": False,
        "default_fee": 0.0,
        "enabled": True,
        "notes": "交易所卖出 PRL 的成交手续费",
    },
    {
        "id": "payout_withdraw",
        "name": "提现 / 出金手续费",
        "category": "withdraw",
        "fee_currency": "CNY",
        "only_official_wallet": False,
        "default_fee": 0.0,
        "enabled": True,
        "notes": "C2C / 银行卡 / 收款平台出金手续费",
    },
]

PIPELINE_PRESETS: dict[str, dict[str, Any]] = {
    "simple_direct": {
        "key": "simple_direct",
        "label": "精简直卖流程（推荐）",
        "description": "官方/交易所钱包 → 交易所卖币 → 法币提现出金（3 个常用环节）",
        "steps": DEFAULT_FEE_PIPELINE_STEPS,
    },
    "multi_hop": {
        "key": "multi_hop",
        "label": "多段换汇出金流程",
        "description": "适用于跨平台转 USDT、换汇外币后再跨境转回国内人民币（6 个环节，币种可改）",
        "steps": [
            {
                "id": "wallet_to_exchange",
                "name": "官方钱包转交易所",
                "category": "transfer",
                "fee_currency": "PRL",
                "only_official_wallet": True,
                "default_fee": 0.0,
                "enabled": True,
                "notes": "官方钱包转币上链手续费，交易所直充钱包自动免除",
            },
            {
                "id": "spot_trade_fee",
                "name": "交易所卖 PRL→USDT 手续费",
                "category": "trade",
                "fee_currency": "USDT",
                "only_official_wallet": False,
                "default_fee": 0.0,
                "enabled": True,
                "notes": "",
            },
            {
                "id": "exchange_withdraw_fee",
                "name": "交易所提币转账手续费",
                "category": "withdraw",
                "fee_currency": "USDT",
                "only_official_wallet": False,
                "default_fee": 0.0,
                "enabled": True,
                "notes": "跨平台提现 USDT/币种手续费",
            },
            {
                "id": "fiat_trade_fee",
                "name": "第二段卖 USDT→法币手续费",
                "category": "trade",
                "fee_currency": "USD",
                "only_official_wallet": False,
                "default_fee": 0.0,
                "enabled": True,
                "notes": "可按需要改成 EUR/USD/HKD 等法币",
            },
            {
                "id": "fx_convert_fee",
                "name": "换汇 / 中转手续费",
                "category": "fx",
                "fee_currency": "USD",
                "only_official_wallet": False,
                "default_fee": 0.0,
                "enabled": True,
                "notes": "跨境购汇或账户中转手续费",
            },
            {
                "id": "payout_fee",
                "name": "国内银行卡 / 支付出金费",
                "category": "payout",
                "fee_currency": "CNY",
                "only_official_wallet": False,
                "default_fee": 0.0,
                "enabled": True,
                "notes": "",
            },
        ],
    },
    "blank": {
        "key": "blank",
        "label": "极简 / 完全自定义",
        "description": "仅保留可选钱包转出步骤，其余所有交易、换汇、提现环节全部自行添加",
        "steps": [
            {
                "id": "wallet_to_exchange",
                "name": "官方钱包转交易所",
                "category": "transfer",
                "fee_currency": "PRL",
                "only_official_wallet": True,
                "default_fee": 0.0,
                "enabled": True,
                "notes": "仅官方钱包可填，不需要时可关闭或删除",
            },
        ],
    },
}


def get_pipeline_presets_meta() -> list[dict[str, Any]]:
    """Return user-facing metadata + step definitions for built-in fee presets."""
    out: list[dict[str, Any]] = []
    for key in ("simple_direct", "multi_hop", "blank"):
        p = PIPELINE_PRESETS[key]
        out.append(
            {
                "key": p["key"],
                "label": p["label"],
                "description": p["description"],
                "steps": normalize_pipeline_steps(p["steps"]),
            }
        )
    return out


@dataclass(frozen=True)
class MemberInfo:
    id: int
    name: str
    color: str


@dataclass(frozen=True)
class PrefixRule:
    prefix: str
    member_id: int


@dataclass(frozen=True)
class WorkerBinding:
    worker_name: str
    member_id: int
    wallet_id: int | None = None


def suggest_prefix(worker_name: str) -> str:
    """Heuristically suggest a member prefix from an unknown worker name."""
    name = (worker_name or "").strip()
    if not name:
        return ""
    for sep in ("_", "-", "."):
        if sep in name:
            head = name.split(sep, 1)[0].strip()
            if head:
                return head
    m = re.match(r"^([A-Za-z0-9]+?)(?:\d+|main|itx|rig\d*)$", name, re.IGNORECASE)
    if m and len(m.group(1)) >= 2:
        return m.group(1)
    m2 = re.match(r"^([A-Za-z]+)", name)
    if m2 and len(m2.group(1)) >= 2:
        return m2.group(1)
    return name[: min(4, len(name))]


def classify_worker(
    worker_name: str,
    members_by_id: dict[int, MemberInfo],
    prefixes: list[PrefixRule],
    bindings: list[WorkerBinding],
    wallet_id: int | None = None,
) -> tuple[MemberInfo | None, str]:
    """Classify a worker into a member.

    Priority:
    1. Exact manual binding scoped to ``wallet_id``.
    2. Global exact manual binding (``wallet_id is None``).
    3. Longest matching prefix (case-insensitive).
    4. Unassigned (``None``, ``"unassigned"``).
    """
    norm_name = (worker_name or "").strip().lower()

    if wallet_id is not None:
        for b in bindings:
            if b.wallet_id == wallet_id and b.worker_name.strip().lower() == norm_name:
                if b.member_id in members_by_id:
                    return members_by_id[b.member_id], "manual_wallet"

    for b in bindings:
        if b.wallet_id is None and b.worker_name.strip().lower() == norm_name:
            if b.member_id in members_by_id:
                return members_by_id[b.member_id], "manual_global"

    sorted_prefixes = sorted(prefixes, key=lambda r: len(r.prefix.strip()), reverse=True)
    for rule in sorted_prefixes:
        p = rule.prefix.strip().lower()
        if p and norm_name.startswith(p):
            if rule.member_id in members_by_id:
                return members_by_id[rule.member_id], f"prefix:{rule.prefix}"

    return None, "unassigned"


def compute_worker_delta(
    prev_shares: int,
    curr_shares: int,
    treat_reset_as_curr: bool = False,
) -> tuple[int, bool]:
    """Compute share delta between two checkpoints.

    Returns ``(delta_shares, reset_detected)``.
    If ``curr_shares < prev_shares``, ``reset_detected`` is ``True``:
    - when ``treat_reset_as_curr`` is True, returns ``curr_shares``
    - otherwise defaults safely to ``0`` so accidental negative/stale inputs never corrupt ratios.
    """
    prev_val = max(0, int(prev_shares))
    curr_val = max(0, int(curr_shares))
    if curr_val < prev_val:
        return (curr_val if treat_reset_as_curr else 0), True
    return curr_val - prev_val, False


def allocate_exact_amounts(
    member_deltas: list[dict[str, Any]],
    total_cny: float,
    total_prl: float = 0.0,
) -> list[dict[str, Any]]:
    """Allocate exact CNY (to 0.01 CNY via Hamilton largest-remainder) and PRL by share delta."""
    total_delta = sum(max(0, int(item.get("delta_shares", 0))) for item in member_deltas)
    total_cny_clean = max(0.0, float(total_cny or 0.0))
    total_prl_clean = max(0.0, float(total_prl or 0.0))
    total_cents = int(round(total_cny_clean * 100))

    results: list[dict[str, Any]] = []
    if total_delta <= 0:
        for item in member_deltas:
            results.append(
                {
                    **item,
                    "delta_shares": max(0, int(item.get("delta_shares", 0))),
                    "ratio": 0.0,
                    "cny_earned": 0.0,
                    "prl_earned": 0.0,
                }
            )
        return results

    remainders: list[tuple[int, int, int]] = []
    floor_cents: list[int] = []

    for idx, item in enumerate(member_deltas):
        d = max(0, int(item.get("delta_shares", 0)))
        numerator = total_cents * d
        fc = numerator // total_delta
        rem = numerator % total_delta
        floor_cents.append(fc)
        remainders.append((rem, d, idx))

    leftover = total_cents - sum(floor_cents)
    if leftover > 0:
        remainders.sort(key=lambda x: (x[0], x[1], -x[2]), reverse=True)
        for k in range(min(leftover, len(remainders))):
            floor_cents[remainders[k][2]] += 1

    total_micro_prl = int(round(total_prl_clean * 1_000_000))
    floor_prl: list[int] = []
    prl_rem: list[tuple[int, int, int]] = []
    for idx, item in enumerate(member_deltas):
        d = max(0, int(item.get("delta_shares", 0)))
        num = total_micro_prl * d
        fp = num // total_delta
        rm = num % total_delta
        floor_prl.append(fp)
        prl_rem.append((rm, d, idx))
    leftover_prl = total_micro_prl - sum(floor_prl)
    if leftover_prl > 0:
        prl_rem.sort(key=lambda x: (x[0], x[1], -x[2]), reverse=True)
        for k in range(min(leftover_prl, len(prl_rem))):
            floor_prl[prl_rem[k][2]] += 1

    for idx, item in enumerate(member_deltas):
        d = max(0, int(item.get("delta_shares", 0)))
        results.append(
            {
                **item,
                "delta_shares": d,
                "ratio": d / total_delta,
                "cny_earned": round(floor_cents[idx] / 100.0, 2),
                "prl_earned": round(floor_prl[idx] / 1_000_000.0, 6),
            }
        )

    return results


def _nonneg_float(val: Any) -> float:
    try:
        num = float(val or 0.0)
        return num if num > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _signed_float(val: Any) -> float:
    try:
        return float(val or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _clean_asset_code(raw: Any, default: str = "USDT") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").strip().upper())[:10]
    return cleaned or default


def normalize_wallet_type(wallet_type: Any) -> str:
    raw = str(wallet_type or "").strip().lower()
    return "exchange" if raw == "exchange" else "official"


def normalize_pipeline_steps(raw_steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Sanitize a list of user-defined fee pipeline step templates."""
    if not isinstance(raw_steps, list):
        raw_steps = []
    used_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:80]
        if not name:
            continue
        raw_id = re.sub(r"[^a-z0-9_-]", "_", str(item.get("id") or "").strip().lower())
        raw_id = re.sub(r"_+", "_", raw_id).strip("_") or f"step_{idx + 1}"
        sid = raw_id
        suffix = 2
        while sid in used_ids:
            sid = f"{raw_id}_{suffix}"
            suffix += 1
        used_ids.add(sid)

        cat = str(item.get("category") or "other").strip().lower()
        if cat not in VALID_FEE_CATEGORIES:
            cat = "other"

        normalized.append(
            {
                "id": sid,
                "name": name,
                "category": cat,
                "fee_currency": _clean_asset_code(item.get("fee_currency"), default="USDT"),
                "only_official_wallet": bool(item.get("only_official_wallet", False)),
                "default_fee": round(_nonneg_float(item.get("default_fee")), 6),
                "enabled": bool(item.get("enabled", True)),
                "notes": str(item.get("notes") or "").strip()[:160],
            }
        )
    return normalized


def normalize_fee_payload(raw: dict[str, Any] | None, wallet_type: str = "official") -> dict[str, Any]:
    """Sanitize customizable settlement fees, conversions, FX rates, and multi-asset residual deltas."""
    src = raw if isinstance(raw, dict) else {}
    wtype = normalize_wallet_type(wallet_type or src.get("wallet_type"))

    fee_entries: list[dict[str, Any]] = []
    for idx, item in enumerate(src.get("fee_entries") or []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()[:80]
        if not label:
            continue
        only_off = bool(item.get("only_official_wallet", False))
        amt = 0.0 if (only_off and wtype == "exchange") else round(_nonneg_float(item.get("amount")), 6)
        if amt <= 0:
            continue
        cat = str(item.get("category") or "other").strip().lower()
        if cat not in VALID_FEE_CATEGORIES:
            cat = "other"
        fee_entries.append(
            {
                "step_id": str(item.get("step_id") or item.get("id") or f"custom_{idx + 1}").strip()[:64],
                "label": label,
                "category": cat,
                "amount": amt,
                "currency": _clean_asset_code(item.get("currency") or item.get("fee_currency"), default="USDT"),
                "only_official_wallet": only_off,
            }
        )

    conversions: list[dict[str, Any]] = []
    for idx, cv in enumerate(src.get("conversions") or []):
        if not isinstance(cv, dict):
            continue
        from_asset = _clean_asset_code(cv.get("from_asset"), default="PRL")
        to_asset = _clean_asset_code(cv.get("to_asset"), default="USDT")
        sold = round(_nonneg_float(cv.get("sold_amount")), 6)
        price = round(_nonneg_float(cv.get("unit_price")), 6)
        rec = round(_nonneg_float(cv.get("received_amount")), 6)
        mkt = round(_nonneg_float(cv.get("market_ref_rate")), 6)
        label = str(cv.get("label") or f"{from_asset} → {to_asset}").strip()[:80]
        if sold <= 0 and rec <= 0 and price <= 0 and mkt <= 0:
            continue
        if price <= 0 and sold > 0 and rec > 0:
            price = round(rec / sold, 6)
        if rec <= 0 and sold > 0 and price > 0:
            rec = round(sold * price, 6)
        conversions.append(
            {
                "label": label or f"{from_asset} → {to_asset}",
                "from_asset": from_asset,
                "to_asset": to_asset,
                "sold_amount": sold,
                "unit_price": price,
                "received_amount": rec,
                "market_ref_rate": mkt,
            }
        )

    fx_rates_cny: dict[str, float] = {}
    raw_fx = src.get("fx_rates_cny")
    if isinstance(raw_fx, dict):
        for k, v in raw_fx.items():
            code = _clean_asset_code(k, default="")
            rate = round(_nonneg_float(v), 6)
            if code and code != "CNY" and rate > 0:
                fx_rates_cny[code] = rate

    residual_deltas: dict[str, float] = {}
    raw_res = src.get("residual_deltas")
    if isinstance(raw_res, dict):
        for k, v in raw_res.items():
            code = _clean_asset_code(k, default="")
            delta = round(_signed_float(v), 6)
            if code and abs(delta) > 1e-8:
                residual_deltas[code] = delta

    raw_trade_ids = src.get("linked_trade_ids") or []
    clean_trade_ids: list[int] = []
    if isinstance(raw_trade_ids, list):
        seen_ids: set[int] = set()
        for tid in raw_trade_ids:
            try:
                ival = int(tid)
                if ival > 0 and ival not in seen_ids:
                    seen_ids.add(ival)
                    clean_trade_ids.append(ival)
            except (TypeError, ValueError):
                continue

    res_src = str(src.get("residual_source") or "direct").strip().lower()
    if res_src not in ("direct", "trades"):
        res_src = "direct"

    return {
        "schema_version": 2,
        "wallet_type": wtype,
        "fee_entries": fee_entries,
        "conversions": conversions,
        "fx_rates_cny": fx_rates_cny,
        "residual_deltas": residual_deltas,
        "residual_source": res_src,
        "linked_trade_ids": sorted(clean_trade_ids),
    }


def _propagate_conversion_rates(
    rate_map: dict[str, float],
    conversions: list[dict[str, Any]],
) -> None:
    """Propagate CNY valuation across any multi-hop asset conversion chain."""
    for _ in range(max(1, len(conversions) + 1)):
        changed = False
        for cv in conversions:
            fa = cv["from_asset"]
            ta = cv["to_asset"]
            price = float(cv.get("unit_price") or 0.0)
            if price <= 0:
                continue
            if fa not in rate_map and ta in rate_map and rate_map[ta] > 0:
                rate_map[fa] = price * rate_map[ta]
                changed = True
            elif ta not in rate_map and fa in rate_map and rate_map[fa] > 0:
                rate_map[ta] = rate_map[fa] / price
                changed = True
        if not changed:
            break


def summarize_fees_and_rates(
    fees: dict[str, Any] | None,
    cny_amount: float = 0.0,
    prl_amount: float = 0.0,
    prl_ref_cny: float = 0.0,
) -> dict[str, Any]:
    """Compute dynamic multi-currency fee breakdown, implied conversion rates, FX spread loss, and loss ratio."""
    f = normalize_fee_payload(fees, wallet_type=str((fees or {}).get("wallet_type") or "official"))
    cny = _nonneg_float(cny_amount)
    prl = _nonneg_float(prl_amount)

    rate_map: dict[str, float] = {"CNY": 1.0}
    for asset, r in f["fx_rates_cny"].items():
        if r > 0:
            rate_map[asset] = float(r)

    conversions = f["conversions"]
    _propagate_conversion_rates(rate_map, conversions)

    if "PRL" not in rate_map:
        if cny > 0 and prl > 0:
            rate_map["PRL"] = cny / prl
        elif prl_ref_cny > 0:
            rate_map["PRL"] = float(prl_ref_cny)
        _propagate_conversion_rates(rate_map, conversions)

    totals_by_currency: dict[str, float] = {}
    fee_items: list[dict[str, Any]] = []
    estimated_fee_cny = 0.0
    unconverted_currencies: list[str] = []

    for entry in f["fee_entries"]:
        amt = float(entry["amount"])
        if amt <= 0:
            continue
        cur = entry["currency"]
        totals_by_currency[cur] = round(totals_by_currency.get(cur, 0.0) + amt, 6)
        r_cny = rate_map.get(cur, 0.0)
        cny_eq = round(amt * r_cny, 2) if r_cny > 0 else None
        if cny_eq is not None:
            estimated_fee_cny += cny_eq
        elif cur not in unconverted_currencies:
            unconverted_currencies.append(cur)
        fee_items.append(
            {
                "step_id": entry["step_id"],
                "label": entry["label"],
                "category": entry["category"],
                "amount": amt,
                "unit": cur,
                "cny_est": cny_eq,
            }
        )

    estimated_fee_cny = round(estimated_fee_cny, 2)

    fx_spread_loss_cny = 0.0
    for cv in conversions:
        mkt = float(cv.get("market_ref_rate") or 0.0)
        price = float(cv.get("unit_price") or 0.0)
        sold = float(cv.get("sold_amount") or 0.0)
        ta = cv["to_asset"]
        r_to_cny = rate_map.get(ta, 0.0)
        if mkt > price > 0 and sold > 0 and r_to_cny > 0:
            fx_spread_loss_cny += sold * (mkt - price) * r_to_cny
    fx_spread_loss_cny = round(fx_spread_loss_cny, 2)

    total_loss_cny = round(estimated_fee_cny + fx_spread_loss_cny, 2)
    gross_cny_est = round(cny + total_loss_cny, 2) if (cny > 0 and total_loss_cny > 0) else cny
    loss_rate_pct = (
        round((total_loss_cny / gross_cny_est) * 100.0, 2)
        if (gross_cny_est > 0 and total_loss_cny > 0)
        else 0.0
    )
    net_cny_per_prl = round(cny / prl, 4) if (cny > 0 and prl > 0) else 0.0

    rounded_rates = {k: round(v, 6 if v < 1 else 4) for k, v in rate_map.items() if k != "CNY" and v > 0}
    has_any = bool(
        fee_items
        or conversions
        or f["fx_rates_cny"]
        or f["residual_deltas"]
        or f["linked_trade_ids"]
    )

    return {
        "has_any": has_any,
        "fees": f,
        "fee_items": fee_items,
        "totals_by_currency": totals_by_currency,
        "estimated_fee_cny": estimated_fee_cny,
        "fx_spread_loss_cny": fx_spread_loss_cny,
        "total_loss_cny": total_loss_cny,
        "unconverted_currencies": unconverted_currencies,
        "loss_rate_pct": loss_rate_pct,
        "net_cny_per_prl": net_cny_per_prl,
        "rate_map": rounded_rates,
        "conversions": conversions,
        "residual_deltas": f["residual_deltas"],
    }
