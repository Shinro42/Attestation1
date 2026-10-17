import logging
import re
from contextlib import contextmanager
import click
import pyperclip
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

EN_LOWER = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./"
RU_LOWER = "ёйцукенгшщзхъфывапролджэячсмитьбю."

EN_TO_RU = str.maketrans(EN_LOWER, RU_LOWER)
RU_TO_EN = str.maketrans(RU_LOWER, EN_LOWER)

for en, ru in zip(EN_LOWER.upper(), RU_LOWER.upper()):
    if en.isalpha():
        EN_TO_RU[ord(en)] = ru
    if ru.isalpha():
        RU_TO_EN[ord(ru)] = en

CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")
LATIN_RE = re.compile(r"[a-zA-Z]")
logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Translation(Base):
    __tablename__ = "translations"

    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str]
    length: Mapped[int]


def detect_direction(text):
    if not text.strip():
        raise click.ClickException("Буфер обмена пуст.")

    cyrillic_count = len(CYRILLIC_RE.findall(text))
    latin_count = len(LATIN_RE.findall(text))

    if cyrillic_count == 0 and latin_count == 0:
        raise click.ClickException("В тексте нет русских или английских букв.")

    if cyrillic_count == latin_count:
        raise click.ClickException("Русских и английских букв поровну. Скопируйте текст одной раскладки.")

    if cyrillic_count > latin_count:
        return "ru-en"
    return "en-ru"


def convert_layout(text) -> str:
    direction = detect_direction(text)
    if direction == "en-ru":
        return text.translate(EN_TO_RU)
    return text.translate(RU_TO_EN)


@contextmanager
def get_session():
    engine = create_engine("sqlite:///stats.db")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def save_translation(session, direction, length):
    try:
        session.add(Translation(direction=direction, length=length))
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise


def count_translations(session):
    rows = session.execute(
        select(Translation.direction, func.count()).group_by(Translation.direction)).all()
    counts = dict(rows)
    longest = session.scalar(select(func.max(Translation.length))) or 0
    return counts, longest


def format_stat(count, total, view):
    percent = count / total * 100 if total else 0
    if view == "abs":
        return f"{count}"
    if view == "rel":
        return f"{percent:.1f}%"
    return f"{count} ({percent:.1f}%)"


def format_statistics(session, view):
    counts, longest = count_translations(session)
    total = sum(counts.values())
    lines = []

    for direction in ("en-ru", "ru-en"):
        value = format_stat(counts.get(direction, 0), total, view)
        lines.append(f"{direction}: {value}")

    lines.append(f"Самый длинный текст: {longest} символов")
    return "\n".join(lines)


@click.group()
@click.pass_context
def cli(ctx):
    try:
        handler = logging.FileHandler("app.log", encoding="utf-8")
    except OSError as error:
        raise click.ClickException("Не удалось открыть app.log. Проверьте права на запись в текущую папку.") from error

    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    ctx.call_on_close(handler.close)
    ctx.call_on_close(lambda: logger.removeHandler(handler))
    logger.info("Запуск команды: %s", ctx.invoked_subcommand)


@cli.command()
def convert():
    try:
        text = pyperclip.paste()
        result = convert_layout(text)
        direction = detect_direction(text)
        pyperclip.copy(result)
    except click.ClickException as error:
        logger.warning("Ошибка входных данных: %s", error)
        raise
    except pyperclip.PyperclipException as error:
        logger.exception("Ошибка доступа к буферу обмена")
        raise click.ClickException("Не удалось прочитать или изменить буфер обмена. ") from error

    try:
        with get_session() as session:
            save_translation(session, direction, len(text))
    except SQLAlchemyError as error:
        logger.exception("Ошибка сохранения статистики")
        raise click.ClickException("Текст исправлен в буфере, но статистика не сохранена.") from error

    logger.info("Преобразование выполнено: %s, длина %s", direction, len(text))
    click.echo(result)


@cli.command()
@click.option("--view", type=click.Choice(["abs", "rel"]), default=None)
def stats(view):
    try:
        with get_session() as session:
            result = format_statistics(session, view)
    except SQLAlchemyError as error:
        logger.exception("Ошибка чтения статистики")
        raise click.ClickException("Не удалось прочитать статистику.") from error

    logger.info("Статистика показана: %s", view or "abs+rel")
    click.echo(result)


if __name__ == "__main__":
    cli()
