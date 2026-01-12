# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Репозиторий платежей (payments) - Stars confirm, идемпотентность, админ-поиск
# Дата изменения: 2026-01-12
# ----------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def payments_insert_pending(
    db: Session,
    *,
    user_id: int,
    provider: str,
    status: str,
    telegram_payment_charge_id: Optional[str],
    provider_payment_charge_id: Optional[str],
    invoice_payload: str,
    plan_code: Optional[str],
    currency: str,
    amount: Optional[int],
    idempotency_key: str,
    raw: Optional[dict[str, Any]],
) -> int:
    """
    Создаём запись о платеже (идемпотентно).
    Возвращает payment_id (существующий или новый).

    Реализация:
      - сначала пытаемся найти по idempotency_key
      - затем вставляем
    """
    existing = db.execute(
        text("SELECT id FROM payments WHERE idempotency_key = :k"),
        {"k": idempotency_key},
    ).scalar_one_or_none()
    if existing:
        return int(existing)

    row = db.execute(
        text(
            """
            INSERT INTO payments (
                user_id, provider, status,
                telegram_payment_charge_id, provider_payment_charge_id,
                invoice_payload, plan_code, currency, amount,
                idempotency_key, raw, created_at, updated_at
            )
            VALUES (
                :user_id, :provider, :status,
                :tg_charge, :prov_charge,
                :invoice_payload, :plan_code, :currency, :amount,
                :idem_key, :raw::jsonb, NOW(), NOW()
            )
            RETURNING id
            """
        ),
        {
            "user_id": user_id,
            "provider": provider,
            "status": status,
            "tg_charge": telegram_payment_charge_id,
            "prov_charge": provider_payment_charge_id,
            "invoice_payload": invoice_payload,
            "plan_code": plan_code,
            "currency": currency,
            "amount": amount,
            "idem_key": idempotency_key,
            "raw": None if raw is None else _to_json(raw),
        },
    ).scalar_one()

    return int(row)


def payments_mark_confirmed(
    db: Session,
    *,
    payment_id: int,
) -> None:
    db.execute(
        text(
            """
            UPDATE payments
            SET status='confirmed', confirmed_at=NOW(), updated_at=NOW()
            WHERE id = :id
            """
        ),
        {"id": payment_id},
    )


def payments_get_by_id(db: Session, payment_id: int) -> Optional[dict[str, Any]]:
    row = db.execute(
        text(
            """
            SELECT
                id, user_id, provider, status,
                telegram_payment_charge_id, provider_payment_charge_id,
                invoice_payload, plan_code, currency, amount,
                idempotency_key, raw,
                created_at, confirmed_at, updated_at
            FROM payments
            WHERE id = :id
            """
        ),
        {"id": payment_id},
    ).mappings().first()
    return dict(row) if row else None


def payments_find(
    db: Session,
    *,
    telegram_payment_charge_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    user_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}

    if telegram_payment_charge_id:
        where.append("telegram_payment_charge_id = :tg")
        params["tg"] = telegram_payment_charge_id

    if idempotency_key:
        where.append("idempotency_key = :idem")
        params["idem"] = idempotency_key

    if user_id is not None:
        where.append("user_id = :uid")
        params["uid"] = int(user_id)

    if status:
        where.append("status = :st")
        params["st"] = status

    where_sql = ""
    if where:
        where_sql = "WHERE " + " AND ".join(where)

    rows = db.execute(
        text(
            f"""
            SELECT
                id, user_id, provider, status,
                telegram_payment_charge_id, provider_payment_charge_id,
                invoice_payload, plan_code, currency, amount,
                idempotency_key,
                created_at, confirmed_at, updated_at
            FROM payments
            {where_sql}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    return [dict(r) for r in rows]


def _to_json(data: dict[str, Any]) -> str:
    """
    JSON сериализация без внешних зависимостей: для передачи в raw::jsonb.
    """
    import json

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
