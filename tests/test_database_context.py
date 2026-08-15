from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import text

from src.platform.contracts import UserContext
from src.platform.database import Database, _reset_postgres_connection, set_transaction_user_context


def test_transaction_user_context_is_parameterized_and_local() -> None:
    session = Mock()
    identity = UserContext(
        user_id="1",
        session_id="11",
        is_platform_admin=True,
    )

    set_transaction_user_context(session, identity)

    statement, parameters = session.execute.call_args.args
    assert "set_config('app.current_user_id', :user_id, true)" in str(statement)
    assert parameters == {
        "user_id": "1",
        "session_id": "11",
        "is_platform_admin": "true",
    }


def test_pool_reset_rolls_back_and_clears_postgres_settings() -> None:
    connection = Mock()
    cursor = connection.cursor.return_value

    _reset_postgres_connection(
        connection,
        None,
        SimpleNamespace(terminate_only=False),
    )

    assert connection.rollback.call_count == 2
    cursor.execute.assert_called_once_with("RESET ALL")
    cursor.close.assert_called_once_with()


def test_reused_pool_connection_cannot_inherit_previous_user_context() -> None:
    first_session = Mock()
    second_session = Mock()
    connection = Mock()
    reset_state = SimpleNamespace(terminate_only=False)
    first_user = UserContext(user_id="1", session_id="11")
    second_user = UserContext(user_id="2", session_id="12")

    set_transaction_user_context(first_session, first_user)
    _reset_postgres_connection(connection, None, reset_state)
    set_transaction_user_context(second_session, second_user)

    assert first_session.execute.call_args.args[1]["user_id"] == "1"
    connection.cursor.return_value.execute.assert_called_once_with("RESET ALL")
    assert second_session.execute.call_args.args[1]["user_id"] == "2"


def test_session_transaction_commits_and_closes() -> None:
    database = Database("sqlite+pysqlite:///:memory:")

    with database.transaction() as session:
        assert session.scalar(text("SELECT 1")) == 1

    assert not session.in_transaction()
    database.dispose()
