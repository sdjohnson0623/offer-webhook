"""Offer engine for the BBC lead voice agent.

Pure math, no I/O. The Retell agent calls the webhook (server.py) mid-call;
this module does the numbers. The agent never invents an ARV and never
offers above max_offer returned here.

Formulas:
- HMHW wholesale: Max Offer = ARV * (1 - profit_margin) - repairs - wholesale_fee
  (Shawn's worked example: 80,000 ARV, 30% margin, 5,000 repairs, 20,000 fee -> 31,000)
- Rental screen: NOI = gross_rent * (1 - vacancy) - tax - insurance
                 - gross_rent * maint_mgmt_pct
- Creative (subject-to): equity_gap = asking - mortgage_balance; the structure
  is take over the existing loan + cash_to_seller for part of the equity.
"""

from __future__ import annotations

DEFAULTS = {
    "profit_margin": 0.30,
    "wholesale_fee": 10000,
    "vacancy": 0.07,
    "insurance_annual": 1800,
    "maint_mgmt_pct": 0.15,
    "target_cap": 0.12,        # rental-hold pricing target
    "target_coc": 0.20,        # Shawn's cash-on-cash bar
    "loan_rate": 0.075,        # assumption for NEW financing scenarios
    "loan_years": 30,
    "down_pct": 0.20,
    "closing_pct": 0.03,
    "cash_to_seller_pct_of_equity": 0.35,  # opening creative structure
}


def _pmt(principal: float, annual_rate: float, years: int = 30) -> float:
    r = annual_rate / 12.0
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def max_cash_offer(arv, repairs, profit_margin=DEFAULTS["profit_margin"],
                   wholesale_fee=DEFAULTS["wholesale_fee"]):
    """HMHW formula. Returns a rounded dollar figure, floored at 0."""
    offer = arv * (1 - profit_margin) - repairs - wholesale_fee
    return max(0, round(offer))


def noi(monthly_rent, annual_tax, insurance_annual=DEFAULTS["insurance_annual"],
        vacancy=DEFAULTS["vacancy"], maint_mgmt_pct=DEFAULTS["maint_mgmt_pct"]):
    gross = monthly_rent * 12
    return gross * (1 - vacancy) - annual_tax - insurance_annual - gross * maint_mgmt_pct


def rental_prices(monthly_rent, annual_tax, cfg=DEFAULTS, repairs=0.0):
    """Prices at which the rental math hits Shawn's targets.

    Repairs are cash the buyer pays on top of the purchase price, so each
    supported price is reduced dollar-for-dollar by the repair estimate
    (floored at 0): the most he can PAY for the house and still hit target
    returns after also funding the rehab.
    """
    n = noi(monthly_rent, annual_tax, cfg["insurance_annual"],
            cfg["vacancy"], cfg["maint_mgmt_pct"])
    if n <= 0:
        return {"noi": round(n), "price_at_target_cap": 0,
                "price_at_target_coc_unlevered": 0,
                "price_at_target_coc_levered": 0,
                "repairs_deducted": round(repairs)}
    # Unlevered: price where cap == target_coc
    p_unlev = n / cfg["target_coc"]
    # Levered: solve price P so (NOI - debt_service) / cash_in == target_coc
    # cash_in = down + closing; debt on (1-down)*P at loan_rate.
    # (n - k*(1-down)*P) = target * (down+closing)*P  ->  P = n / (k*(1-down) + target*(down+closing))
    monthly_factor = _pmt(1.0, cfg["loan_rate"], cfg["loan_years"]) * 12
    denom = monthly_factor * (1 - cfg["down_pct"]) + cfg["target_coc"] * (
        cfg["down_pct"] + cfg["closing_pct"])
    p_lev = n / denom if denom > 0 else 0
    return {
        "noi": round(n),
        "price_at_target_cap": max(0, round(n / cfg["target_cap"] - repairs)),
        "price_at_target_coc_unlevered": max(0, round(p_unlev - repairs)),
        "price_at_target_coc_levered": max(0, round(p_lev - repairs)),
        "repairs_deducted": round(repairs),
    }


def creative_structure(asking, mortgage_balance, mortgage_rate=None, cfg=DEFAULTS):
    """Subject-to opening structure: take over the loan + cash for part of equity."""
    if not mortgage_balance or mortgage_balance <= 0:
        return None
    equity = max(0, asking - mortgage_balance)
    cash_to_seller = round(equity * cfg["cash_to_seller_pct_of_equity"])
    rate = mortgage_rate if mortgage_rate else cfg["loan_rate"]
    return {
        "equity_gap": round(equity),
        "cash_to_seller_opening": cash_to_seller,
        "loan_balance_taken_over": round(mortgage_balance),
        "est_monthly_pi": round(_pmt(mortgage_balance, rate, cfg["loan_years"])),
        "rate_assumed": rate,
        "total_effective_price": round(mortgage_balance + cash_to_seller),
    }


def evaluate(payload: dict) -> dict:
    """Single entry point for the webhook.

    Required: arv (pre-loaded from comps — never agent-invented).
    Optional: repairs, asking_price, mortgage_balance, mortgage_rate,
              monthly_rent, annual_tax, profit_margin, wholesale_fee.
    """
    cfg = dict(DEFAULTS)
    for k in ("profit_margin", "wholesale_fee"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]

    arv = float(payload["arv"])
    repairs = float(payload.get("repairs") or 0)
    asking = payload.get("asking_price")
    asking = float(asking) if asking else None

    cash_max = max_cash_offer(arv, repairs, cfg["profit_margin"], cfg["wholesale_fee"])

    out = {
        "opening_offer": cash_max,   # HMHW cash number — where the agent opens
        "arv": round(arv),
        "repairs": round(repairs),
    }

    rent = payload.get("monthly_rent")
    tax = payload.get("annual_tax")
    if rent and tax is not None:
        rp = rental_prices(float(rent), float(tax), cfg, repairs)
        out["rental"] = rp
        # A hold is attractive if the financed 20%-CoC price clears the cash max.
        out["rental_hold_viable"] = rp["price_at_target_coc_levered"] >= cash_max

    bal = payload.get("mortgage_balance")
    creative = None
    if bal and asking:
        creative = creative_structure(asking, float(bal),
                                      payload.get("mortgage_rate"), cfg)
    out["creative"] = creative

    # Recommended opening structure + the hard ceiling for it.
    # The agent may never agree above "ceiling" for the recommended structure.
    if creative and asking and float(bal) >= 0.5 * asking:
        out["recommended_structure"] = "creative_subject_to"
        out["ceiling"] = min(round(asking), creative["total_effective_price"])
        out["opening_position"] = (
            "Lead with taking over the existing loan plus cash to the seller "
            "for part of their equity; anchor with the cash max offer if they "
            "push back.")
    elif out.get("rental_hold_viable"):
        out["recommended_structure"] = "cash_offer_rental_hold"
        out["ceiling"] = out["rental"]["price_at_target_coc_levered"]
        out["opening_position"] = (
            "Cash offer at the max offer; the numbers work as a rental hold "
            "at that price.")
    else:
        out["recommended_structure"] = "cash_offer_wholesale"
        out["ceiling"] = cash_max
        out["opening_position"] = (
            "Cash offer at the max offer, as-is, fast close; if their floor "
            "is above it, pivot to structure (time/payments) or park as a "
            "follow-up. Never exceed the max offer.")
    # Ceiling: the most we may pay under ANY approved structure, never above
    # the asking price when one is known. Opening: the HMHW cash number,
    # kept below the ceiling.
    candidates = [cash_max]
    if out.get("rental"):
        candidates.append(out["rental"]["price_at_target_coc_levered"])
    if creative:
        candidates.append(creative["total_effective_price"])
    ceiling = max(candidates)
    if asking:
        ceiling = min(ceiling, round(asking))
    opening = min(cash_max, ceiling)
    if opening >= ceiling:
        opening = round(ceiling * 0.93)
    out["opening_offer"] = opening
    out["ceiling"] = ceiling
    out["max_offer"] = ceiling  # hard cap the agent may state
    out["walk_away"] = ceiling
    return out
