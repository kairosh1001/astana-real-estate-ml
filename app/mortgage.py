from __future__ import annotations

from dataclasses import asdict, dataclass


HOUSING_LOAN_MIN_RATE = 0.035
HOUSING_LOAN_MAX_RATE = 0.05
HOUSING_LOAN_MAX_AMOUNT = 100_000_000
HOUSING_LOAN_MAX_YEARS = 25
INTERMEDIATE_LOAN_RATE = 0.085
INTERMEDIATE_LOAN_MAX_AMOUNT = 200_000_000
REQUIRED_SAVINGS_SHARE = 0.5
OTBASY_SOURCE_VERIFIED = "13 августа 2026 года"


@dataclass(frozen=True)
class MortgageAnalysis:
    apartment_price: float
    required_savings: float
    housing_loan_amount: float
    housing_loan_fits_limit: bool
    housing_monthly_min: float
    housing_monthly_max: float
    housing_term_years: int
    housing_min_rate_pct: float
    housing_max_rate_pct: float
    housing_max_amount: float
    intermediate_loan_amount: float
    intermediate_loan_fits_limit: bool
    intermediate_rate_pct: float
    intermediate_max_amount: float
    source_verified: str


def annuity_payment(principal: float, annual_rate: float, years: int) -> float:
    if principal <= 0 or years <= 0:
        return 0.0
    monthly_rate = annual_rate / 12
    months = years * 12
    factor = (1 + monthly_rate) ** months
    return principal * monthly_rate * factor / (factor - 1)


def analyze_otbasy_mortgage(
    apartment_price: float,
    *,
    term_years: int = 20,
) -> dict:
    safe_price = max(float(apartment_price or 0), 0.0)
    safe_term = min(max(int(term_years), 1), HOUSING_LOAN_MAX_YEARS)
    required_savings = safe_price * REQUIRED_SAVINGS_SHARE
    housing_loan_amount = safe_price - required_savings
    analysis = MortgageAnalysis(
        apartment_price=safe_price,
        required_savings=required_savings,
        housing_loan_amount=housing_loan_amount,
        housing_loan_fits_limit=housing_loan_amount <= HOUSING_LOAN_MAX_AMOUNT,
        housing_monthly_min=annuity_payment(
            housing_loan_amount, HOUSING_LOAN_MIN_RATE, safe_term
        ),
        housing_monthly_max=annuity_payment(
            housing_loan_amount, HOUSING_LOAN_MAX_RATE, safe_term
        ),
        housing_term_years=safe_term,
        housing_min_rate_pct=HOUSING_LOAN_MIN_RATE * 100,
        housing_max_rate_pct=HOUSING_LOAN_MAX_RATE * 100,
        housing_max_amount=HOUSING_LOAN_MAX_AMOUNT,
        # The official intermediate product lends 100% of the property price
        # while the client's 50% deposit remains pledged during the first stage.
        intermediate_loan_amount=safe_price,
        intermediate_loan_fits_limit=safe_price <= INTERMEDIATE_LOAN_MAX_AMOUNT,
        intermediate_rate_pct=INTERMEDIATE_LOAN_RATE * 100,
        intermediate_max_amount=INTERMEDIATE_LOAN_MAX_AMOUNT,
        source_verified=OTBASY_SOURCE_VERIFIED,
    )
    return asdict(analysis)
