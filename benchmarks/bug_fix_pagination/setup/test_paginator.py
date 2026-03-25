"""Tests for the pagination utility. DO NOT MODIFY THESE TESTS."""

import pytest
from paginator import paginate


def test_basic_pagination():
    items = list(range(1, 11))  # [1..10]
    result = paginate(items, page=1, per_page=3)
    assert result["items"] == [1, 2, 3]
    assert result["total_pages"] == 4  # 10 items / 3 per page = 4 pages
    assert result["has_next"] is True
    assert result["has_prev"] is False


def test_last_page_partial():
    """The last page should contain remaining items even if fewer than per_page."""
    items = list(range(1, 11))  # [1..10]
    result = paginate(items, page=4, per_page=3)
    assert result["items"] == [10]  # Only 1 item on last page
    assert result["total_pages"] == 4
    assert result["has_next"] is False
    assert result["has_prev"] is True


def test_exact_division():
    items = list(range(1, 13))  # [1..12]
    result = paginate(items, page=4, per_page=3)
    assert result["items"] == [10, 11, 12]
    assert result["total_pages"] == 4


def test_single_page():
    items = [1, 2]
    result = paginate(items, page=1, per_page=10)
    assert result["items"] == [1, 2]
    assert result["total_pages"] == 1
    assert result["has_next"] is False
    assert result["has_prev"] is False


def test_empty_items():
    result = paginate([], page=1, per_page=5)
    assert result["items"] == []
    assert result["total_pages"] == 1


def test_page_out_of_range():
    items = list(range(1, 6))  # [1..5]
    result = paginate(items, page=3, per_page=5)
    assert result["items"] == []  # No items on page 3


def test_invalid_page():
    with pytest.raises(ValueError):
        paginate([1, 2, 3], page=0, per_page=2)


def test_invalid_per_page():
    with pytest.raises(ValueError):
        paginate([1, 2, 3], page=1, per_page=0)


def test_seven_items_three_per_page():
    """7 items at 3 per page = 3 pages (3+3+1)."""
    items = list(range(1, 8))
    result = paginate(items, page=1, per_page=3)
    assert result["total_pages"] == 3

    result = paginate(items, page=3, per_page=3)
    assert result["items"] == [7]
    assert result["has_next"] is False
