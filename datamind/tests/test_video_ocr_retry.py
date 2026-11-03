"""Retry malformed provider output without accepting partial video evidence."""
import asyncio
import base64
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from datamind.config import VideoConfig
from datamind.core.errors import CapabilityError
from datamind.capabilities.ingest.video import providers


def response(text, stop_reason='stop'):
    return SimpleNamespace(content=[{'type': 'text', 'text': text}],
                           stop_reason=stop_reason, resolved_model='fixture-vision')


@pytest.fixture
def probe(tmp_path, monkeypatch):
    image = tmp_path / 'frame.png'
    image.write_bytes(b'original-frame')
    records, usage = [], []
    monkeypatch.setattr(providers, 'record_diagnostic', lambda kind, data: records.append(dict(data)))
    monkeypatch.setattr('datamind.core.model_clients._record_nested_usage', usage.append)

    class Client:
        def __init__(self, outputs):
            self.outputs, self.calls = iter(outputs), []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            value = next(self.outputs)
            if isinstance(value, BaseException):
                raise value
            return value

    return image, records, usage, Client


async def test_retry_retranscribes_original_image_and_counts_every_response(probe):
    image, records, usage, Client = probe
    client = Client([response('["page",["text","42"]'), response('["page",["text","42"]]')])
    result = await providers.describe_frame(client, image, VideoConfig(
        vision={'model': 'fixture-vision'}, ocr_format_retries=2))
    assert result['ocr_text'] == '42'
    assert len(client.calls) == len(usage) == len(records) == 2
    assert records[0]['retrying'] and 'parse_error' in records[0]
    assert records[0]['response_text'] == '["page",["text","42"]'
    assert records[1]['attempt'] == 2 and not records[1]['retrying']
    for call in client.calls:
        assert len(call['messages']) == 1
        content = call['messages'][0]['content']
        assert base64.b64decode(content[1]['source']['data']) == b'original-frame'


@pytest.mark.parametrize('bad', ['["page",', '["page",42]', 'not JSON'])
async def test_exhaustion_rejects_incomplete_or_invalid_evidence(probe, bad):
    image, records, usage, Client = probe
    client = Client([response(bad)] * 3)
    with pytest.raises(CapabilityError, match='invalid JSON Hiccup') as exc:
        await providers.describe_frame(client, image, VideoConfig(
            vision={'model': 'fixture'}, ocr_format_retries=2))
    assert isinstance(exc.value.__cause__, ValueError)
    assert len(client.calls) == len(usage) == len(records) == 3
    assert [r['retrying'] for r in records] == [True, True, False]
    assert 'error' in records[-1]


@pytest.mark.parametrize('reason', ['length', 'max_tokens'])
async def test_truncation_is_not_retried_with_unchanged_token_limit(probe, reason):
    image, records, usage, Client = probe
    client = Client([response('["page"]', reason)])
    with pytest.raises(CapabilityError, match='truncated'):
        await providers.describe_frame(client, image, VideoConfig(
            vision={'model': 'fixture'}, ocr_format_retries=2))
    assert len(client.calls) == len(usage) == 1
    assert not records[0]['retrying']


@pytest.mark.parametrize('error', [RuntimeError('transport failed'), asyncio.CancelledError()])
async def test_transport_errors_and_cancellation_are_not_format_retries(probe, error):
    image, records, usage, Client = probe
    client = Client([error])
    with pytest.raises(type(error)):
        await providers.describe_frame(client, image, VideoConfig(
            vision={'model': 'fixture'}, ocr_format_retries=2))
    assert len(client.calls) == 1 and not usage
    assert not records[0]['retrying'] and 'error' in records[0]


def test_retry_budget_is_bounded_and_disabled_by_default():
    assert VideoConfig().ocr_format_retries == 0
    for value in (-1, 4):
        with pytest.raises(ValidationError):
            VideoConfig(ocr_format_retries=value)
