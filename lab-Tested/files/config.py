
import os
import psycopg2
from psycopg2.extras import RealDictCursor

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "learning_labs")
DB_USER = os.getenv("DB_USER", "learning_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")


def get_db():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def get_cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)


def convert_placeholders(sql):
    return sql.replace("?", "%s")


def execute_script(conn, sql):
    with conn.cursor() as cur:
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)
    conn.commit()


def fetch_all(conn, sql, params=None):
    with get_cursor(conn) as cur:
        cur.execute(convert_placeholders(sql), params or ())
        return cur.fetchall()


def fetch_one(conn, sql, params=None):
    with get_cursor(conn) as cur:
        cur.execute(convert_placeholders(sql), params or ())
        return cur.fetchone()


