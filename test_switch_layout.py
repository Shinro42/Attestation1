import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import switch_layout as app


@pytest.fixture(autouse=True)
def working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    app.Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ghbdtn", "привет"),
        ("руддщ", "hello"),
        ("Ghbdtn", "Привет"),
        ("Руддщ", "Hello"),
        ("GHBDTN", "ПРИВЕТ"),
        ("РУДДЩ", "HELLO"),
        ("ghbdtn 123\n", "привет 123\n"),
        ("a;'", "фжэ"),
        ("ЖЭХЪ", ";'[]"),
        ("ghbdtn;", "приветж"),
        ("привет.", "ghbdtn/"),
        ("ghbdtn я", "привет я"),
        ("привет a", "ghbdtn a"),
    ],
)
def test_convert_layout(text, expected):
    assert app.convert_layout(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "\n", "123!?", "aя"])
def test_invalid_text(text):
    with pytest.raises(app.click.ClickException):
        app.convert_layout(text)


def test_save_and_count(session):
    app.save_translation(session, "en-ru", 6)
    app.save_translation(session, "en-ru", 57)
    app.save_translation(session, "ru-en", 5)

    assert app.count_translations(session) == ({"en-ru": 2, "ru-en": 1}, 57)


@pytest.mark.parametrize(
    "view, first, second",
    [
        (None, "en-ru: 2 (66.7%)", "ru-en: 1 (33.3%)"),
        ("abs", "en-ru: 2", "ru-en: 1"),
        ("rel", "en-ru: 66.7%", "ru-en: 33.3%"),
    ],
)
def test_statistics(session, view, first, second):
    app.save_translation(session, "en-ru", 6)
    app.save_translation(session, "en-ru", 57)
    app.save_translation(session, "ru-en", 5)

    assert app.format_statistics(session, view).splitlines() == [
        first,
        second,
        "Самый длинный текст: 57 символов",
    ]


@pytest.mark.parametrize("view", [None, "abs", "rel"])
def test_empty_database(session, view):
    assert app.count_translations(session) == ({}, 0)
    result = app.format_statistics(session, view)
    assert "Самый длинный текст: 0 символов" in result
    assert "en-ru: 0" in result
    assert "ru-en: 0" in result


def test_convert_command(session, monkeypatch):
    clipboard = {"text": "Ghbdtn"}
    monkeypatch.setattr(app, "get_session", lambda: session)
    monkeypatch.setattr(app.pyperclip, "paste", lambda: clipboard["text"])
    monkeypatch.setattr(app.pyperclip, "copy", lambda text: clipboard.update(text=text))

    result = CliRunner().invoke(app.cli, ["convert"])

    assert result.exit_code == 0
    assert result.output == "Привет\n"
    assert clipboard["text"] == "Привет"
    assert app.count_translations(session) == ({"en-ru": 1}, 6)


def test_empty_clipboard_command(session, monkeypatch):
    monkeypatch.setattr(app, "get_session", lambda: session)
    monkeypatch.setattr(app.pyperclip, "paste", lambda: "")

    result = CliRunner().invoke(app.cli, ["convert"])

    assert result.exit_code != 0
    assert "Буфер обмена пуст" in result.output
    assert app.count_translations(session) == ({}, 0)


def test_clipboard_failure(session, monkeypatch):
    monkeypatch.setattr(app.pyperclip, "paste", lambda: "ghbdtn")

    def fail_copy(text):
        raise app.pyperclip.PyperclipException("Буфер обмена недоступен")

    monkeypatch.setattr(app.pyperclip, "copy", fail_copy)
    monkeypatch.setattr(app, "get_session", lambda: session)

    result = CliRunner().invoke(app.cli, ["convert"])

    assert result.exit_code != 0
    assert "Не удалось прочитать или изменить буфер обмена" in result.output
    assert app.count_translations(session) == ({}, 0)


@pytest.mark.parametrize("view", [None, "abs", "rel"])
def test_stats_command(session, monkeypatch, view):
    monkeypatch.setattr(app, "get_session", lambda: session)
    app.save_translation(session, "en-ru", 6)
    arguments = ["stats"]
    if view:
        arguments += ["--view", view]

    result = CliRunner().invoke(app.cli, arguments)

    assert result.exit_code == 0
    assert result.output == app.format_statistics(session, view) + "\n"


def test_database_created_on_launch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    first = runner.invoke(app.cli, ["stats"])
    assert first.exit_code == 0
    assert (tmp_path / "stats.db").exists()

    with app.get_session() as session:
        app.save_translation(session, "ru-en", 12)

    second = runner.invoke(app.cli, ["stats", "--view", "abs"])
    assert second.exit_code == 0
    assert "ru-en: 1" in second.output
    assert "Самый длинный текст: 12 символов" in second.output


def test_transaction_rollback(session):
    with pytest.raises(IntegrityError):
        app.save_translation(session, "en-ru", None)

    assert app.count_translations(session) == ({}, 0)
    app.save_translation(session, "ru-en", 5)
    assert app.count_translations(session) == ({"ru-en": 1}, 5)


@pytest.mark.parametrize("problem", ["directory", "corrupt"])
def test_database_error(problem, tmp_path):
    path = tmp_path / "stats.db"
    if problem == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"broken database" * 100)

    result = CliRunner().invoke(app.cli, ["stats"])

    assert result.exit_code != 0
    assert "Не удалось прочитать статистику" in result.output
    assert "Traceback" not in result.output
    log = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "ERROR" in log
    assert "Ошибка чтения статистики" in log


def test_conversion_database_error(tmp_path, monkeypatch):
    (tmp_path / "stats.db").mkdir()
    clipboard = {"text": "ghbdtn"}
    monkeypatch.setattr(app.pyperclip, "paste", lambda: clipboard["text"])
    monkeypatch.setattr(app.pyperclip, "copy", lambda text: clipboard.update(text=text))

    result = CliRunner().invoke(app.cli, ["convert"])

    assert result.exit_code != 0
    assert clipboard["text"] == "привет"
    assert "Текст исправлен в буфере, но статистика не сохранена" in result.output
    assert "Traceback" not in result.output


def test_logging_conversion(tmp_path, monkeypatch):
    clipboard = {"text": "ghbdtn"}
    monkeypatch.setattr(app.pyperclip, "paste", lambda: clipboard["text"])
    monkeypatch.setattr(app.pyperclip, "copy", lambda text: clipboard.update(text=text))
    handlers_before = list(app.logger.handlers)

    result = CliRunner().invoke(app.cli, ["convert"])

    assert result.exit_code == 0
    log = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "Преобразование выполнено: en-ru, длина 6" in log
    assert "ghbdtn" not in log
    assert app.logger.handlers == handlers_before


def test_logging_validation_error(tmp_path, monkeypatch):
    monkeypatch.setattr(app.pyperclip, "paste", lambda: "123")

    result = CliRunner().invoke(app.cli, ["convert"])

    assert result.exit_code != 0
    log = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "WARNING" in log
    assert "Ошибка входных данных" in log


def test_logging_file_unavailable(tmp_path):
    (tmp_path / "app.log").mkdir()

    result = CliRunner().invoke(app.cli, ["stats"])

    assert result.exit_code != 0
    assert "Не удалось открыть app.log" in result.output
    assert "Traceback" not in result.output


def test_engine_disposed(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    disposed = []
    event.listen(engine, "engine_disposed", lambda engine: disposed.append(True))
    monkeypatch.setattr(app, "create_engine", lambda url: engine)

    with app.get_session() as session:
        app.save_translation(session, "en-ru", 6)

    assert disposed == [True]


def test_engine_disposed_after_error(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    disposed = []
    event.listen(engine, "engine_disposed", lambda engine: disposed.append(True))
    monkeypatch.setattr(app, "create_engine", lambda url: engine)

    with pytest.raises(IntegrityError), app.get_session() as session:
        app.save_translation(session, "en-ru", None)

    assert disposed == [True]


@pytest.mark.parametrize("arguments", [["stats", "--view", "wrong"], ["unknown"]])
def test_invalid_command(arguments):
    result = CliRunner().invoke(app.cli, arguments)

    assert result.exit_code != 0
    assert "Error:" in result.output
