"""
# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: API для фиксации оплат Telegram Stars и выдачи подписок
# Дата изменения: 2026-01-12
#
# Функции:
#  - POST /api/v1/payments/telegram/success  (идемпотентно по telegram_payment_charge_id)
#  - GET  /api/v1/admin/payments             (последние платежи)
#  - GET  /api/v1/admin/users/{telegram_id}/payments (история платежей пользователя)
# ----------------------------------------------------------
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from config import get_settings
from db import get_db
from models import Payment, Subscription, SubscriptionPlan, User
from main import require_mgmt_token, utcnow  # если у тебя main.py называется иначе — поправишь импорт

logger = logging.getLogger("vpn-backend")
settings = get_settings()

router = APIRouter(tags=["payments"])


class TelegramPaymentSuccessIn(BaseModel):
    telegram_id: int = Field(..., description="Telegram ID пользователя")
    currency: str = Field(..., description="Валюта Telegram (для Stars обычно XTR)")
    amount: float = Field(..., ge=0, description="Сумма (в Stars)")
    invoice_payload: str = Field(..., description="payload из invoice (например vpn_plan:m1_69:tgid:ts)")
    telegram_payment_charge_id: str = Field(..., description="telegram_payment_charge_id из SuccessfulPayment")
    provider_payment_charge_id: Optional[str] = Field(None, description="provider_payment_charge_id (если есть)")


class TelegramPaymentSuccessOut(BaseModel):
    ok: bool
    message: str
    telegram_id: int
    payment_id: Optional[int] = None
    subscription_id: Optional[int] = None
    active_until: Optional[str] = None
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None


class AdminPaymentItem(BaseModel):
    id: int
    provider: str
    telegram_id: int
    currency: str
    amount: float
    invoice_payload: str
    telegram_payment_charge_id: str
    status: str
    created_at: str
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None


def _parse_plan_code_from_payload(payload: str) -> str:
    """
    Ожидаемый формат: vpn_plan:<plan_code>:<telegram_id>:<ts>
    Пример: vpn_plan:m1_69:351136125:1768233051
    """
    raw = (payload or "").strip()
    parts = raw.split(":")
    if len(parts) < 2:
        raise ValueError("Некорректный invoice_payload (ожидается vpn_plan:<plan_code>:...)")
    if parts[0] != "vpn_plan":
        raise ValueError("Некорректный invoice_payload (ожидается prefix vpn_plan)")
    plan_code = (parts[1] or "").strip()
    if not plan_code:
        raise ValueError("Некорректный invoice_payload: plan_code пустой")
    return plan_code


def _deactivate_user_subscriptions(db: Session, user_id: int) -> None:
    subs = (
        db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.is_active.is_(True))
            .order_by(Subscription.ends_at.desc())
        )
        .scalars()
        .all()
    )
    for s in subs:
        s.is_active = False
        db.add(s)


@router.post(
    "/api/v1/payments/telegram/success",
    response_model=TelegramPaymentSuccessOut,
    summary="Зафиксировать успешный платеж Telegram Stars и активировать подписку (идемпотентно)",
)
def telegram_payment_success(
    payload: TelegramPaymentSuccessIn,
    db: Session = Depends(get_db),
) -> TelegramPaymentSuccessOut:
    # 1) Найти пользователя
    user = db.execute(select(User).where(User.telegram_id == payload.telegram_id)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    # 2) Идемпотентность: если такой charge_id уже есть — возвращаем уже созданный результат
    existing_payment = (
        db.execute(select(Payment).where(Payment.telegram_payment_charge_id == payload.telegram_payment_charge_id))
        .scalars()
        .first()
    )
    if existing_payment:
        # попробуем отдать связанную подписку/план
        plan_code = None
        plan_name = None
        if existing_payment.plan_id:
            plan = db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == existing_payment.plan_id)).scalar_one_or_none()
            if plan:
                plan_code = plan.code
                plan_name = plan.name

        active_until = None
        if existing_payment.subscription_id:
            sub = db.execute(select(Subscription).where(Subscription.id == existing_payment.subscription_id)).scalar_one_or_none()
            if sub and sub.ends_at:
                active_until = sub.ends_at.isoformat()

        return TelegramPaymentSuccessOut(
            ok=True,
            message="Платёж уже зафиксирован ранее (идемпотентность).",
            telegram_id=payload.telegram_id,
            payment_id=existing_payment.id,
            subscription_id=existing_payment.subscription_id,
            active_until=active_until,
            plan_code=plan_code,
            plan_name=plan_name,
        )

    # 3) Определить тариф из invoice_payload
    try:
        plan_code = _parse_plan_code_from_payload(payload.invoice_payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    plan = db.execute(select(SubscriptionPlan).where(SubscriptionPlan.code == plan_code)).scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Тарифный план не найден: {plan_code}")

    if not plan.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Тарифный план отключен")

    # 4) Доп. защита: сумма должна совпадать (минимально строго)
    # Разрешим небольшие расхождения из-за типов (float/decimal).
    expected = float(plan.price_stars)
    paid = float(payload.amount)
    if abs(expected - paid) > 0.0001:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Сумма оплаты не совпадает с тарифом: paid={paid} expected={expected}",
        )

    now = utcnow()
    ends_at = now + timedelta(days=int(plan.duration_days))

    # 5) Деактивировать текущие активные подписки пользователя
    _deactivate_user_subscriptions(db, user.id)

    # 6) Создать новую подписку
    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        server_id=None,
        starts_at=now,
        ends_at=ends_at,
        is_active=True,
        is_trial=False,
        source="stars",
    )
    db.add(sub)
    db.flush()  # получить sub.id без commit

    # 7) Записать платеж
    pay = Payment(
        provider="telegram_stars",
        user_id=user.id,
        telegram_id=int(payload.telegram_id),
        plan_id=plan.id,
        subscription_id=sub.id,
        currency=str(payload.currency),
        amount=paid,
        invoice_payload=str(payload.invoice_payload),
        telegram_payment_charge_id=str(payload.telegram_payment_charge_id),
        provider_payment_charge_id=str(payload.provider_payment_charge_id) if payload.provider_payment_charge_id else None,
        status="paid",
    )
    db.add(pay)

    db.commit()
    db.refresh(pay)
    db.refresh(sub)

    logger.info(
        "payments/telegram/success: stored payment_id=%s sub_id=%s telegram_id=%s plan=%s amount=%s %s",
        pay.id,
        sub.id,
        payload.telegram_id,
        plan.code,
        paid,
        payload.currency,
    )

    return TelegramPaymentSuccessOut(
        ok=True,
        message="Оплата зафиксирована, подписка активирована.",
        telegram_id=payload.telegram_id,
        payment_id=pay.id,
        subscription_id=sub.id,
        active_until=sub.ends_at.isoformat(),
        plan_code=plan.code,
        plan_name=plan.name,
    )


@router.get(
    "/api/v1/admin/payments",
    response_model=list[AdminPaymentItem],
    summary="Последние платежи (admin)",
    tags=["admin"],
)
def admin_last_payments(
    limit: int = 20,
    _token: str = Depends(require_mgmt_token),
    db: Session = Depends(get_db),
) -> list[AdminPaymentItem]:
    if limit <= 0:
        limit = 20
    if limit > 200:
        limit = 200

    rows = (
        db.execute(select(Payment).order_by(desc(Payment.created_at)).limit(limit))
        .scalars()
        .all()
    )

    result: list[AdminPaymentItem] = []
    for p in rows:
        plan_code = None
        plan_name = None
        if p.plan_id:
            pl = db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == p.plan_id)).scalar_one_or_none()
            if pl:
                plan_code = pl.code
                plan_name = pl.name

        result.append(
            AdminPaymentItem(
                id=p.id,
                provider=p.provider,
                telegram_id=int(p.telegram_id),
                currency=str(p.currency),
                amount=float(p.amount),
                invoice_payload=p.invoice_payload,
                telegram_payment_charge_id=p.telegram_payment_charge_id,
                status=p.status,
                created_at=p.created_at.isoformat() if p.created_at else "",
                plan_code=plan_code,
                plan_name=plan_name,
            )
        )
    return result


@router.get(
    "/api/v1/admin/users/{telegram_id}/payments",
    response_model=list[AdminPaymentItem],
    summary="Платежи пользователя по telegram_id (admin)",
    tags=["admin"],
)
def admin_user_payments(
    telegram_id: int,
    limit: int = 50,
    _token: str = Depends(require_mgmt_token),
    db: Session = Depends(get_db),
) -> list[AdminPaymentItem]:
    if limit <= 0:
        limit = 50
    if limit > 500:
        limit = 500

    rows = (
        db.execute(
            select(Payment)
            .where(Payment.telegram_id == telegram_id)
            .order_by(desc(Payment.created_at))
            .limit(limit)
        )
        .scalars()
        .all()
    )

    result: list[AdminPaymentItem] = []
    for p in rows:
        plan_code = None
        plan_name = None
        if p.plan_id:
            pl = db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == p.plan_id)).scalar_one_or_none()
            if pl:
                plan_code = pl.code
                plan_name = pl.name

        result.append(
            AdminPaymentItem(
                id=p.id,
                provider=p.provider,
                telegram_id=int(p.telegram_id),
                currency=str(p.currency),
                amount=float(p.amount),
                invoice_payload=p.invoice_payload,
                telegram_payment_charge_id=p.telegram_payment_charge_id,
                status=p.status,
                created_at=p.created_at.isoformat() if p.created_at else "",
                plan_code=plan_code,
                plan_name=plan_name,
            )
        )
    return result
