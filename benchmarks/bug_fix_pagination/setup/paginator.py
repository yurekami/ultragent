"""Pagination utility for splitting collections into pages."""

from typing import TypeVar, Sequence

T = TypeVar("T")


def paginate(items: Sequence[T], page: int, per_page: int) -> dict:
    """
    Paginate a sequence of items.

    Args:
        items: The full collection to paginate
        page: Page number (1-indexed)
        per_page: Items per page

    Returns:
        dict with keys: items, page, per_page, total_items, total_pages, has_next, has_prev
    """
    if page < 1:
        raise ValueError("Page must be >= 1")
    if per_page < 1:
        raise ValueError("per_page must be >= 1")

    total_items = len(items)
    # BUG: integer division truncates, missing last partial page
    total_pages = total_items // per_page
    if total_pages == 0:
        total_pages = 1

    start = (page - 1) * per_page
    end = start + per_page
    page_items = list(items[start:end])

    return {
        "items": page_items,
        "page": page,
        "per_page": per_page,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }
