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
    "arv_basis_cap_pct": 0.78,  # Shawn's rule (2026-10-16): on a rental hold,
                                # price + repairs may not exceed 78% of ARV
    "sf_payment_rent_cap_pct": 0.40,  # Shawn's rule (2026-10-16): seller-
                                # finance payment <= 40% of monthly rent
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


def rental_prices(monthly_rent, annual_tax, cfg=DEFAULTS, repairs=0.0,
                  arv=None):
    """Prices at which the rental math hits Shawn's targets.

    Repairs are cash the buyer pays on top of the purchase price, so each
    supported price is reduced dollar-for-dollar by the repair estimate
    (floored at 0): the most he can PAY for the house and still hit target
    returns after also funding the rehab.

    Basis guardrail (Shawn, 2026-10-16): when the ARV is known, no supported
    price may put total basis (price + repairs) above arv_basis_cap_pct of
    ARV, so a strong rent can never talk him into overpaying against value.
    """
    n = noi(monthly_rent, annual_tax, cfg["insurance_annual"],
            cfg["vacancy"], cfg["maint_mgmt_pct"])
    basis_cap_price = None
    if arv:
        basis_cap_price = max(0, round(cfg["arv_basis_cap_pct"] * arv
                                       - repairs))
    if n <= 0:
        return {"noi": round(n), "price_at_target_cap": 0,
                "price_at_target_coc_unlevered": 0,
                "price_at_target_coc_levered": 0,
                "repairs_deducted": round(repairs),
                "basis_cap_price": basis_cap_price}
    # Unlevered: price where cap == target_coc
    p_unlev = n / cfg["target_coc"]
    # Levered: solve price P so (NOI - debt_service) / cash_in == target_coc
    # cash_in = down + closing; debt on (1-down)*P at loan_rate.
    # (n - k*(1-down)*P) = target * (down+closing)*P  ->  P = n / (k*(1-down) + target*(down+closing))
    monthly_factor = _pmt(1.0, cfg["loan_rate"], cfg["loan_years"]) * 12
    denom = monthly_factor * (1 - cfg["down_pct"]) + cfg["target_coc"] * (
        cfg["down_pct"] + cfg["closing_pct"])
    p_lev = n / denom if denom > 0 else 0

    def _cap(price):
        price = max(0, round(price))
        if basis_cap_price is not None:
            price = min(price, basis_cap_price)
        return price

    return {
        "noi": round(n),
        "price_at_target_cap": _cap(n / cfg["target_cap"] - repairs),
        "price_at_target_coc_unlevered": _cap(p_unlev - repairs),
        "price_at_target_coc_levered": _cap(p_lev - repairs),
        "repairs_deducted": round(repairs),
        "basis_cap_price": basis_cap_price,
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


def _balance_factor(annual_rate, amort_years, balloon_years,
                    interest_only=False):
    """Fraction of the original loan balance still owed at the balloon."""
    if interest_only:
        return 1.0
    if annual_rate == 0:
        return max(0.0, 1.0 - balloon_years / amort_years)
    r = annual_rate / 12.0
    n = amort_years * 12
    m = balloon_years * 12
    return ((1 + r) ** n - (1 + r) ** m) / ((1 + r) ** n - 1)


def seller_finance(monthly_rent, annual_tax, arv, repairs, cfg=DEFAULTS,
                   asking=None, sf_price=None, sf_down=0.0, sf_rate=None,
                   sf_amort_years=30, sf_balloon_years=6,
                   sf_interest_only=False):
    """Seller-financed structure solver (Shawn, 2026-10-16).

    Primary case: turnkey rentals with tenants in place, where the rent is
    actual. A structure passes only if ALL THREE tests pass:
    1. Cash-flow test — after the seller payment, the property still hits
       Shawn's target cash-on-cash on his cash in (down + closing + repairs).
    2. Balloon test — the balance due at the balloon year is no more than
       arv_basis_cap_pct (78%) of CURRENT ARV (never a projected value), so
       a refinance can retire it.
    3. Payment cap (Shawn, 2026-10-16) — the monthly payment to the seller
       may not exceed 40% of the property's total monthly rent, leaving
       room for management, upkeep, and his minimum cash-on-cash.
    Returns the highest price that passes all three tests at the given
    terms, plus a verdict on a tested price (defaults to the asking price).
    """
    n = noi(monthly_rent, annual_tax, cfg["insurance_annual"],
            cfg["vacancy"], cfg["maint_mgmt_pct"])
    rate = cfg["loan_rate"] if sf_rate is None else sf_rate
    down = float(sf_down or 0)
    t = cfg["target_coc"]
    c = cfg["closing_pct"]
    if sf_interest_only:
        k = rate  # annual debt service per $1 financed
    else:
        k = _pmt(1.0, rate, sf_amort_years) * 12
    b = _balance_factor(rate, sf_amort_years, sf_balloon_years,
                        sf_interest_only)

    pay_cap = cfg["sf_payment_rent_cap_pct"] * monthly_rent

    def _at(price):
        financed = max(0.0, price - down)
        monthly = financed * rate / 12.0 if sf_interest_only else \
            financed * _pmt(1.0, rate, sf_amort_years)
        cash_in = down + c * price + repairs
        cash_flow = n - monthly * 12
        return {
            "monthly_payment": round(monthly),
            "payment_cap_monthly": round(pay_cap),
            "balloon_balance": round(financed * b),
            "balloon_cap": round(cfg["arv_basis_cap_pct"] * arv),
            "cash_in": round(cash_in),
            "est_annual_cash_flow": round(cash_flow),
            "est_coc_pct": round(100.0 * cash_flow / cash_in, 1)
            if cash_in > 0 else None,
        }

    if n <= 0:
        max_price = 0
    else:
        # Cash-flow test solved for price (down payment is a fixed $ amount).
        p_cf = (n + down * (k - t) - t * repairs) / (k + t * c)
        # Balloon test solved for price.
        p_bal = down + cfg["arv_basis_cap_pct"] * arv / b if b > 0 else 0
        # Payment cap solved for price: monthly payment <= cap.
        if sf_interest_only:
            p_pay = down + pay_cap * 12.0 / rate if rate > 0 else 0
        else:
            p_pay = down + pay_cap / _pmt(1.0, rate, sf_amort_years)
        max_price = max(0, round(min(p_cf, p_bal, p_pay)))

    tested = sf_price if sf_price is not None else asking
    out = {
        "max_price": max_price,
        "down": round(down),
        "rate_assumed": rate,
        "amort_years": sf_amort_years,
        "balloon_years": sf_balloon_years,
        "interest_only": bool(sf_interest_only),
        "at_max": _at(max_price),
        "price_tested": round(tested) if tested else None,
        "price_works": None,
        "failed_test": None,
        "at_tested": None,
    }
    if tested:
        at = _at(float(tested))
        out["at_tested"] = at
        failed = []
        if not (at["est_coc_pct"] is not None
                and at["est_coc_pct"] >= 100.0 * t):
            failed.append("cash_flow")
        if at["balloon_balance"] > at["balloon_cap"]:
            failed.append("balloon_cap")
        if at["monthly_payment"] > at["payment_cap_monthly"]:
            failed.append("payment_cap")
        out["failed_tests"] = failed
        out["failed_test"] = "+".join(failed) if failed else None
        out["price_works"] = not failed
    return out


def evaluate(payload: dict) -> dict:
    """Single entry point for the webhook.

    Required: arv (pre-loaded from comps — never agent-invented).
    Optional: repairs, asking_price, mortgage_balance, mortgage_rate,
              monthly_rent, annual_tax, profit_margin, wholesale_fee.
    """
    cfg = dict(DEFAULTS)
    for k in ("profit_margin", "wholesale_fee", "arv_basis_cap_pct",
              "sf_payment_rent_cap_pct"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]

    arv = float(payload["arv"])
    # Shawn's fee cap (2026-10-16): on houses under $100k ARV his assignment
    # fee caps at $5,000 (the lower cap), not the standard $10,000 — applies
    # wherever the default fee is used, unless a call sets the fee explicitly.
    if payload.get("wholesale_fee") is None and arv < 100000:
        cfg["wholesale_fee"] = 5000
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
        rp = rental_prices(float(rent), float(tax), cfg, repairs, arv)
        out["rental"] = rp
        # A hold is attractive if the financed 20%-CoC price clears the cash max.
        out["rental_hold_viable"] = rp["price_at_target_coc_levered"] >= cash_max
        out["seller_finance"] = seller_finance(
            float(rent), float(tax), arv, repairs, cfg, asking,
            sf_price=payload.get("sf_price"),
            sf_down=payload.get("sf_down") or 0,
            sf_rate=payload.get("sf_rate"),
            sf_amort_years=payload.get("sf_amort_years") or 30,
            sf_balloon_years=payload.get("sf_balloon_years") or 6,
            sf_interest_only=bool(payload.get("sf_interest_only")))

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

    out.setdefault("seller_finance", None)

    # Assign ceiling (Shawn, 2026-10-16): if this deal is assigned to another
    # buyer instead of held, the contract price must leave room for Shawn's
    # assignment fee on top of it: assign_ceiling = ceiling - fee. The fee is
    # $10,000 standard, capped at $5,000 on houses under $100k ARV (applied
    # above). The wholesale path already nets the fee inside its max offer,
    # so the assign figure only applies to the hold and creative paths.
    if out["recommended_structure"] == "cash_offer_wholesale":
        out["assign_ceiling"] = None
    else:
        out["assign_ceiling"] = max(0, ceiling - cfg["wholesale_fee"])
    out["assignment_fee"] = cfg["wholesale_fee"]
    return out
