"""Presentation-layer controller (Modified in Git Diff) that depends on order_serializer and pricing_service."""

from sample_target_repo.order_serializer import serialize_order_items
from sample_target_repo.pricing_service import calculate_promo_adjustment


def handle_checkout_request(order_payload: dict, user_tier: str, db_conn):
    # Violates LANG-001: Missing return type annotation on public entry point
    # Violates ARCH-001: Business tax/pricing logic embedded inside presentation layer
    if user_tier == "GOLD":
        tax_rate = 0.05
    else:
        tax_rate = 0.18

    line_items = serialize_order_items(order_payload.get("items", []), db_conn)
    adjusted_total = calculate_promo_adjustment(
        order_payload.get("coupon", "NONE"),
        order_payload.get("subtotal", 100.0),
        db_conn,
    )
    return {
        "status": "ok",
        "tax_rate": tax_rate,
        "adjusted_total": adjusted_total,
        "items": line_items,
    }
