import boto3 as _boto3

from cc_headless.adapters.secondary.session.dynamodb_session_store import (  # noqa: F401
    _TERMINAL_STATES,
    VALID_TRANSITIONS,
    DynamoDbSessionStore,
    InvalidStateTransitionError,
    SessionCancelledError,
    _now_iso,
    _ttl,
    build_rca_id,
)
from cc_headless.config.settings import DYNAMODB_TABLE_NAME, ENGINE  # noqa: F401

_ddb = _boto3.client("dynamodb")
_default_store = DynamoDbSessionStore(_ddb)


def check_duplicate(rca_id, *, dynamodb_client=None):
    store = DynamoDbSessionStore(dynamodb_client) if dynamodb_client else _default_store
    return store.check_duplicate(rca_id)


def create_session(rca_id, alarm_name, idempotency_key, *, alarm_data=None, dynamodb_client=None):
    store = DynamoDbSessionStore(dynamodb_client) if dynamodb_client else _default_store
    return store.create_session(rca_id, alarm_name, idempotency_key, alarm_data=alarm_data)


def update_state(rca_id, state, *, dynamodb_client=None):
    store = DynamoDbSessionStore(dynamodb_client) if dynamodb_client else _default_store
    return store.update_state(rca_id, state)


def mark_completed(rca_id, root_cause, *, dynamodb_client=None):
    store = DynamoDbSessionStore(dynamodb_client) if dynamodb_client else _default_store
    return store.mark_completed(rca_id, root_cause)


def mark_failed(rca_id, error_reason, *, dynamodb_client=None):
    store = DynamoDbSessionStore(dynamodb_client) if dynamodb_client else _default_store
    return store.mark_failed(rca_id, error_reason)


def mark_outdated(rca_id, reason, *, dynamodb_client=None):
    store = DynamoDbSessionStore(dynamodb_client) if dynamodb_client else _default_store
    return store.mark_outdated(rca_id, reason)
