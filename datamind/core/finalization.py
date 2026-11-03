"""Local final-answer checks; recovery stays in the existing main-model session."""
from __future__ import annotations

import asyncio
import inspect
import json

from jsonschema import Draft202012Validator, ValidationError

from .diagnostics import redact
from .errors import CapabilityError, FinalAnswerContractError


async def validate_final_answer(answer, *, schema=None, validator=None,
                                request_id, workspace_revision, settings=None):
    """Return parsed output, or a recoverable rejection; never call a model.

    A validator receives parsed JSON when a schema is supplied, otherwise raw
    text. It returns None on success, or raises a descriptive ValueError or
    CapabilityError. Unexpected programmer errors and cancellation propagate.
    Synchronous validators run off the event loop so file reads share the host
    deadline; they must be read-only because cancellation cannot stop a thread.
    """
    try:
        value = json.loads(answer) if schema is not None else answer
        if schema is not None:
            Draft202012Validator(schema).validate(value)
        if validator is not None:
            arguments = {'request_id': request_id, 'workspace_revision': workspace_revision}
            if inspect.iscoroutinefunction(validator) or inspect.iscoroutinefunction(getattr(validator, '__call__', None)):
                outcome = await validator(value, **arguments)
            else:
                outcome = await asyncio.to_thread(validator, value, **arguments)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if outcome is not None:
                raise TypeError('A final_validator must return None on success or raise a validation error')
        return value
    except json.JSONDecodeError as exc:
        reason = f'Final output must be valid JSON (line {exc.lineno}, column {exc.colno}).'
    except ValidationError as exc:
        location = '/' + '/'.join(str(x) for x in exc.absolute_path)
        # Do not echo the failed model output, which can contain large tool dumps.
        rule = json.dumps(exc.validator_value, ensure_ascii=False)
        reason = f'Final output at {location} violates {exc.validator}: {rule[:1000]}.'
    except FinalAnswerContractError:
        raise
    except (CapabilityError, ValueError) as exc:
        reason = str(exc)
    raise FinalAnswerContractError(redact(reason, settings)[:2000], stop_reason='validation_failed') from None
