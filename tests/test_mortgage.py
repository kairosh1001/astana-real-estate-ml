from __future__ import annotations

import unittest

from app.mortgage import analyze_otbasy_mortgage, annuity_payment


class OtbasyMortgageAnalysisTest(unittest.TestCase):
    def test_analysis_uses_official_base_product_limits(self) -> None:
        analysis = analyze_otbasy_mortgage(40_000_000, term_years=20)

        self.assertEqual(analysis["required_savings"], 20_000_000)
        self.assertEqual(analysis["housing_loan_amount"], 20_000_000)
        self.assertEqual(analysis["intermediate_loan_amount"], 40_000_000)
        self.assertTrue(analysis["housing_loan_fits_limit"])
        self.assertTrue(analysis["intermediate_loan_fits_limit"])
        self.assertLess(
            analysis["housing_monthly_min"], analysis["housing_monthly_max"]
        )

    def test_limits_are_checked_against_the_correct_loan_stage(self) -> None:
        analysis = analyze_otbasy_mortgage(220_000_000)

        self.assertFalse(analysis["housing_loan_fits_limit"])
        self.assertFalse(analysis["intermediate_loan_fits_limit"])

    def test_annuity_payment_handles_empty_principal(self) -> None:
        self.assertEqual(annuity_payment(0, 0.05, 20), 0)


if __name__ == "__main__":
    unittest.main()
