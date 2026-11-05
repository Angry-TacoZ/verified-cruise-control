"""Deterministic wrong-vehicle filename filter.

Fail-open boundaries are the load-bearing behavior: anything ambiguous
stays in the audit; only filenames that provably name a different
(year, make, model) are excluded.
"""

from __future__ import annotations

import pytest

from audits.vehicle_match import filename_vehicle, is_wrong_vehicle, slug


def test_slug_normalizes_case_and_separators():
    assert slug("MINI") == "mini"
    assert slug("Cooper S") == "coopers"
    assert slug(None) == ""
    assert slug("") == ""


def test_filename_vehicle_extracts_triple():
    assert filename_vehicle("2024_mini_cooper-pic-7288656705.jpg") == (
        "2024",
        "mini",
        "cooper",
    )


def test_filename_vehicle_accepts_dash_separators():
    assert filename_vehicle("2023-audi-q5-pic-1.jpg") == ("2023", "audi", "q5")


def test_filename_vehicle_returns_none_without_triple():
    assert filename_vehicle("dollar-car_ae22db82242c877.jpg") is None
    assert filename_vehicle("397432559619947607-296x222.jpg") is None
    assert filename_vehicle("") is None
    assert filename_vehicle(None) is None


def test_matching_vehicle_is_kept():
    assert (
        is_wrong_vehicle(
            "2024_mini_cooper-pic-7288656705-296x222.jpg", 2024, "MINI", "Cooper"
        )
        is False
    )


def test_different_year_is_excluded():
    assert (
        is_wrong_vehicle(
            "2020_dodge_challenger-pic-14136523764.jpg", 2024, "MINI", "Cooper"
        )
        is True
    )


def test_different_make_is_excluded():
    assert (
        is_wrong_vehicle(
            "2022_ford_explorer-pic-9730521025.jpg", 2024, "MINI", "Cooper"
        )
        is True
    )


def test_different_model_is_excluded():
    assert (
        is_wrong_vehicle("2024_mini_countryman-pic-9.jpg", 2024, "MINI", "Cooper")
        is True
    )


def test_trim_variant_is_kept():
    # "coopers" (Cooper S file) must not exclude a "Cooper" listing, and
    # vice versa: prefix tolerance only, never a different model.
    assert (
        is_wrong_vehicle("2024_mini_coopers-pic-1.jpg", 2024, "MINI", "Cooper") is False
    )
    assert (
        is_wrong_vehicle("2024_mini_cooper-pic-1.jpg", 2024, "MINI", "Cooper S")
        is False
    )


def test_ambiguous_filenames_are_kept():
    assert (
        is_wrong_vehicle("dollar-car_ae22db82242c877.jpg", 2024, "MINI", "Cooper")
        is False
    )
    assert (
        is_wrong_vehicle("397432559619947607-296x222.jpg", 2024, "MINI", "Cooper")
        is False
    )
    assert is_wrong_vehicle("", 2024, "MINI", "Cooper") is False
    assert is_wrong_vehicle(None, 2024, "MINI", "Cooper") is False


def test_incomplete_listing_vehicle_keeps_everything():
    assert (
        is_wrong_vehicle("2020_dodge_challenger-pic-1.jpg", None, "MINI", "Cooper")
        is False
    )
    assert (
        is_wrong_vehicle("2020_dodge_challenger-pic-1.jpg", 2024, None, "Cooper")
        is False
    )
    assert (
        is_wrong_vehicle("2020_dodge_challenger-pic-1.jpg", 2024, "MINI", None) is False
    )


@pytest.mark.parametrize("year", [2024, "2024"])
def test_year_accepts_int_or_string(year):
    assert (
        is_wrong_vehicle("2024_mini_cooper-pic-1.jpg", year, "MINI", "Cooper") is False
    )
    assert (
        is_wrong_vehicle("2023_mini_cooper-pic-1.jpg", year, "MINI", "Cooper") is True
    )
