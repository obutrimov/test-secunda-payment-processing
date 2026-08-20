"""
Юнит-тесты для src/consumer/main.py.

Покрывают:
  - _execute_payment_logic: обновление статуса, вызов webhook,
    обработка отсутствия платежа
  - _send_webhook: корректная отправка HTTP POST и обработка ошибок
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.consumer.main import _execute_payment_logic, _send_webhook
from src.models import Currency, PaymentStatus

# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------


def make_mock_payment(
    status: PaymentStatus = PaymentStatus.PENDING,
    webhook_url: str = "https://example.com/webhook",
) -> MagicMock:
    """
    Создаёт MagicMock, имитирующий объект Payment.
    Используем MagicMock, а не Payment.__new__, т.к. SA instrumentation
    требует полной инициализации экземпляра через mapper.
    """
    payment = MagicMock()
    payment.id = uuid.uuid4()
    payment.amount = Decimal("100.00")
    payment.currency = Currency.RUB
    payment.status = status
    payment.description = "Test"
    payment.extra_data = None
    payment.idempotency_key = str(uuid.uuid4())
    payment.webhook_url = webhook_url
    payment.created_at = datetime.now(tz=UTC)
    payment.updated_at = datetime.now(tz=UTC)
    return payment


# ===========================================================================
# _send_webhook
# ===========================================================================


async def test_send_webhook_posts_correct_payload():
    """_send_webhook выполняет POST на webhook_url с корректным JSON."""
    payment = make_mock_payment(status=PaymentStatus.SUCCEEDED)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)

    await _send_webhook(mock_client, payment)

    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs.args[0] == payment.webhook_url
    sent_json = call_kwargs.kwargs["json"]
    assert sent_json["payment_id"] == str(payment.id)
    assert sent_json["status"] == payment.status.value


async def test_send_webhook_calls_raise_for_status():
    """_send_webhook вызывает raise_for_status() на ответе."""
    payment = make_mock_payment()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)

    await _send_webhook(mock_client, payment)

    mock_response.raise_for_status.assert_called_once()


async def test_send_webhook_raises_on_http_error():
    """_send_webhook пробрасывает HTTPStatusError наверх (без tenacity retry)."""
    payment = make_mock_payment()
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)

    # Вызываем __wrapped__ чтобы обойти tenacity retry в unit-тесте
    with pytest.raises(httpx.HTTPStatusError):
        await _send_webhook.__wrapped__(mock_client, payment)


async def test_send_webhook_raises_on_request_error():
    """_send_webhook пробрасывает RequestError при сетевом сбое."""
    payment = make_mock_payment()
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(
        side_effect=httpx.RequestError("Connection refused", request=MagicMock())
    )

    with pytest.raises(httpx.RequestError):
        await _send_webhook.__wrapped__(mock_client, payment)


async def test_send_webhook_includes_updated_at():
    """Payload вебхука содержит updated_at в ISO-формате."""
    payment = make_mock_payment()
    now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    payment.updated_at = now

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)

    await _send_webhook(mock_client, payment)

    sent_json = mock_client.post.call_args.kwargs["json"]
    assert sent_json["updated_at"] == now.isoformat()


# ===========================================================================
# _execute_payment_logic
# ===========================================================================


async def test_execute_payment_logic_payment_not_found():
    """Если платёж не найден в БД — функция завершается без исключений."""
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    await _execute_payment_logic(
        payment_id=uuid.uuid4(),
        session=mock_session,
        http_client=mock_client,
    )

    mock_client.post.assert_not_called()


@pytest.mark.parametrize(
    "random_val,expected_status",
    [
        (0.1, PaymentStatus.SUCCEEDED),  # < 0.9 → успех
        (0.9, PaymentStatus.FAILED),  # >= 0.9 → неудача
    ],
)
async def test_execute_payment_logic_updates_status(random_val, expected_status):
    """Статус платежа меняется на SUCCEEDED или FAILED в зависимости от random."""
    payment = make_mock_payment()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=payment)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("src.consumer.main.asyncio.sleep", new_callable=AsyncMock),
        patch("src.consumer.main.random.uniform", return_value=0.1),
        patch("src.consumer.main.random.random", return_value=random_val),
        patch("src.consumer.main._send_webhook", new_callable=AsyncMock),
    ):
        await _execute_payment_logic(
            payment_id=payment.id,
            session=mock_session,
            http_client=mock_client,
        )

    assert payment.status == expected_status
    mock_session.commit.assert_awaited_once()


async def test_execute_payment_logic_calls_webhook():
    """После обработки обязательно вызывается _send_webhook."""
    payment = make_mock_payment()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=payment)
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_send_webhook = AsyncMock()

    with (
        patch("src.consumer.main.asyncio.sleep", new_callable=AsyncMock),
        patch("src.consumer.main.random.uniform", return_value=0.1),
        patch("src.consumer.main.random.random", return_value=0.05),
        patch("src.consumer.main._send_webhook", mock_send_webhook),
    ):
        await _execute_payment_logic(
            payment_id=payment.id,
            session=mock_session,
            http_client=mock_client,
        )

    mock_send_webhook.assert_awaited_once_with(mock_client, payment)


async def test_execute_payment_logic_webhook_before_commit():
    """_send_webhook вызывается ПЕРЕД commit, чтобы при ошибке не сохранять статус."""
    payment = make_mock_payment()
    call_order = []

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=payment)

    async def record_commit():
        call_order.append("commit")

    mock_session.commit = AsyncMock(side_effect=record_commit)
    mock_session.refresh = AsyncMock()
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    async def record_webhook(client, p):
        call_order.append("webhook")

    with (
        patch("src.consumer.main.asyncio.sleep", new_callable=AsyncMock),
        patch("src.consumer.main.random.uniform", return_value=0.1),
        patch("src.consumer.main.random.random", return_value=0.05),
        patch("src.consumer.main._send_webhook", side_effect=record_webhook),
    ):
        await _execute_payment_logic(
            payment_id=payment.id,
            session=mock_session,
            http_client=mock_client,
        )

    assert call_order == ["webhook", "commit"], "webhook должен вызываться до commit"


async def test_execute_payment_logic_webhook_failure_propagates():
    """Если webhook упал — исключение пробрасывается, commit не вызывается."""
    payment = make_mock_payment()
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=payment)
    mock_session.commit = AsyncMock()
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("src.consumer.main.asyncio.sleep", new_callable=AsyncMock),
        patch("src.consumer.main.random.uniform", return_value=0.1),
        patch("src.consumer.main.random.random", return_value=0.05),
        patch(
            "src.consumer.main._send_webhook",
            AsyncMock(side_effect=httpx.RequestError("timeout", request=MagicMock())),
        ),
    ):
        with pytest.raises(httpx.RequestError):
            await _execute_payment_logic(
                payment_id=payment.id,
                session=mock_session,
                http_client=mock_client,
            )

    mock_session.commit.assert_not_awaited()
