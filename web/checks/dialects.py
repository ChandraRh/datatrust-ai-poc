"""
The few SQL differences between the databases DataTrust checks. Everything else in the engine is shared.
Identifiers are always quoted here; values never go into SQL text (they are compared in Python).
To add a database type (e.g. Snowflake): one more Dialect subclass + its connector in estate/.
"""
import re

READ_ONLY_VIOLATION = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|exec|execute|call|copy|vacuum|"
    r"reindex|into|commit|rollback|dbcc|backup|restore|shutdown|kill)\b",
    re.I)                                    # second line of defence: PostgreSQL runs checks read-only, SQL Server rolls back


def clean_select(sql: str) -> str:
    """A user-written query must be ONE read-only SELECT (or WITH ... SELECT). Returns it without a trailing ';'."""
    text = re.sub(r"--[^\n]*", " ", sql or "")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S).strip().rstrip(";").strip()
    if not text:
        raise ValueError("the query is empty")
    if ";" in text:
        raise ValueError("only one statement is allowed (remove the ';')")
    if not re.match(r"(?is)^(select|with)\b", text):
        raise ValueError("the query must start with SELECT or WITH")
    bad = READ_ONLY_VIOLATION.search(re.sub(r"'(?:[^']|'')*'", "''", text))      # ignore words inside string literals
    if bad:
        raise ValueError(f"'{bad.group(0)}' is not allowed: checks only read data")
    return text


def clean_where(where: str) -> str:
    """Optional row filter for a table check (e.g. business_date >= current_date - 7)."""
    where = (where or "").strip()
    if not where:
        return ""
    clean_select(f"SELECT 1 WHERE {where}")
    return where


class Dialect:
    name = ""
    param = "%s"

    def ident(self, name: str) -> str:
        raise NotImplementedError

    def table(self, schema: str, table: str) -> str:
        return f"{self.ident(schema)}.{self.ident(table)}"

    def where(self, where: str) -> str:
        w = clean_where(where)
        return f" WHERE {w}" if w else ""

    def limit(self, select: str, n: int) -> str:
        raise NotImplementedError

    def columns_sql(self) -> str:
        return (f"SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                f"WHERE table_schema = {self.param} AND table_name = {self.param} ORDER BY ordinal_position")


class Postgres(Dialect):
    name = "postgresql"
    param = "%s"

    def ident(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def limit(self, select: str, n: int) -> str:
        return f"SELECT * FROM ({select}) AS dt_q LIMIT {int(n)}"


class SqlServer(Dialect):
    name = "sqlserver"
    param = "?"

    def ident(self, name: str) -> str:
        return "[" + str(name).replace("]", "]]") + "]"

    def limit(self, select: str, n: int) -> str:
        if re.match(r"(?is)^with\b", select):                 # a CTE cannot sit in a derived table: rows are cut after fetch
            return select
        return f"SELECT TOP {int(n)} * FROM ({select}) AS dt_q"


DIALECTS = {"postgresql": Postgres(), "sqlserver": SqlServer()}


def for_type(source_type: str) -> Dialect:
    d = DIALECTS.get(source_type)
    if not d:
        raise ValueError(f"checks are not available for {source_type} yet (PostgreSQL and SQL Server are supported)")
    return d
