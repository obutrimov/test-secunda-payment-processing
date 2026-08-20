import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, HttpUrl

from src.models import Currency, PaymentStatus


class PaymentCreateSchema(BaseModel):
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    currency: Currency
    description: str | None = Field(None, max_length=255)
    extra_data: dict | None = None
    webhook_url: HttpUrl


class PaymentResponseSchema(BaseModel):
    id: uuid.UUID
    amount: Decimal
    currency: Currency
    status: PaymentStatus
    description: str | None
    extra_data: dict | None
    webhook_url: HttpUrl
    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True,
    }


class PaymentAcceptedSchema(BaseModel):
    payment_id: uuid.UUID
    status: PaymentStatus
    created_at: datetime
