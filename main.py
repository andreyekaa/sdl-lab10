import logging
import os
import re
import secrets
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import psycopg
import yaml
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from psycopg import errors, sql

WHITELIST = {"host", "port", "dbname", "sslmode", "connect_timeout", "application_name"}
LOG_ENV_VAR = "APP_LOG_FILE"
CONFIG_ENV_VAR = "APP_CONFIG_PATH"
MAX_DISPLAY_ROWS = 200
TOKEN_SESSION_KEY = "credential_token"


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    is_identity: bool
    has_default: bool
    is_nullable: bool


@dataclass(frozen=True)
class ForeignKeyInfo:
    constraint_name: str
    column_name: str
    ref_table: str
    ref_column: str


@dataclass
class TableInfo:
    name: str
    columns: List[ColumnInfo] = field(default_factory=list)
    pk_columns: List[str] = field(default_factory=list)
    foreign_keys: List[ForeignKeyInfo] = field(default_factory=list)

    def column_map(self) -> Dict[str, ColumnInfo]:
        return {column.name: column for column in self.columns}


@dataclass(frozen=True)
class DbCredentials:
    user: str
    password: str


class MaxLevelFilter(logging.Filter):
    def __init__(self, exclusive_max: int):
        super().__init__()
        self.exclusive_max = exclusive_max

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.exclusive_max


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("db_app")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    logging.Formatter.converter = time.gmtime
    datefmt = "%Y-%m-%dT%H:%M:%S"
    console_fmt = "%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s"
    file_fmt = "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s"

    console_formatter = logging.Formatter(console_fmt, datefmt=datefmt)
    file_formatter = logging.Formatter(file_fmt, datefmt=datefmt)

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(MaxLevelFilter(logging.WARNING))
    stdout_handler.setFormatter(console_formatter)
    logger.addHandler(stdout_handler)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(console_formatter)
    logger.addHandler(stderr_handler)

    log_file = os.getenv(LOG_ENV_VAR, "").strip()
    if log_file:
        try:
            path = Path(log_file)
            if path.is_dir():
                path = path / "db_app.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)
        except Exception as exc:
            logger.warning("File logging disabled: %s", exc)

    return logger


logger = setup_logging()
credential_store: Dict[str, DbCredentials] = {}


def load_config(cfg_path: str | Path) -> Dict[str, Any]:
    path = Path(cfg_path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("config must be .yaml/.yml")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config file must contain a key-value mapping")

    config: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in WHITELIST:
            continue
        if key in {"port", "connect_timeout"}:
            config[key] = int(value)
        else:
            config[key] = value
    return config


def get_base_config() -> Dict[str, Any]:
    config_path = os.getenv(CONFIG_ENV_VAR) or str(Path(__file__).with_name("config.yaml"))
    return load_config(config_path)


def log_friendly_error(log: logging.Logger, user_message: str, exc: Exception) -> None:
    log.error(user_message)
    log.debug("DETAIL: %s", str(exc))
    log.debug("TRACE: %s", traceback.format_exc())


def connection_error_message(exc: Exception) -> str:
    text = str(exc).lower()
    if "password authentication failed" in text:
        return "Не удалось подключиться: неверный логин или пароль."
    if "connection refused" in text:
        return "Не удалось подключиться: сервер БД отклонил соединение."
    if "timeout" in text or "timed out" in text:
        return "Не удалось подключиться: истекло время ожидания БД."
    if "does not exist" in text and "database" in text:
        return "Не удалось подключиться: база данных из конфига не найдена."
    return "Не удалось подключиться к БД. Проверьте учетные данные и настройки."


def query_error_message(exc: Exception) -> str:
    if isinstance(exc, errors.UniqueViolation):
        return "Операция не выполнена: значение должно быть уникальным."
    if isinstance(exc, errors.ForeignKeyViolation):
        return "Операция не выполнена: связанная запись не найдена."
    if isinstance(exc, errors.NotNullViolation):
        return "Операция не выполнена: обязательное поле не заполнено."
    if isinstance(exc, errors.CheckViolation):
        return "Операция не выполнена: значение нарушает ограничение таблицы."
    if isinstance(exc, errors.InvalidTextRepresentation):
        return "Операция не выполнена: значение имеет неверный формат."
    return "Запрос не выполнен. Проверьте введенные данные."


def connect_db(credentials: DbCredentials) -> psycopg.Connection:
    return psycopg.connect(**get_base_config(), user=credentials.user, password=credentials.password)


def current_credentials() -> DbCredentials | None:
    token = session.get(TOKEN_SESSION_KEY)
    if not token:
        return None
    return credential_store.get(token)


def forget_credentials() -> None:
    token = session.pop(TOKEN_SESSION_KEY, None)
    if token:
        credential_store.pop(token, None)
    session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_credentials() is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def fetch_schema(conn: psycopg.Connection) -> Dict[str, TableInfo]:
    schema: Dict[str, TableInfo] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name;
            """
        )
        for (table_name,) in cur.fetchall():
            schema[table_name] = TableInfo(name=table_name)

        if not schema:
            return schema

        table_names = list(schema)

        cur.execute(
            """
            SELECT table_name, column_name, data_type, is_identity, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(%s)
            ORDER BY table_name, ordinal_position;
            """,
            (table_names,),
        )
        for table_name, column_name, data_type, is_identity, column_default, is_nullable in cur.fetchall():
            schema[table_name].columns.append(
                ColumnInfo(
                    name=column_name,
                    data_type=data_type,
                    is_identity=(is_identity == "YES"),
                    has_default=(column_default is not None),
                    is_nullable=(is_nullable == "YES"),
                )
            )

        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = 'public' AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY tc.table_name, kcu.ordinal_position;
            """
        )
        for table_name, column_name in cur.fetchall():
            if table_name in schema:
                schema[table_name].pk_columns.append(column_name)

        cur.execute(
            """
            SELECT
                tc.constraint_name,
                tc.table_name,
                kcu.column_name,
                ccu.table_name AS ref_table_name,
                ccu.column_name AS ref_column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.table_schema = 'public' AND tc.constraint_type = 'FOREIGN KEY'
            ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position;
            """
        )
        for constraint_name, table_name, column_name, ref_table_name, ref_column_name in cur.fetchall():
            if table_name in schema and ref_table_name in schema:
                schema[table_name].foreign_keys.append(
                    ForeignKeyInfo(
                        constraint_name=constraint_name,
                        column_name=column_name,
                        ref_table=ref_table_name,
                        ref_column=ref_column_name,
                    )
                )

    return schema


def format_table_label(table: TableInfo) -> str:
    pk = ", ".join(table.pk_columns) if table.pk_columns else "-"
    foreign_keys = ", ".join(
        f"{fk.column_name}->{fk.ref_table}.{fk.ref_column}" for fk in table.foreign_keys
    ) or "-"
    return f"{table.name} | PK: {pk} | FK: {foreign_keys}"


def table_options(schema: Dict[str, TableInfo]) -> List[TableInfo]:
    return [schema[name] for name in sorted(schema)]


def insertable_columns(table: TableInfo, skip_names: Iterable[str] = ()) -> List[ColumnInfo]:
    skip = set(skip_names)
    return [column for column in table.columns if not column.is_identity and column.name not in skip]


def single_pk_column(table: TableInfo) -> ColumnInfo | None:
    if len(table.pk_columns) != 1:
        return None
    return table.column_map().get(table.pk_columns[0])


def parse_value(raw: str, data_type: str) -> Any:
    text = raw.strip()
    if text.lower() in {"null", "none"}:
        return None

    data_type = data_type.lower()
    try:
        if data_type in {"smallint", "integer", "bigint"}:
            return int(text)
        if data_type in {"real", "double precision", "numeric", "decimal"}:
            return Decimal(text)
        if data_type == "boolean":
            truth_map = {
                "1": True,
                "true": True,
                "t": True,
                "yes": True,
                "y": True,
                "0": False,
                "false": False,
                "f": False,
                "no": False,
                "n": False,
            }
            if text.lower() not in truth_map:
                raise ValueError("invalid boolean")
            return truth_map[text.lower()]
        if data_type == "date":
            return date.fromisoformat(text)
        if "timestamp" in data_type:
            return datetime.fromisoformat(text)
    except Exception as exc:
        raise ValueError(f"Некорректное значение для типа {data_type}: {text}") from exc

    return text


def parse_form_value(column: ColumnInfo, raw: str) -> Any:
    text = raw.strip()
    if text == "":
        if column.is_nullable:
            return None
        raise ValueError(f"Поле {column.name} обязательно.")

    value = parse_value(text, column.data_type)
    if value is None and not column.is_nullable:
        raise ValueError(f"Поле {column.name} не может быть NULL.")
    return value


def split_group_values(raw: str) -> List[str]:
    return [part.strip() for part in re.split(r"[,;\n]+", raw) if part.strip()]


def parse_group_values(column: ColumnInfo, raw: str) -> List[Any]:
    parts = split_group_values(raw)
    if not parts:
        raise ValueError("Укажите хотя бы одно значение для фильтра.")

    values: List[Any] = []
    for part in parts:
        value = parse_value(part, column.data_type)
        if value is None:
            raise ValueError("NULL нельзя передавать в группу значений IN (...).")
        values.append(value)
    return values


def make_insert_query(table_name: str, column_names: Sequence[str], returning: str | None = None) -> sql.SQL:
    if column_names:
        query = (
            sql.SQL("INSERT INTO {} (").format(sql.Identifier(table_name))
            + sql.SQL(", ").join(sql.Identifier(name) for name in column_names)
            + sql.SQL(") VALUES (")
            + sql.SQL(", ").join(sql.Placeholder() for _ in column_names)
            + sql.SQL(")")
        )
    else:
        query = sql.SQL("INSERT INTO {} DEFAULT VALUES").format(sql.Identifier(table_name))

    if returning:
        query += sql.SQL(" RETURNING {}").format(sql.Identifier(returning))
    query += sql.SQL(";")
    return query


def make_select_query(
    table_name: str,
    filters: Sequence[tuple[str, Any]],
    limit: int | None = None,
) -> tuple[sql.SQL, tuple[Any, ...]]:
    query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table_name))
    params: List[Any] = []

    if filters:
        predicates: List[sql.SQL] = []
        for column_name, value in filters:
            if value is None:
                predicates.append(sql.SQL("{} IS NULL").format(sql.Identifier(column_name)))
            else:
                predicates.append(sql.SQL("{} = %s").format(sql.Identifier(column_name)))
                params.append(value)
        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(predicates)

    if limit is not None:
        query += sql.SQL(" LIMIT %s")
        params.append(limit)

    query += sql.SQL(";")
    return query, tuple(params)


def make_update_query(table_name: str, assignments: Sequence[str], pk_column: str) -> sql.SQL:
    return (
        sql.SQL("UPDATE {} SET ").format(sql.Identifier(table_name))
        + sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in assignments)
        + sql.SQL(" WHERE {} = %s;").format(sql.Identifier(pk_column))
    )


def make_bulk_update_query(table_name: str, set_column: str, filter_column: str, value_count: int) -> sql.SQL:
    return (
        sql.SQL("UPDATE {} SET {} = %s WHERE {} IN (").format(
            sql.Identifier(table_name),
            sql.Identifier(set_column),
            sql.Identifier(filter_column),
        )
        + sql.SQL(", ").join(sql.Placeholder() for _ in range(value_count))
        + sql.SQL(");")
    )


def format_cell(value: Any) -> str:
    if value is None:
        return "NULL"
    return str(value)


def rows_from_cursor(cur: psycopg.Cursor) -> tuple[List[str], List[Dict[str, str]], List[Dict[str, Any]]]:
    headers = [item.name for item in cur.description or []]
    raw_rows = [dict(zip(headers, row)) for row in cur.fetchall()]
    rendered_rows = [
        {header: format_cell(row.get(header)) for header in headers}
        for row in raw_rows
    ]
    return headers, rendered_rows, raw_rows


def fetch_table_rows(
    conn: psycopg.Connection,
    table: TableInfo,
    filters: Sequence[tuple[str, Any]] = (),
) -> tuple[List[str], List[Dict[str, str]], List[Dict[str, Any]]]:
    query, params = make_select_query(table.name, filters, limit=MAX_DISPLAY_ROWS)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return rows_from_cursor(cur)


def fetch_row_by_pk(
    conn: psycopg.Connection,
    table: TableInfo,
    pk_column: ColumnInfo,
    pk_value: Any,
) -> Dict[str, Any] | None:
    query = (
        sql.SQL("SELECT * FROM {} WHERE {} = %s LIMIT 1;").format(
            sql.Identifier(table.name),
            sql.Identifier(pk_column.name),
        )
    )
    with conn.cursor() as cur:
        cur.execute(query, (pk_value,))
        row = cur.fetchone()
        if row is None:
            return None
        headers = [item.name for item in cur.description or []]
        return dict(zip(headers, row))


def get_table(schema: Dict[str, TableInfo], table_name: str) -> TableInfo:
    table = schema.get(table_name)
    if table is None:
        abort(404)
    return table


def collect_filters(table: TableInfo) -> tuple[List[tuple[str, Any]], Dict[str, str], List[str]]:
    filters: List[tuple[str, Any]] = []
    raw_filters: Dict[str, str] = {}
    errors_found: List[str] = []

    for column in table.columns:
        key = f"filter__{column.name}"
        raw_value = request.args.get(key, "").strip()
        raw_filters[column.name] = raw_value
        if raw_value == "":
            continue
        try:
            filters.append((column.name, parse_value(raw_value, column.data_type)))
        except ValueError as exc:
            errors_found.append(f"{column.name}: {exc}")

    return filters, raw_filters, errors_found


def collect_insert_values(table: TableInfo) -> tuple[List[str], List[Any], List[str]]:
    column_names: List[str] = []
    values: List[Any] = []
    errors_found: List[str] = []

    for column in insertable_columns(table):
        raw = request.form.get(column.name, "")
        if raw.strip() == "":
            if column.is_nullable or column.has_default:
                continue
            errors_found.append(f"Поле {column.name} обязательно.")
            continue
        try:
            value = parse_form_value(column, raw)
        except ValueError as exc:
            errors_found.append(str(exc))
            continue
        column_names.append(column.name)
        values.append(value)

    return column_names, values, errors_found


def collect_update_values(table: TableInfo) -> tuple[List[str], List[Any], List[str]]:
    assignments: List[str] = []
    values: List[Any] = []
    errors_found: List[str] = []

    for column in table.columns:
        if column.name in table.pk_columns:
            continue
        raw = request.form.get(column.name, "")
        try:
            value = parse_form_value(column, raw)
        except ValueError as exc:
            errors_found.append(str(exc))
            continue
        assignments.append(column.name)
        values.append(value)

    return assignments, values, errors_found


def render_page(template_name: str, **context: Any) -> str:
    return render_template(template_name, **context)


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("APP_FORCE_SECURE_COOKIES", "false").lower() in {"1", "true", "yes"},
)


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self';")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-XSS-Protection", "0")
    return response


@app.context_processor
def inject_template_globals() -> dict[str, Any]:
    return {"authenticated": current_credentials() is not None}


@app.route("/", methods=["GET", "POST"])
def login():
    if current_credentials() is not None and request.method == "GET":
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        user = request.form.get("user", "").strip()
        password = request.form.get("password", "")
        remote_addr = request.headers.get("X-Forwarded-For", request.remote_addr)

        if not user or not password:
            flash("Введите логин и пароль.", "error")
            logger.warning("Failed DB login attempt from %s: empty credentials", remote_addr)
            return render_page("login.html", title="Вход")

        credentials = DbCredentials(user=user, password=password)
        try:
            with connect_db(credentials) as conn:
                fetch_schema(conn)
        except Exception as exc:
            logger.warning(
                "Failed DB login attempt user=%r remote=%s reason=%s",
                user,
                remote_addr,
                connection_error_message(exc),
            )
            logger.debug("Failed login detail: %s", traceback.format_exc())
            flash(connection_error_message(exc), "error")
            return render_page("login.html", title="Вход")

        token = secrets.token_urlsafe(32)
        credential_store[token] = credentials
        session.clear()
        session[TOKEN_SESSION_KEY] = token
        logger.info("Successful DB login user=%s remote=%s", user, remote_addr)
        return redirect(url_for("dashboard"))

    return render_page("login.html", title="Вход")


@app.route("/logout")
def logout():
    forget_credentials()
    flash("Сессия завершена.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    credentials = current_credentials()
    assert credentials is not None
    return render_page(
        "dashboard.html",
        title="Главная",
        db_user=credentials.user,
    )


@app.route("/tables")
@login_required
def tables():
    credentials = current_credentials()
    assert credentials is not None
    try:
        with connect_db(credentials) as conn:
            schema = fetch_schema(conn)
            if not schema:
                flash("В схеме public нет таблиц.", "error")
                return redirect(url_for("dashboard"))

            tables_list = table_options(schema)
            table_name = request.args.get("table") or tables_list[0].name
            table = get_table(schema, table_name)
            filters, raw_filters, filter_errors = collect_filters(table)
            for error in filter_errors:
                flash(error, "error")
            if filter_errors:
                filters = []
            headers, rows, raw_rows = fetch_table_rows(conn, table, filters)
    except Exception as exc:
        log_friendly_error(logger, query_error_message(exc), exc)
        flash(query_error_message(exc), "error")
        return redirect(url_for("dashboard"))

    return render_page(
        "tables.html",
        title="Просмотр таблиц",
        tables=tables_list,
        table=table,
        pk_column=single_pk_column(table),
        headers=headers,
        rows=rows,
        raw_rows=raw_rows,
        raw_filters=raw_filters,
        max_rows=MAX_DISPLAY_ROWS,
    )


@app.route("/add")
@login_required
def add_index():
    credentials = current_credentials()
    assert credentials is not None
    try:
        with connect_db(credentials) as conn:
            schema = fetch_schema(conn)
    except Exception as exc:
        log_friendly_error(logger, connection_error_message(exc), exc)
        flash(connection_error_message(exc), "error")
        return redirect(url_for("dashboard"))

    return render_page(
        "table_picker.html",
        title="Добавление",
        heading="Добавление строк",
        description="Выберите таблицу для вставки новой записи.",
        tables=table_options(schema),
        endpoint="add_table",
        format_table_label=format_table_label,
    )


@app.route("/add/<table_name>", methods=["GET", "POST"])
@login_required
def add_table(table_name: str):
    credentials = current_credentials()
    assert credentials is not None
    form_values = dict(request.form)

    try:
        with connect_db(credentials) as conn:
            schema = fetch_schema(conn)
            table = get_table(schema, table_name)
            columns = insertable_columns(table)

            if request.method == "POST":
                column_names, values, errors_found = collect_insert_values(table)
                if errors_found:
                    for error in errors_found:
                        flash(error, "error")
                else:
                    query = make_insert_query(table.name, column_names)
                    with conn.cursor() as cur:
                        cur.execute(query, values)
                        affected_rows = cur.rowcount
                    conn.commit()
                    flash(f"Добавлено строк: {affected_rows}.", "success")
                    logger.info("Inserted row into %s by DB user %s", table.name, credentials.user)
                    return redirect(url_for("tables", table=table.name))
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        log_friendly_error(logger, query_error_message(exc), exc)
        flash(query_error_message(exc), "error")
        return redirect(url_for("add_table", table_name=table_name))

    return render_page(
        "add.html",
        title="Добавление строки",
        table=table,
        columns=columns,
        form_values=form_values,
    )


@app.route("/update")
@login_required
def update_index():
    credentials = current_credentials()
    assert credentials is not None
    try:
        with connect_db(credentials) as conn:
            schema = fetch_schema(conn)
    except Exception as exc:
        log_friendly_error(logger, connection_error_message(exc), exc)
        flash(connection_error_message(exc), "error")
        return redirect(url_for("dashboard"))

    return render_page(
        "table_picker.html",
        title="Обновление",
        heading="Обновление записи",
        description="Выберите таблицу. Страница поддерживает таблицы с одним первичным ключом.",
        tables=table_options(schema),
        endpoint="update_table",
        format_table_label=format_table_label,
    )


@app.route("/update/<table_name>", methods=["GET", "POST"])
@login_required
def update_table(table_name: str):
    credentials = current_credentials()
    assert credentials is not None
    pk_raw = request.values.get("pk", "").strip()
    form_values = dict(request.form)
    row_values: Dict[str, str] | None = None

    try:
        with connect_db(credentials) as conn:
            schema = fetch_schema(conn)
            table = get_table(schema, table_name)
            pk_column = single_pk_column(table)
            headers, rows, raw_rows = fetch_table_rows(conn, table)

            if pk_column and pk_raw:
                try:
                    pk_value = parse_form_value(pk_column, pk_raw)
                except ValueError as exc:
                    flash(str(exc), "error")
                    pk_value = None

                if pk_value is not None:
                    raw_row = fetch_row_by_pk(conn, table, pk_column, pk_value)
                    if raw_row is None:
                        flash("Запись с таким первичным ключом не найдена.", "error")
                    else:
                        row_values = {key: format_cell(value) for key, value in raw_row.items()}

            if request.method == "POST":
                if not pk_column:
                    flash("У таблицы нет одиночного первичного ключа.", "error")
                elif not pk_raw:
                    flash("Укажите первичный ключ.", "error")
                else:
                    pk_value = parse_form_value(pk_column, pk_raw)
                    assignments, values, errors_found = collect_update_values(table)
                    if errors_found:
                        for error in errors_found:
                            flash(error, "error")
                    else:
                        query = make_update_query(table.name, assignments, pk_column.name)
                        with conn.cursor() as cur:
                            cur.execute(query, tuple(values + [pk_value]))
                            affected_rows = cur.rowcount
                        conn.commit()
                        if affected_rows:
                            flash(f"Обновлено строк: {affected_rows}.", "success")
                            logger.info("Updated row in %s by DB user %s", table.name, credentials.user)
                            return redirect(url_for("tables", table=table.name))
                        flash("Запись не найдена, изменения не применены.", "error")
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        log_friendly_error(logger, query_error_message(exc), exc)
        flash(query_error_message(exc), "error")
        return redirect(url_for("update_table", table_name=table_name, pk=pk_raw))

    return render_page(
        "update.html",
        title="Обновление записи",
        table=table,
        pk_column=pk_column,
        pk_raw=pk_raw,
        row_values=row_values,
        form_values=form_values,
        headers=headers,
        rows=rows,
        raw_rows=raw_rows,
    )


@app.route("/bulk-update")
@login_required
def bulk_update_index():
    credentials = current_credentials()
    assert credentials is not None
    try:
        with connect_db(credentials) as conn:
            schema = fetch_schema(conn)
    except Exception as exc:
        log_friendly_error(logger, connection_error_message(exc), exc)
        flash(connection_error_message(exc), "error")
        return redirect(url_for("dashboard"))

    return render_page(
        "table_picker.html",
        title="Групповое обновление",
        heading="Групповое обновление",
        description="Выберите таблицу для обновления строк по группе однотипных значений.",
        tables=table_options(schema),
        endpoint="bulk_update_table",
        format_table_label=format_table_label,
    )


@app.route("/bulk-update/<table_name>", methods=["GET", "POST"])
@login_required
def bulk_update_table(table_name: str):
    credentials = current_credentials()
    assert credentials is not None
    form_values = dict(request.form)

    try:
        with connect_db(credentials) as conn:
            schema = fetch_schema(conn)
            table = get_table(schema, table_name)
            columns_by_name = table.column_map()
            updatable_columns = [column for column in table.columns if column.name not in table.pk_columns]

            if request.method == "POST":
                set_column = columns_by_name.get(request.form.get("set_column", ""))
                filter_column = columns_by_name.get(request.form.get("filter_column", ""))

                errors_found: List[str] = []
                if set_column is None or set_column.name in table.pk_columns:
                    errors_found.append("Выберите корректную изменяемую колонку.")
                if filter_column is None:
                    errors_found.append("Выберите корректную колонку фильтра.")

                set_value = None
                filter_values: List[Any] = []
                if set_column is not None:
                    try:
                        set_value = parse_form_value(set_column, request.form.get("set_value", ""))
                    except ValueError as exc:
                        errors_found.append(str(exc))
                if filter_column is not None:
                    try:
                        filter_values = parse_group_values(filter_column, request.form.get("filter_values", ""))
                    except ValueError as exc:
                        errors_found.append(str(exc))

                if errors_found:
                    for error in errors_found:
                        flash(error, "error")
                else:
                    assert set_column is not None
                    assert filter_column is not None
                    query = make_bulk_update_query(
                        table.name,
                        set_column.name,
                        filter_column.name,
                        len(filter_values),
                    )
                    with conn.cursor() as cur:
                        cur.execute(query, tuple([set_value] + filter_values))
                        affected_rows = cur.rowcount
                    conn.commit()
                    flash(f"Обновлено строк: {affected_rows}.", "success")
                    logger.info("Bulk updated %s row(s) in %s by DB user %s", affected_rows, table.name, credentials.user)
                    return redirect(url_for("tables", table=table.name))
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        log_friendly_error(logger, query_error_message(exc), exc)
        flash(query_error_message(exc), "error")
        return redirect(url_for("bulk_update_table", table_name=table_name))

    return render_page(
        "bulk_update.html",
        title="Групповое обновление",
        table=table,
        updatable_columns=updatable_columns,
        form_values=form_values,
    )


def main() -> int:
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8090"))
    debug = os.getenv("APP_DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
